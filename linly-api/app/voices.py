"""音色训练管理：上传音频 → 自动切片/识别 →（人工校对）→ 训练 → 注册成可用音色。

## 为什么训练**不走** TaskQueue

`queue.py` 那条队列是给口播推理用的：单 worker、asyncio.to_thread、一次几十秒。
训练是几十分钟起步的长任务，塞进去会把所有推理请求堵死，而且 `to_thread`
没法优雅终止。所以这里另起一套：**独立子进程 + 自己的状态表 + 文件日志**。

## 为什么要有「校对」这一环

实测过的真实案例：ASR 把「待办事项」识别成「代办事项」。这类同音字错误会
**直接教坏模型**，训练完才发现就白跑一趟。所以默认流程在 ASR 之后停下，
把标注交给人改，确认后再训练。想要全自动可以传 `auto_train=true` 跳过。

## 状态机

    uploading → asr（切片+识别中）→ reviewing（等人工校对）
                                          ├─(提交校对)─→ training → done
                                          └─(auto_train)──────────┘
    任意阶段出错 → failed
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config
# 状态枚举只在 schemas.py 定义一份（避免出现两处真值来源）
from .schemas import VoiceStatus

logger = logging.getLogger(__name__)


# pipeline.py 打印的阶段标记 → 进度。训练本身占大头，所以切分偏后。
_STAGE_PROGRESS = [
    ("音色训练开始", 0.02),
    ("切出", 0.08),
    ("标注", 0.20),
    ("预处理 1/3", 0.26),
    ("预处理 2/3", 0.34),
    ("预处理 3/3", 0.42),
    ("数据自检", 0.46),
    ("训练 s2", 0.50),
    ("训练 s1", 0.85),
    ("音色已注册", 1.0),
]

# 阶段标记 → 给用户看的中文进度文案
_STAGE_LABEL = {
    "音色训练开始": "准备中",
    "切出": "音频切片",
    "标注": "语音识别（ASR）",
    "预处理 1/3": "文本→音素",
    "预处理 2/3": "HuBERT 特征提取",
    "预处理 3/3": "语义 token 提取",
    "数据自检": "数据校验",
    "训练 s2": "训练 SoVITS（决定音色）",
    "训练 s1": "训练 GPT（决定韵律）",
    "音色已注册": "注册模型",
}


@dataclass
class VoiceRecord:
    voice_id: str
    status: VoiceStatus = VoiceStatus.ASR
    stage: str = "排队中"
    progress: float = 0.0
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    source_name: str = ""
    source_audio: str = ""
    segments: int = 0
    auto_train: bool = False
    epochs_s1: int = config.TRAIN_EPOCHS_S1
    epochs_s2: int = config.TRAIN_EPOCHS_S2
    error: str | None = None
    proc: subprocess.Popen | None = field(default=None, repr=False, compare=False)

    @property
    def work_dir(self) -> Path:
        return config.TRAIN_WORK_DIR / self.voice_id

    @property
    def transcript_path(self) -> Path:
        return self.work_dir / "transcript.txt"

    @property
    def log_path(self) -> Path:
        return self.work_dir / "api_train.log"

    @property
    def model_dir(self) -> Path:
        return config.VOICES_DIR / self.voice_id

    @property
    def elapsed(self) -> float | None:
        if self.started_at is None:
            return None
        return (self.finished_at or time.time()) - self.started_at

    def to_info(self) -> dict:
        return {
            "voice_id": self.voice_id,
            "status": self.status.value,
            "stage": self.stage,
            "progress": round(self.progress, 3),
            "created_at": _iso(self.created_at),
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "elapsed_seconds": round(self.elapsed, 1) if self.elapsed is not None else None,
            "source_name": self.source_name,
            "segments": self.segments,
            "auto_train": self.auto_train,
            "epochs_s1": self.epochs_s1,
            "epochs_s2": self.epochs_s2,
            "has_transcript": self.transcript_path.exists(),
            "model_ready": (self.model_dir / "sovits.pth").exists(),
            # 训练完成后，这个音色就能用 `finetuned:<voice_id>|...` 调用了
            "usage": f"finetuned:{self.voice_id}|<参考音频>|<参考文本>|中文",
            "error": self.error,
        }


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


class VoiceManager:
    """音色训练任务的管理者。单进程内存态 + 每任务 JSON 落盘。"""

    def __init__(self) -> None:
        self._records: dict[str, VoiceRecord] = {}
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------ 生命周期

    async def startup(self) -> None:
        """从磁盘恢复历史音色记录（服务重启后训练进程已死，标为失败）。"""
        config.TRAIN_WORK_DIR.mkdir(parents=True, exist_ok=True)
        for d in config.TRAIN_WORK_DIR.iterdir():
            if not d.is_dir():
                continue
            state = d / "record.json"
            if not state.exists():
                continue
            try:
                rec = VoiceRecord(**{**json.loads(state.read_text("utf-8")),
                                     "status": VoiceStatus("failed"),
                                     "error": "服务重启，训练进程已中断"})
                if rec.status in (VoiceStatus.ASR, VoiceStatus.TRAINING):
                    rec.status = VoiceStatus.FAILED
                elif (config.VOICES_DIR / rec.voice_id / "sovits.pth").exists():
                    rec.status, rec.progress = VoiceStatus.DONE, 1.0
                self._records[rec.voice_id] = rec
            except Exception:
                logger.warning("恢复音色记录失败：%s", d, exc_info=True)

        # 已注册但无记录的模型（手工放进 voices/ 的）也列出来，方便调用
        if config.VOICES_DIR.is_dir():
            for d in config.VOICES_DIR.iterdir():
                if d.is_dir() and (d / "sovits.pth").exists() \
                        and d.name not in self._records:
                    self._records[d.name] = VoiceRecord(
                        voice_id=d.name, status=VoiceStatus.DONE, progress=1.0,
                        stage="已注册（外部）", source_name="(外部导入)")
        logger.info("音色管理器就绪：%d 个音色记录", len(self._records))

    async def shutdown(self) -> None:
        for rec in self._records.values():
            if rec.proc and rec.proc.poll() is None:
                rec.proc.terminate()
        for t in self._tasks:
            t.cancel()

    # ------------------------------------------------------------ 查询

    def get(self, voice_id: str) -> VoiceRecord | None:
        return self._records.get(voice_id)

    def list(self) -> list[VoiceRecord]:
        return sorted(self._records.values(), key=lambda r: r.created_at, reverse=True)

    # ------------------------------------------------------------ 创建与执行

    async def create(self, voice_id: str, audio_path: Path, source_name: str, *,
                     auto_train: bool, epochs_s1: int, epochs_s2: int) -> VoiceRecord:
        if voice_id in self._records:
            raise ValueError(f"音色名 {voice_id!r} 已存在")
        if not re.fullmatch(r"[A-Za-z0-9_]{1,32}", voice_id):
            raise ValueError("音色名只允许字母/数字/下划线，且不超过 32 字符")

        rec = VoiceRecord(voice_id=voice_id, source_name=source_name,
                          source_audio=str(audio_path), auto_train=auto_train,
                          epochs_s1=epochs_s1, epochs_s2=epochs_s2)
        self._records[voice_id] = rec
        rec.work_dir.mkdir(parents=True, exist_ok=True)
        self._persist(rec)

        # 同名的产物目录先清掉，避免上一轮的残留 ckpt 被误当成新产物
        for sub in ("dataset", "slicer_opt", "gpt_weights", "sovits_weights",
                    "logs_s1"):
            shutil.rmtree(rec.work_dir / sub, ignore_errors=True)

        args = [
            config.TRAIN_PYTHON, str(config.TRAIN_SCRIPT),
            "--input", str(audio_path),
            "--voice", voice_id,
            "--work-dir", str(rec.work_dir),
            "--epochs-s1", str(epochs_s1),
            "--epochs-s2", str(epochs_s2),
        ]
        if not auto_train:
            # 第一段：只做到 ASR，产出 transcript.txt 停下来等人校对
            args.append("--stop-after-asr")

        self._spawn(rec, args)
        # 阶段标签决定收尾判定：auto_train 时子进程会一口气跑完训练，
        # 若仍标 "asr"，_finish 会把它当成「ASR 跑完等校对」而错置为 reviewing。
        t = asyncio.create_task(
            self._watch(rec, phase="asr" if not auto_train else "train"))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return rec

    async def start_training(self, rec: VoiceRecord, transcript: str | None) -> None:
        """人工校对完成后，带着校对文件启动训练。"""
        if rec.status not in (VoiceStatus.REVIEWING, VoiceStatus.FAILED):
            raise ValueError(f"当前状态 {rec.status.value} 不能启动训练")
        if transcript is not None:
            rec.transcript_path.write_text(transcript, encoding="utf-8")

        rec.status = VoiceStatus.TRAINING
        rec.error = None
        rec.stage = "启动训练"
        rec.progress = 0.46
        rec.finished_at = None
        self._persist(rec)

        args = [
            config.TRAIN_PYTHON, str(config.TRAIN_SCRIPT),
            "--input", rec.source_audio,
            "--voice", rec.voice_id,
            "--work-dir", str(rec.work_dir),
            "--epochs-s1", str(rec.epochs_s1),
            "--epochs-s2", str(rec.epochs_s2),
        ]
        if rec.transcript_path.exists():
            args += ["--transcript-file", str(rec.transcript_path)]
        self._spawn(rec, args)
        t = asyncio.create_task(self._watch(rec, phase="train"))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def read_transcript(self, rec: VoiceRecord) -> str:
        if not rec.transcript_path.exists():
            raise ValueError("标注文件还不存在——ASR 可能尚未完成")
        # 隐藏首部的说明性注释行，交给用户的应该是干净的正文，避免误改
        lines = [ln for ln in rec.transcript_path.read_text("utf-8").splitlines()
                 if not ln.startswith("#")]
        return "\n".join(lines) + "\n"

    def write_transcript(self, rec: VoiceRecord, text: str) -> int:
        clean = [ln for ln in text.splitlines() if ln.strip() and "\t" in ln]
        if not clean:
            raise ValueError("标注内容为空，或每行缺少 TAB 分隔（应为「文件名<TAB>文本」）")
        header = ("# 每行格式：文件名<TAB>识别文本\n"
                  "# 由人工校对后提交。不要改动左侧文件名，不要把 TAB 换成空格。\n")
        rec.transcript_path.write_text(header + "\n".join(clean) + "\n", encoding="utf-8")
        rec.status = VoiceStatus.REVIEWING
        rec.stage = "已提交校对，可开始训练"
        self._persist(rec)
        return len(clean)

    def delete(self, rec: VoiceRecord) -> None:
        if rec.proc and rec.proc.poll() is None:
            rec.proc.terminate()
        shutil.rmtree(rec.work_dir, ignore_errors=True)
        shutil.rmtree(rec.model_dir, ignore_errors=True)
        self._records.pop(rec.voice_id, None)
        logger.info("音色已删除：%s", rec.voice_id)

    # ------------------------------------------------------------ 内部

    def _spawn(self, rec: VoiceRecord, args: list[str]) -> None:
        env = os.environ.copy()
        # 平台注入的是 0，对 libgomp 非法（部署时踩过，见交接文档坑 3）
        env["OMP_NUM_THREADS"] = env.get("OMP_NUM_THREADS", "") or "8"
        env["MKL_NUM_THREADS"] = env.get("MKL_NUM_THREADS", "") or "8"
        env["PYTHONIOENCODING"] = "utf-8"
        if env["OMP_NUM_THREADS"] in ("0", ""):
            env["OMP_NUM_THREADS"] = "8"
        if env["MKL_NUM_THREADS"] in ("0", ""):
            env["MKL_NUM_THREADS"] = "8"

        rec.work_dir.mkdir(parents=True, exist_ok=True)
        rec.started_at = time.time()
        fh = open(rec.log_path, "a", encoding="utf-8")
        fh.write(f"\n{'=' * 60}\n$ {' '.join(args)}\n{'=' * 60}\n")
        fh.flush()
        # 日志是**追加**的，所以必须记住本轮从哪个偏移开始读。
        # 否则 _watch 会从 0 重读上一轮的内容（比如 ASR 阶段那句
        # 「已在 ASR 后停下」），把已经置成 TRAINING 的状态又改回 REVIEWING。
        rec._log_offset = fh.tell()  # type: ignore[attr-defined]
        logger.info("启动训练子进程：%s", rec.voice_id)
        rec.proc = subprocess.Popen(
            args, cwd=str(Path(config.TRAIN_SCRIPT).parent),
            stdout=fh, stderr=subprocess.STDOUT, env=env, text=True,
        )

    def _drain(self, rec: VoiceRecord, pos: int) -> int:
        """从上次位置读到底，返回新位置。

        ⚠️ 必须用 `readline()` 循环，**不能**用 `for line in f:` ——
        文件迭代器会做缓冲预读，之后再调 `tell()` 会抛
        `OSError: telling position disabled by next() call`（已踩过）。
        """
        if not rec.log_path.exists():
            return pos
        with open(rec.log_path, encoding="utf-8", errors="replace") as f:
            f.seek(pos)
            while True:
                line = f.readline()
                if not line:
                    break
                self._digest(rec, line)
            return f.tell()

    async def _watch(self, rec: VoiceRecord, *, phase: str) -> None:
        """盯着子进程，边读日志边更新进度。"""
        pos = getattr(rec, "_log_offset", 0)
        try:
            while True:
                pos = self._drain(rec, pos)
                if rec.proc is None:
                    break
                rc = rec.proc.poll()
                if rc is not None:
                    pos = self._drain(rec, pos)   # 收尾：把剩余输出读完再判定
                    self._finish(rec, rc, phase)
                    break
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("监视训练进程出错")
            rec.status = VoiceStatus.FAILED
            rec.error = f"{type(e).__name__}: {e}"
            self._persist(rec)

    def _digest(self, rec: VoiceRecord, line: str) -> None:
        """从子进程输出里解析阶段与进度。"""
        m = re.search(r"\[(\d{2}:\d{2}:\d{2})\]\s+(.*)", line)
        text = m.group(2) if m else line
        for marker, prog in _STAGE_PROGRESS:
            if marker in text:
                if prog > rec.progress:
                    rec.progress = prog
                rec.stage = _STAGE_LABEL.get(marker, rec.stage)
                if rec.status == VoiceStatus.ASR and "标注" in text:
                    rec.segments = _parse_segments(text)
                # auto_train 时子进程一口气跑完全流程，中途状态得从 asr 切到 training，
                # 否则整个训练期对外都显示「切片+识别中」，看起来像卡住了。
                if rec.status == VoiceStatus.ASR and prog >= 0.46:
                    rec.status = VoiceStatus.TRAINING
                break
        if "已在 ASR 后停下" in text:
            rec.status = VoiceStatus.REVIEWING
            rec.stage = "等待人工校对标注"
            rec.progress = max(rec.progress, 0.25)
        if '"segments":' in line:
            n = _parse_segments(line)
            if n:
                rec.segments = n

    def _finish(self, rec: VoiceRecord, rc: int, phase: str) -> None:
        rec.finished_at = time.time()
        if rc == 0:
            if phase == "asr":
                rec.status = VoiceStatus.REVIEWING
                rec.stage = "等待人工校对标注"
                rec.progress = max(rec.progress, 0.25)
            else:
                if (rec.model_dir / "sovits.pth").exists():
                    rec.status, rec.stage, rec.progress = VoiceStatus.DONE, "完成", 1.0
                else:
                    rec.status = VoiceStatus.FAILED
                    rec.error = "训练进程正常退出，但没有找到产物 sovits.pth"
        else:
            rec.status = VoiceStatus.FAILED
            rec.error = f"训练进程退出码 {rc}，详见日志 {rec.log_path}"
            tail = _tail(rec.log_path)
            if tail:
                rec.error += f"\n{tail}"
        self._persist(rec)
        logger.info("音色 %s 阶段 %s 结束：%s", rec.voice_id, phase, rec.status.value)

    def _persist(self, rec: VoiceRecord) -> None:
        state = {k: v for k, v in rec.__dict__.items()
                 if k not in ("proc",) and not k.startswith("_")}
        state["status"] = rec.status.value
        try:
            rec.work_dir.mkdir(parents=True, exist_ok=True)
            (rec.work_dir / "record.json").write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001
            logger.warning("写音色记录失败：%s", rec.voice_id, exc_info=True)


def _parse_segments(text: str) -> int:
    m = re.search(r"(\d+)\s*段|'segments':\s*(\d+)|\"segments\":\s*(\d+)", text)
    if not m:
        return 0
    return int(next(g for g in m.groups() if g))


def _tail(path: Path, n: int = 12) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:  # noqa: BLE001
        return ""
