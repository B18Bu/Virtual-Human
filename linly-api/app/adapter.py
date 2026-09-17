"""Linly-Talker 适配层 —— 唯一与项目内部实现耦合的模块。

设计意图：把「怎么调用 Linly-Talker」收敛到这一个文件，HTTP 层 / 队列 / 鉴权 /
文件下发都不依赖项目目录结构。这样即使项目源码更新、函数签名变化，改动面也只有这里。

三个必须遵守的项目侧约束（都已核对源码，改动前先读交接文档 5.4 / 5.5）：

1. **import 即加载权重**：`import VITS` 会在模块顶层加载 chinese-roberta + chinese-hubert
   并 `.half().to(device)`（VITS/GPT_SoVITS.py:53-65）；`import TFG` 会在模块顶层初始化
   mmpose DWPose 与人脸检测（Musetalk/musetalk/utils/preprocessing.py:15-23）。
   而且两个包的导入都被 try/except 包着，**失败只打印一行中文提示就继续**。
   → 因此本模块**顶层绝不 import 项目模块**，一律推迟到 `load()` 之后的按需加载。
2. **必须以仓库根为 CWD**：项目里全是相对路径（`./Musetalk/models/...`、`checkpoints/...`），
   并靠 `sys.path.append('CosyVoice/')` 这类相对插入。
   → `load()` 里 `os.chdir(PROJECT_DIR)`。
3. **不要复用 webui.py 的 `TTS_response` / `Talker_response_img`**：它们依赖 webui.py 在
   `__main__` 里才创建的模块全局，且内部大量调用 `gr.Warning`（无 Gradio 环境会报错）。
   → 直接调用各引擎类。
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Callable

from . import config, storage

logger = logging.getLogger(__name__)

ProgressCb = Callable[[float, str], None]
CancelCb = Callable[[], bool]

# ---------------------------------------------------------------- 常量
# 项目里写死的权重路径（相对仓库根）。载荷已逐个核对存在。
GPT_SOVITS_GPT = "GPT_SoVITS/pretrained_models/s1bert25hz-2kh-longer-epoch=68e-step=50232.ckpt"
GPT_SOVITS_SOVITS = "GPT_SoVITS/pretrained_models/s2G488k.pth"
COSYVOICE_SFT_DIR = "checkpoints/CosyVoice_ckpt/CosyVoice-300M-SFT"
DEFAULT_EDGE_VOICE = "zh-CN-XiaoxiaoNeural"
DEFAULT_COSYVOICE_SPK = "中文女"
# 微调音色的存放根目录（由 voice-train/pipeline.py 训练产出，**刻意在项目仓库之外**）。
# ⚠️ 该路径不能含 "pretrained" 子串 —— 见下面 _gpt_sovits 里的注释。
VOICES_ROOT = Path("/root/autodl-tmp/voices")
# GPT-SoVITS 的语言参数用**中文名**当键（GPT_SoVITS.py:398 的 dict_language），
# 传语言码 "zh" 会直接 KeyError。可选项就是下面这六个。
GPT_SOVITS_LANGS = ("中文", "英文", "日文", "中英混合", "日英混合", "多语种混合")
DEFAULT_GPT_SOVITS_LANG = "中文"

# 口型驱动：本服务收的是**图片**，SadTalker 是图片原生路径；MuseTalk 原生吃视频，
# 需要先把图片合成一段静态视频（见 _image_to_video）。
IMAGE_NATIVE_HEAD = "sadtalker"
VIDEO_NATIVE_HEAD = "musetalk"
# Wav2Lip：部署提示词把它列为低显存兜底（< 6G 时只跑它 + EdgeTTS），
# 权重与封装本项目都自带（checkpoints/wav2lip.pth、TFG/Wav2Lip.py），属图片原生。
WAV2LIP_HEAD = "wav2lip"
SUPPORTED_HEADS = (IMAGE_NATIVE_HEAD, VIDEO_NATIVE_HEAD, WAV2LIP_HEAD)

# src/utils/videoio.py 的 save_video_with_watermark 用 uuid4() 命名临时文件，
# 且以裸文件名调用 ffmpeg（落在 CWD，即仓库根），需要按此模式精确回收。
_STRAY_TMP = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.(mp4|wav)$"
)


def _rmtree_quietly(path: Path) -> None:
    """删除目录，失败不抛（清理路径不该影响主流程）。"""
    try:
        if path.is_dir():
            shutil.rmtree(path)
    except OSError:
        pass


def _noop_progress(*_args: object, **_kwargs: object) -> None:
    """替换项目里 `gr.Progress(...)` 的默认回调。

    项目多个方法把 `progress=gr.Progress(track_tqdm=True)` 写成默认参数，在没有 Gradio
    请求上下文的进程里调用它们并不安全；统一传这个空实现，进度我们自己上报。
    """
    return None


class BasePipeline:
    """所有适配实现的公共接口。"""

    name = "base"

    def load(self) -> None:
        """加载模型。可能在启动时调用，也可能在首个任务时惰性调用。"""

    def is_ready(self) -> bool:
        return False

    def generate(
        self,
        *,
        image_path: Path,
        text: str,
        mode: str,
        voice: str | None,
        progress_cb: ProgressCb,
        should_cancel: CancelCb,
    ) -> Path:
        """执行一次「人像 + 文本 → 口播视频」，返回产物路径。"""
        raise NotImplementedError

    def describe(self) -> dict:
        return {"impl": self.name, "ready": self.is_ready()}


# ---------------------------------------------------------------------------
# 真实实现
# ---------------------------------------------------------------------------
class RealPipeline(BasePipeline):
    """调用 Linly-Talker 的真实模型（文本 → 语音 → 口型驱动）。

    `load()` 很轻（chdir + sys.path + 环境整备），真正的权重在首次用到时按引擎惰性加载：
    · TTS：edge（无需权重）/ cosyvoice / gpt-sovits
    · 口型：sadtalker / musetalk

    `voice` 参数约定（prefix 语法，便于在一个字符串里表达引擎 + 参数）：

    | 取值 | 含义 |
    | --- | --- |
    | 留空 / `default` | Edge-TTS + 默认中文女声 |
    | `edge:zh-CN-YunxiNeural`，或直接给 `zh-CN-YunxiNeural` | Edge-TTS 指定音色 |
    | `cosyvoice` / `cosyvoice:中文女` | CosyVoice SFT 模式（内置说话人，无需参考音频） |
    | `gptsovits:<参考音频路径>|<参考文本>` | GPT-SoVITS 声音克隆；参考音频需 3~10 秒，路径为服务器本地路径 |

    产物：由队列统一改名为 `<task_id>.mp4` 落到 outputs 目录，本类只负责返回路径。
    """

    name = "linly-talker"

    def __init__(self) -> None:
        self._ready = False
        self._project_root = config.PROJECT_DIR
        self._work_root = config.OUTPUT_DIR / "work"
        self._tts_cache: dict[str, object] = {}
        self._talker_cache: dict[str, object] = {}

    # ------------------------------------------------------------ 生命周期
    def load(self) -> None:
        """只做「让项目模块可被正确导入/调用」的准备，不加载任何权重。"""
        root = self._project_root
        if not (root / "webui.py").is_file():
            raise RuntimeError(f"未找到 Linly-Talker 项目（缺 webui.py）：{root}")

        # 约束 2：项目内全是相对路径，必须以仓库根为 CWD。
        os.chdir(root)
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))

        self._sanitise_thread_env()
        self._ensure_bin_on_path()
        # TFG/MuseTalk.py 会 import gradio，关掉它启动时的联网打点，避免无谓等待。
        os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")

        self._work_root.mkdir(parents=True, exist_ok=True)
        self._ready = True
        logger.info("RealPipeline 就绪：CWD=%s（权重按引擎惰性加载）", root)

    def is_ready(self) -> bool:
        return self._ready

    def describe(self) -> dict:
        return {
            "impl": self.name,
            "ready": self._ready,
            "project_dir": str(self._project_root),
            "tts_loaded": sorted(self._tts_cache),
            "talker_loaded": sorted(self._talker_cache),
            "auto_mode": self._auto_mode(),
        }

    # ------------------------------------------------------------ 主流程
    def generate(
        self,
        *,
        image_path: Path,
        text: str,
        mode: str,
        voice: str | None,
        progress_cb: ProgressCb,
        should_cancel: CancelCb,
    ) -> Path:
        if not self._ready:
            raise RuntimeError("RealPipeline 尚未 load()，无法生成")
        if not image_path.is_file():
            raise FileNotFoundError(f"人像图片不存在：{image_path}")

        head = self._resolve_head(mode)
        self._cleanup_previous()

        workdir = self._work_root / uuid.uuid4().hex[:12]
        workdir.mkdir(parents=True, exist_ok=True)

        def checkpoint(stage: str) -> None:
            if should_cancel():
                raise RuntimeError(f"任务已被取消（{stage}）")

        try:
            checkpoint("准备")
            progress_cb(0.02, f"准备（{head}）")

            # ① 文本 → 语音
            engine, raw_audio = self._synthesize(text, voice, workdir, progress_cb)

            checkpoint("语音合成后")
            # ② 统一成 16k 单声道 wav：Edge-TTS 写出的其实是 MP3 字节（扩展名不可信），
            #    而 SadTalker 内部用 librosa 读音频，格式不统一会直接抛错。
            audio = self._to_wav(raw_audio, workdir / "speech.wav")
            progress_cb(0.42, f"语音就绪（{engine}）")

            # ③ 人像 + 语音 → 口播视频
            if head == IMAGE_NATIVE_HEAD:
                video = self._run_sadtalker(image_path, audio, workdir, progress_cb)
            elif head == WAV2LIP_HEAD:
                video = self._run_wav2lip(image_path, audio, workdir, progress_cb)
            else:
                video = self._run_musetalk(image_path, audio, workdir, progress_cb)

            checkpoint("口型驱动后")
            progress_cb(1.0, "完成")
            logger.info("生成完成：head=%s engine=%s -> %s", head, engine, video)
            return video
        except Exception:
            # 失败时把本次中间产物留在原地会被下一次任务的 _cleanup_previous 回收，
            # 但这里顺手清一次，避免连续失败时磁盘堆积。
            _rmtree_quietly(workdir)
            raise

    # ------------------------------------------------------------ TTS
    def _synthesize(
        self, text: str, voice: str | None, workdir: Path, progress_cb: ProgressCb
    ) -> tuple[str, Path]:
        """文本 → 音频文件（可能是 mp3/wav，交给 _to_wav 统一）。"""
        engine, param = self._parse_voice(voice)

        if engine == "edge":
            progress_cb(0.06, "语音合成（Edge-TTS）")
            out = workdir / "tts.mp3"
            tts = self._edge_tts()
            # ⚠️ VOLUME 必须传 100，不能传 0。
            # `TTS/EdgeTTS.py:146` 的 preprocess 是 `volume = 100 - volume`
            # 再拼成 `-{volume}%` —— 也就是说这个参数是**反的**：
            #   VOLUME=100 → "-0%"  → 满音量（UI 滑块默认值就是 100）
            #   VOLUME=0   → "-100%" → **静音**
            # 传 0 会得到一条长度正常、格式合法、内容全零的音轨，极难发现：
            # ffprobe 照样报 aac / 16kHz / 时长吻合，只有量波形才看得出来。
            # （项目自己的 EdgeTTS.test() 也写的 0，所以照抄它一样会中招。）
            # RATE=0 → "+0%"、PITCH=0 → "+0Hz"，这两个传 0 才是真的无变化，保持。
            tts.predict(text, str(param), 0, 100, 0, str(out), str(workdir / "tts.vtt"))
            progress_cb(0.30, "语音合成完成")
            return "edge-tts", out

        if engine == "cosyvoice":
            progress_cb(0.06, f"语音合成（CosyVoice SFT / {param}）")
            out = workdir / "tts.wav"
            self._cosyvoice().predict_sft(text, str(param), save_path=str(out))
            progress_cb(0.30, "语音合成完成")
            return f"cosyvoice:{param}", out

        if engine == "gptsovits":
            ref_wav, prompt_text, lang = param  # type: ignore[misc]
            if not Path(ref_wav).is_file():
                raise FileNotFoundError(f"GPT-SoVITS 参考音频不存在（需服务器本地路径）：{ref_wav}")
            progress_cb(0.06, "语音合成（GPT-SoVITS 克隆）")
            out = workdir / "tts.wav"
            # 参考音频需 3~10 秒，否则 get_tts_wav 内部会抛 OSError。
            # 语言参数必须传**中文名**（"中文"），不能传语言码（"zh"）：
            # GPT_SoVITS.py:398 的 dict_language 是拿中文名当键的，传 "zh" 会 KeyError。
            self._gpt_sovits().predict(
                ref_wav, prompt_text, lang, text, lang, "不切", save_path=str(out)
            )
            progress_cb(0.30, "语音合成完成")
            return f"gpt-sovits:{lang}", out

        if engine == "finetuned":
            voice_id, ref_wav, prompt_text, lang = param  # type: ignore[misc]
            if not Path(ref_wav).is_file():
                raise FileNotFoundError(
                    f"微调音色的参考音频不存在（需服务器本地路径）：{ref_wav}")
            progress_cb(0.06, f"语音合成（微调音色 {voice_id}）")
            out = workdir / "tts.wav"
            # 与 gptsovits 分支同理：参考音频 3~10 秒，语言必须传中文名。
            # 区别只在于用哪个模型实例（微调权重 + 同一套底座 BERT/HuBERT）。
            self._gpt_sovits(voice_id).predict(
                ref_wav, prompt_text, lang, text, lang, "不切", save_path=str(out))
            progress_cb(0.30, "语音合成完成")
            return f"finetuned:{voice_id}", out

        raise ValueError(f"无法识别的 voice：{voice!r}")  # 兜底，_parse_voice 已校验

    @staticmethod
    def _parse_voice(voice: str | None) -> tuple[str, object]:
        """把 voice 字符串解析成 (引擎, 参数)。"""
        raw = (voice or "").strip()
        if not raw or raw.lower() in ("default", "auto"):
            return "edge", DEFAULT_EDGE_VOICE

        head, _, tail = raw.partition(":")
        lowered = head.lower()

        if lowered in ("edge", "edge-tts", "edgetts"):
            return "edge", (tail or DEFAULT_EDGE_VOICE)
        if lowered in ("cosyvoice", "cosyvoice-sft"):
            return "cosyvoice", (tail or DEFAULT_COSYVOICE_SPK)
        if lowered in ("gptsovits", "gpt-sovits", "vits"):
            ref_wav, _, rest = tail.partition("|")
            prompt_text, _, lang = rest.partition("|")
            if not ref_wav:
                raise ValueError(
                    "voice 需形如 gptsovits:<参考音频路径>|<参考文本>[|<语言>]"
                )
            # 语言必须用中文名，理由见 _synthesize 里的注释
            lang = (lang or DEFAULT_GPT_SOVITS_LANG).strip()
            if lang not in GPT_SOVITS_LANGS:
                raise ValueError(
                    f"GPT-SoVITS 语言取值非法：{lang!r}（可选 {'/'.join(GPT_SOVITS_LANGS)}）"
                )
            return "gptsovits", (ref_wav, prompt_text, lang)

        # 微调音色：finetuned:<音色名>|<参考音频>|<参考文本>[|<语言>]
        # 刻意新开一个 head 而不是往 gptsovits: 的语法里插字段 —— 后者已经按 "|"
        # 分割，插进去会破坏所有既有调用方。这个 head 是纯新增，零回归。
        if lowered in ("finetuned", "finetune", "custom", "voice"):
            voice_id, _, rest = tail.partition("|")
            ref_wav, _, rest2 = rest.partition("|")
            prompt_text, _, lang = rest2.partition("|")
            if not (voice_id and ref_wav and prompt_text):
                raise ValueError(
                    "voice 需形如 finetuned:<音色名>|<参考音频>|<参考文本>[|<语言>]"
                )
            lang = (lang or DEFAULT_GPT_SOVITS_LANG).strip()
            if lang not in GPT_SOVITS_LANGS:
                raise ValueError(
                    f"GPT-SoVITS 语言取值非法：{lang!r}（可选 {'/'.join(GPT_SOVITS_LANGS)}）"
                )
            return "finetuned", (voice_id, ref_wav, prompt_text, lang)

        # 便利写法：直接把 Edge 音色名丢进来（zh-CN-XiaoxiaoNeural 这类）。
        if "Neural" in raw or re.match(r"^[a-z]{2}-[A-Z]{2}-", raw):
            return "edge", raw

        raise ValueError(
            f"无法识别的 voice：{voice!r}；"
            "可用：留空/edge[:音色] / cosyvoice[:说话人] / "
            "gptsovits:<参考音频>|<参考文本>[|<语言>] / "
            "finetuned:<音色名>|<参考音频>|<参考文本>[|<语言>]"
        )

    # ------------------------------------------------------------ 口型驱动
    @staticmethod
    def _sadtalker_preprocess() -> str:
        """SadTalker 的预处理模式，默认 `full`（用 LINLY_SADTALKER_PREPROCESS 覆盖）。

        这个参数决定成片的构图与体量，不是可有可无的调优项：

        · `crop`：只把检出的人脸裁出来做动画，返回 **256x256 的纯人脸视频**——
          `src/facerender/animate.py:229` 的 `if 'full' in preprocess.lower()` 不成立，
          既不调 `paste_pic` 回贴原图，`return_path` 就是那个临时人脸视频。
          256x256@20fps 经 h264 CRF23 只有约 120 kbps，4.3 秒的片子约 63KB，
          达不到部署提示词第 5 条「mp4 文件，且文件大于 100KB」（实测 64254 字节）。
        · `full`：把动画后的人脸 `seamlessClone` 回**原图**再返回，640x1024 的源图
          就得到 640x1024 的成片——像素量约 `crop` 的 10 倍，且这才是口播视频该有的构图。

        另注意 `src/utils/init_path.py:18-22` 会据此换权重：`full` 走
        `mapping_00109-model.pth.tar` + `facerender_still.yaml`（coeff_nc=73），
        `crop` 走 `mapping_00229-model.pth.tar` + `facerender.yaml`（coeff_nc=70）。
        两套文件都已在位，切过去不需要额外下载。
        """
        mode = os.getenv("LINLY_SADTALKER_PREPROCESS", "full").strip().lower() or "full"
        if mode not in ("crop", "full", "extcrop", "extfull"):
            # 静默放行的话，`init_path` 与 `preprocess.py` 会各自按子串猜，落到未预期的分支。
            raise ValueError(
                f"LINLY_SADTALKER_PREPROCESS 取值非法：{mode!r}"
                "（可选 crop / full / extcrop / extfull）"
            )
        return mode

    def _run_sadtalker(
        self, image: Path, audio: Path, workdir: Path, progress_cb: ProgressCb
    ) -> Path:
        talker = self._talker(IMAGE_NATIVE_HEAD)
        progress_cb(0.45, "口型驱动（SadTalker）")
        result = talker.test2(
            source_image=str(image),
            driven_audio=str(audio),
            preprocess=self._sadtalker_preprocess(),
            still_mode=self._env_flag("LINLY_SADTALKER_STILL", False),
            use_enhancer=self._env_flag("LINLY_ENHANCER", False),
            batch_size=1,
            size=256,
            fps=20,
            # 产物落到服务自己的 work 目录，避免往仓库里写。
            result_dir=str(workdir),
        )
        if not result or not Path(result).is_file():
            raise RuntimeError(f"SadTalker 未产出视频：{result!r}")
        progress_cb(0.95, "口型驱动完成")
        return Path(result)

    def _run_wav2lip(
        self, image: Path, audio: Path, workdir: Path, progress_cb: ProgressCb
    ) -> Path:
        """Wav2Lip：只重绘嘴部区域，其余画面原样保留；显存占用最低，是低显存档的兜底。

        注意两个项目侧的实现细节（源码不能改，只能绕着走）：
        · `TFG/Wav2Lip.py:169` 的返回值是**写死的** `'results/example_answer.mp4'`，
          不接受输出目录 → 调完必须自己搬走，否则产物留在仓库里；
        · 它还会往 `temp/` 写 `result.avi` / `temp.wav`（相对 cwd = 仓库根）。
          这两个目录都在 .gitignore 里，但仍要回收，见 _cleanup_previous。
        """
        talker = self._talker(WAV2LIP_HEAD)
        progress_cb(0.45, "口型驱动（Wav2Lip）")

        produced = talker.predict(
            str(image),
            str(audio),
            8,  # batch_size
            fps=25,
            enhance=self._env_flag("LINLY_ENHANCER", False),
        )
        src = Path(produced) if produced else None
        if not src or not src.is_file():
            raise RuntimeError(f"Wav2Lip 未产出视频：{produced!r}")

        dst = workdir / "wav2lip.mp4"
        shutil.move(str(src), str(dst))
        progress_cb(0.95, "口型驱动完成")
        return dst

    def _run_musetalk(
        self, image: Path, audio: Path, workdir: Path, progress_cb: ProgressCb
    ) -> Path:
        talker = self._talker(VIDEO_NATIVE_HEAD)

        progress_cb(0.45, "合成静态人像视频")
        # MuseTalk 原生吃视频；用图片合成一段约 2 秒的静态视频即可——
        # 推理时它会按音频长度循环取帧（inference_noprepare 里 idx % len(...)）。
        avatar_video = self._image_to_video(image, workdir / f"{workdir.name}-avatar.mp4")

        progress_cb(0.50, "提取关键点与人像素材")
        # prepare_material 会把素材写到相对路径 ./results/avatars/<视频名>/（仓库内、已被
        # .gitignore 覆盖），由 _cleanup_previous 在下一个任务开始时回收。
        talker.prepare_material(str(avatar_video), bbox_shift=0, progress=_noop_progress)

        progress_cb(0.70, "口型驱动（MuseTalk）")
        video = talker.inference_noprepare(
            str(audio), str(avatar_video), bbox_shift=0, fps=25, progress=_noop_progress
        )
        if not video or not Path(video).is_file():
            raise RuntimeError(f"MuseTalk 未产出视频：{video!r}")
        progress_cb(0.95, "口型驱动完成")
        return Path(video)

    def _resolve_head(self, mode: str) -> str:
        """把请求里的 mode 解析成实际使用的口型引擎。"""
        raw = (mode or "auto").strip().lower()
        if raw in SUPPORTED_HEADS:
            return raw
        if raw == "auto":
            return self._auto_mode()
        raise ValueError(
            f"未知 mode：{mode!r}（支持：auto/{'/'.join(SUPPORTED_HEADS)}）"
        )

    @staticmethod
    def _auto_mode() -> str:
        """auto 的默认选择：SadTalker。

        部署提示词按显存分档（>=11G 可启用 MuseTalk），但本服务入参是**图片**，
        SadTalker 才是图片原生路径；MuseTalk 需要先合成静态视频，多一层不确定性。
        因此 auto 默认走 SadTalker，需要 MuseTalk 时显式传 mode=musetalk，
        也可用环境变量 LINLY_AUTO_MODE 改默认。
        """
        return os.getenv("LINLY_AUTO_MODE", IMAGE_NATIVE_HEAD).strip().lower() or IMAGE_NATIVE_HEAD

    # ------------------------------------------------------------ 引擎装载（惰性）
    def _edge_tts(self):
        if "edge" not in self._tts_cache:
            logger.info("惰性加载 Edge-TTS（无需本地权重）")
            from TTS.EdgeTTS import EdgeTTS

            self._tts_cache["edge"] = EdgeTTS(list_voices=False)
        return self._tts_cache["edge"]

    def _cosyvoice(self):
        if "cosyvoice" not in self._tts_cache:
            # 注意：import VITS 包本身就会加载 BERT + HuBERT（约束 1），因此推迟到这里。
            logger.info("惰性加载 CosyVoice（首次会连带加载 GPT-SoVITS 的 BERT/HuBERT）")
            from VITS import CosyVoiceTTS

            self._tts_cache["cosyvoice"] = CosyVoiceTTS(COSYVOICE_SFT_DIR)
        return self._tts_cache["cosyvoice"]

    @staticmethod
    def _restore_gptsovits_utils_module() -> None:
        """把顶层 `utils` 模块纠正回 `GPT_SoVITS/utils.py`。

        `s2G488k.pth` 是 pickle 存的，里面按**模块名 `utils`** 引用 `HParams`，
        而 `HParams` 定义在 `GPT_SoVITS/utils.py`（上游就是这么存的，靠
        `VITS/GPT_SoVITS.py:9` 的 `sys.path.append('GPT_SoVITS/')` 找回来）。

        问题在于 `import TFG` 会往 sys.path 里塞 `./Musetalk` 及其子目录，
        `Musetalk/musetalk/utils/utils.py` 会**先**以顶层 `utils` 的身份进 sys.modules，
        于是 torch.load 报：
            Can't get attribute 'HParams' on <module 'utils' from
            '.../Musetalk/musetalk/utils/utils.py'>
        而 `sys.path.append` 是追加到末尾的，靠路径顺序赢不回来，只能显式按文件加载。
        """
        import importlib.util

        wrong = sys.modules.get("utils")
        if wrong is not None and "GPT_SoVITS" in getattr(wrong, "__file__", ""):
            return  # 已经是对的
        target = Path(os.getcwd()) / "GPT_SoVITS" / "utils.py"
        if not target.is_file():
            return
        spec = importlib.util.spec_from_file_location("utils", str(target))
        if spec is None or spec.loader is None:
            return
        module = importlib.util.module_from_spec(spec)
        sys.modules["utils"] = module
        spec.loader.exec_module(module)
        logger.info("已把顶层 utils 纠正为 %s（原为 %s）", target,
                    getattr(wrong, "__file__", "未加载"))

    @staticmethod
    def _finetuned_paths(voice_id: str) -> tuple[str, str]:
        """把音色名解析成 (gpt_ckpt, sovits_pth) 两个绝对路径。

        约定：每个音色一个目录，里面固定叫 `gpt.ckpt` 与 `sovits.pth`
        （训练完由 pipeline.py 的 register_voice 落成这个形状）。

        ⚠️ 这里**不能**把目录放到 `GPT_SoVITS/pretrained_models/` 下：
        `VITS/GPT_SoVITS.py:351` 有一句
            `if ("pretrained" not in sovits_path): del vq_model.enc_q`
        它是对**完整路径做子串匹配**。微调产物按上游约定本就不含 enc_q
        （process_ckpt.py 的 savee 里 `if "enc_q" in key: continue`），
        路径里带上 "pretrained" 会让判断走错分支，白留一份用不上的后验编码器。
        """
        if not re.fullmatch(r"[A-Za-z0-9_]{1,32}", voice_id or ""):
            raise ValueError(f"音色名非法：{voice_id!r}（只允许字母/数字/下划线）")
        d = VOICES_ROOT / voice_id
        gpt, sovits = d / "gpt.ckpt", d / "sovits.pth"
        if not gpt.is_file() or not sovits.is_file():
            raise FileNotFoundError(
                f"音色 {voice_id!r} 的权重不完整，期望 {gpt} 与 {sovits}。"
                f"可先调 POST /api/v1/voices/{voice_id} 查看训练状态。"
            )
        return str(gpt), str(sovits)

    def _gpt_sovits(self, voice_id: str | None = None):
        """取 GPT-SoVITS 实例。voice_id 为 None 用底座（零样本），否则用微调音色。

        按 voice_id 分档缓存（与 `_talker(head)` 同一形状）。注意每个音色会常驻一份
        s1+s2 权重（约 240MB 显存）；共享的 BERT + HuBERT 只有一份，不随音色增长。
        """
        key = f"gptsovits:{voice_id}" if voice_id else "gptsovits"
        if key not in self._tts_cache:
            from VITS import GPT_SoVITS

            if voice_id:
                gpt_path, sovits_path = self._finetuned_paths(voice_id)
                logger.info("惰性加载微调音色 %s（%s）", voice_id, gpt_path)
            else:
                gpt_path, sovits_path = GPT_SOVITS_GPT, GPT_SOVITS_SOVITS
                logger.info("惰性加载 GPT-SoVITS 底座（含 BERT + HuBERT + s1/s2 权重）")

            # ⚠️ 必须在**每次** load_model 之前跑，不能只在首次入缓存时做：
            # pickle 里引用的是顶层 `utils` 模块，而 `import TFG` 会把
            # Musetalk/musetalk/utils/utils.py 注册成顶层 utils，
            # 导致 torch.load 报 `Can't get attribute 'HParams'`。
            # 该函数幂等（已正确时提前返回），放进 load 路径是安全的。
            self._restore_gptsovits_utils_module()
            model = GPT_SoVITS()
            model.load_model(gpt_path, sovits_path)
            self._tts_cache[key] = model
        return self._tts_cache[key]

    def _talker(self, head: str):
        if head not in self._talker_cache:
            if head == IMAGE_NATIVE_HEAD:
                logger.info("惰性加载 SadTalker（构造函数即加载全部权重）")
                # 注意：导入包的子模块必先执行 TFG/__init__.py，而它会 import MuseTalk
                # → 连带加载 mmpose DWPose 与人脸检测（约 1G 显存）。也就是说即使只用
                # SadTalker，这一步也会把 DWPose 一起装进来，属预期内代价。
                from TFG.SadTalker import SadTalker

                self._talker_cache[head] = SadTalker(
                    checkpoint_path="checkpoints", config_path="src/config"
                )
            elif head == WAV2LIP_HEAD:
                logger.info("惰性加载 Wav2Lip（构造函数即加载权重）")
                # 同样要先执行 TFG/__init__.py，DWPose 那笔开销躲不掉。
                from TFG.Wav2Lip import Wav2Lip

                self._talker_cache[head] = Wav2Lip()
            else:
                logger.info("惰性加载 MuseTalk（先只建对象，init_model 在任务里调）")
                from TFG.MuseTalk import MuseTalk_RealTime

                talker = MuseTalk_RealTime()
                talker.init_model()  # 加载 whisper/vae/unet/pe
                self._talker_cache[head] = talker
        return self._talker_cache[head]

    # ------------------------------------------------------------ 工具
    @staticmethod
    def _sanitise_thread_env() -> None:
        """平台注入 `OMP_NUM_THREADS=0` 对 libgomp 是非法值；且 nproc 报 224 而 cgroup 只给 25 核。"""
        for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS"):
            raw = os.environ.get(key)
            if raw is None or not raw.isdigit() or int(raw) <= 0:
                os.environ[key] = "8"

    @staticmethod
    def _ensure_bin_on_path() -> None:
        """把解释器同级的 bin 目录前置到 PATH。

        项目里多处用 `subprocess.run(..., shell=True)` 调**裸命令名**（例如
        `src/utils/videoio.py:25` 的 ffmpeg、`TFG/MuseTalk.py` 里的 ffmpeg 合成），
        这些只认 PATH。若服务不是经 `conda activate linly` 启动（例如直接用
        `/root/.../envs/linly/bin/python` 跑），PATH 里就没有该目录，子进程会以
        退出码 127（command not found）失败——实测踩过。
        """
        env_bin = str(Path(sys.executable).parent)
        parts = os.environ.get("PATH", "").split(os.pathsep)
        if env_bin not in parts:
            os.environ["PATH"] = os.pathsep.join([env_bin, *parts])
        if not (Path(env_bin) / "ffmpeg").is_file():
            logger.warning("%s 下没有 ffmpeg，项目内的视频合成会失败", env_bin)

    @staticmethod
    def _env_flag(name: str, default: bool = False) -> bool:
        raw = os.environ.get(name)
        if raw is None:
            return default
        return raw.strip().lower() in ("1", "true", "yes", "on")

    @staticmethod
    def _ffmpeg() -> str:
        exe = shutil.which("ffmpeg")
        if exe is None:
            # 项目把 ffmpeg 装在 conda 环境内，PATH 未生效时按解释器同级目录兜底。
            candidate = Path(sys.executable).parent / "ffmpeg"
            if candidate.is_file():
                return str(candidate)
            raise RuntimeError("找不到 ffmpeg（Linly-Talker 的音频/视频合成都依赖它）")
        return exe

    def _to_wav(self, src: Path, dst: Path) -> Path:
        """统一转成 16kHz 单声道 wav。"""
        proc = subprocess.run(
            [self._ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
             "-i", str(src), "-ar", "16000", "-ac", "1", str(dst)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0 or not dst.is_file():
            raise RuntimeError(f"音频转 wav 失败：{proc.stderr.strip()[:200]}")
        return dst

    def _image_to_video(self, image: Path, dst: Path, *, fps: int = 25, seconds: float = 2.0) -> Path:
        """把静态图片合成一段短视频，供 MuseTalk 使用。

        `scale=trunc(iw/2)*2:trunc(ih/2)*2` 保证宽高为偶数——H.264 要求，
        而且 MuseTalk 的关键点/mask 处理也依赖可用的解码结果。
        """
        proc = subprocess.run(
            [self._ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
             "-loop", "1", "-i", str(image), "-t", str(seconds), "-r", str(fps),
             "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-pix_fmt", "yuv420p", str(dst)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0 or not dst.is_file():
            raise RuntimeError(f"图片转视频失败：{proc.stderr.strip()[:200]}")
        return dst

    def _cleanup_previous(self) -> None:
        """回收上一个任务的中间产物。

        单 worker 串行执行，且队列在上一个任务返回后已把产物搬去 outputs，
        所以这里可以安全地整目录清掉：
        · 服务自己的 work 目录；
        · 仓库内 results/avatars/*（MuseTalk 把素材写死在相对路径，会无限增长）。
        """
        for child in list(self._work_root.glob("*")):
            _rmtree_quietly(child)
        avatars = self._project_root / "results" / "avatars"
        if avatars.is_dir():
            for child in list(avatars.glob("*")):
                _rmtree_quietly(child)
        # Wav2Lip 把中间产物写死在 temp/（相对仓库根）：temp/result.avi、temp/temp.wav。
        # temp/ 同时是 Gradio 的 GRADIO_TEMP_DIR，所以只删这两个具名文件，不整目录清。
        for name in ("result.avi", "temp.wav"):
            try:
                (self._project_root / "temp" / name).unlink()
            except OSError:
                pass
        # src/utils/videoio.py 的 save_video_with_watermark 会先用裸文件名（uuid4().mp4）
        # 在当前工作目录（= 仓库根）里落一个临时文件，再 move 到目标位置。正常路径下它
        # 转瞬即逝，但进程若在中间崩掉就会在仓库根留下游离文件——这里按 uuid 命名精确回收，
        # 以免弄脏 git status（.gitignore 只忽略了 *.wav 和某个具体 mp4）。
        for stray in self._project_root.glob("*.mp4"):
            if _STRAY_TMP.match(stray.name):
                try:
                    stray.unlink()
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# 占位实现（仅验证链路，不产生真实视频）
# ---------------------------------------------------------------------------
class MockPipeline(BasePipeline):
    """不加载任何模型，睡眠数秒后写一个占位产物。

    用途：验证 鉴权 / 入队 / 串行调度 / 进度上报 / 文件下发 是否正常。
    产物文件名以 .mp4 结尾但内容是文本标记，**不是可播放视频**——
    健康检查会返回 pipeline_impl=mock，避免误判为已跑通。
    """

    name = "mock"

    def __init__(self, delay_seconds: float = 3.0) -> None:
        self._delay = delay_seconds
        self._ready = True

    def load(self) -> None:
        self._ready = True

    def is_ready(self) -> bool:
        return self._ready

    def generate(
        self,
        *,
        image_path: Path,
        text: str,
        mode: str,
        voice: str | None,
        progress_cb: ProgressCb,
        should_cancel: CancelCb,
    ) -> Path:
        stages = [
            (0.05, "加载人像"),
            (0.25, "文本预处理"),
            (0.50, "语音合成(TTS)"),
            (0.80, "口型驱动"),
            (0.95, "编码封装"),
        ]
        step = self._delay / len(stages)
        for fraction, stage in stages:
            if should_cancel():
                raise RuntimeError("任务已被取消")
            progress_cb(fraction, stage)
            time.sleep(step)

        # 队列会把产物统一改名为 <task_id>.mp4，这里先用临时名
        dest = storage.result_path(f"mock-{image_path.stem}", ".mp4")
        dest.write_text(
            "[MockPipeline 占位产物，非真实视频]\n"
            f"image={image_path}\n"
            f"text={text}\n"
            f"mode={mode}\n"
            f"voice={voice}\n",
            encoding="utf-8",
        )
        progress_cb(1.0, "完成")
        return dest


def build_pipeline() -> BasePipeline:
    """按项目就位情况选择实现。

    LINLY_PIPELINE=mock 可强制走占位实现（无卡环境下验证 HTTP 链路时用）。
    RealPipeline 的 load() 很轻（不加载权重），因此这里直接调用；
    失败时退回 Mock 并如实上报，避免服务起不来。
    """
    forced = os.getenv("LINLY_PIPELINE", "").strip().lower()
    if forced == "mock":
        logger.warning("LINLY_PIPELINE=mock：使用 MockPipeline（仅验证 HTTP 链路）")
        return MockPipeline()
    if forced not in ("", "real", "linly-talker"):
        logger.warning("未知的 LINLY_PIPELINE=%s，按 real 处理", forced)

    if not config.PROJECT_DIR.is_dir():
        logger.warning("项目目录 %s 不存在，使用 MockPipeline。", config.PROJECT_DIR)
        return MockPipeline()

    pipeline = RealPipeline()
    try:
        pipeline.load()
    except Exception as exc:  # noqa: BLE001 - 起服务优先，失败原因要如实暴露
        logger.exception("RealPipeline 初始化失败，退回 MockPipeline：%s", exc)
        return MockPipeline()
    return pipeline
