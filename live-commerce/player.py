#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
直播播放器 —— 常驻 ffmpeg + FIFO 喂流

核心机制（见 FAQ弹幕匹配实现计划.md §2）：

    ffmpeg 启动一次就不再重启，输入是一条 FIFO。
    要切换内容时，**只是在字节流里换一个 TS 文件继续写**。
    TS 是流式容器，字节级首尾相接即可连续播放（已实测：拼接后时长零偏差）。

为什么不能 kill 掉 ffmpeg 再起一个新的：
    实测 ffmpeg 启动到产出首字节要 **1.8 秒**。每次插播都重启 = 每次约 2 秒黑屏，
    直播里肉眼可见，且可能触发平台断流保护。

用法：
    # 播主播放列表（本地文件，不推流，测试用）
    python player.py --profile xiaomei --main build/xiaomei/playlist.ts \\
        --output local:/tmp/live.flv --max-seconds 60

    # 接 RTMP（需换成有稳定带宽的机器，AutoDL 上行不达标）
    python player.py --profile xiaomei --main build/xiaomei/playlist.ts \\
        --output rtmp://<推流地址>

    # 弹幕从 stdin 进（另开一个终端）
    echo '{"text":"敏感肌能用吗"}' | python player.py --profile xiaomei ...

    # 无弹幕，纯跑主列表（自测）
    python player.py --profile xiaomei --main ... --no-danmaku
"""
import argparse
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from danmaku import parse_line
from matcher import FaqMatcher, DEFAULT_INDEX

HERE = Path(__file__).resolve().parent
FFMPEG = "/root/autodl-tmp/conda/envs/linly/bin/ffmpeg"
FIFO_PATH = Path("/tmp/linly_live.fifo")

# FIFO 写块大小。
#
# **必须是 188 的整数倍**——MPEG-TS 的包长固定 188 字节，每写完一块就检查一次
# 弹幕、可能在这里中断去插播。对齐包边界才能保证中断点不落在半个包里。
# 188×174 ≈ 32KB。按本机 640x1024 约 600KB/s 的码率，一块约 55ms 的内容，
# 即插播的响应粒度约 55ms——远低于人眼可感知的阈值。
TS_PACKET = 188
CHUNK = TS_PACKET * 174


class Player:
    def __init__(self, main: Path, output: str, matcher: FaqMatcher,
                 fifo: Path = FIFO_PATH, verbose: bool = True):
        self.main = Path(main)
        self.chunks: list[Path] = []
        self.chunk_idx = 0
        if self.main.is_dir():
            # 块模式（推荐）：主列表是按关键帧切好的 TS 目录。
            # 这样每次中断/插入/续播都落在 IDR 上，拼接零瑕疵。
            self.chunks = sorted(self.main.glob("*.ts"))
            if not self.chunks:
                raise SystemExit(f"目录里没有 .ts 文件：{self.main}")
            self.main_ts = self.chunks[0]
        else:
            # 单文件模式：能跑，但中断点可能落在 GOP 中间，拼接会有轻微瑕疵
            self.main_ts = self.main
        self.output = output
        self.matcher = matcher
        self.fifo = Path(fifo)
        self.verbose = verbose

        self.danmaku_q: queue.Queue = queue.Queue()
        self.running = True
        self.proc: subprocess.Popen | None = None
        self.fifo_fh = None
        self._pending_hit = None       # 喂流被弹幕打断时暂存命中的结果
        self._deadline: float | None = None
        self._main_offset = 0          # 单文件模式的续播位置

        self.stats = {"main_clips": 0, "inserts": 0, "restarts": 0,
                      "started_at": time.time()}

    # ── 日志 ──────────────────────────────────────────────────────

    def log(self, msg: str):
        if self.verbose:
            el = time.time() - self.stats["started_at"]
            print(f"[{el:7.1f}s] {msg}", flush=True)

    # ── ffmpeg ────────────────────────────────────────────────────

    def _output_args(self) -> list:
        if self.output.startswith("local:"):
            path = self.output.split(":", 1)[1]
            return ["-f", "flv", path]
        if self.output.startswith(("rtmp://", "rtmps://")):
            return ["-f", "flv", self.output]
        raise SystemExit(f"不支持的 output：{self.output}（用 local:/path 或 rtmp://...）")

    def _start_ffmpeg(self) -> subprocess.Popen:
        # -re 必须在 -i **之前**（它是输入选项）。少了它 ffmpeg 会全速读 FIFO，
        # 主列表每秒循环好几遍——实测踩过这个坑。
        #
        # -f mpegts 显式指定：FIFO 不可 seek，让 ffmpeg 自己去探测格式既慢又易错。
        #
        # ⚠️ **不要加 +genpts**。TS 里的时间戳本来就是有效的，重新生成反而和它们
        #    冲突，实测会产生 46 条/30秒的 "non monotonically increasing dts" 警告。
        #    只留 +igndts（忽略拼接点的时间戳回退），实测这一项就能把警告降到 0 条。
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error",
               "-fflags", "+igndts",
               "-re", "-f", "mpegts", "-i", str(self.fifo),
               "-c", "copy", "-flvflags", "no_duration_filesize",
               *self._output_args()]
        self.log(f"启动 ffmpeg：{' '.join(cmd[6:])}")
        return subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL)

    def _ensure_ffmpeg(self):
        """ffmpeg 挂了就重启。重启会丢几秒画面，但比整场停播好。"""
        if self.proc and self.proc.poll() is None:
            return
        if self.proc is not None:
            self.stats["restarts"] += 1
            self.log(f"⚠️  ffmpeg 已退出(code={self.proc.returncode})，重启"
                     f"（第 {self.stats['restarts']} 次）")
        self.proc = self._start_ffmpeg()

    # ── FIFO 与喂流 ───────────────────────────────────────────────

    def _open_fifo(self):
        """
        以二进制写方式打开 FIFO。

        ⚠️ 这一步会**阻塞**直到有读者（ffmpeg）打开它。
        所以必须先启动 ffmpeg 再调本方法，顺序反了会死锁。
        """
        self.log("等待 ffmpeg 打开 FIFO…")
        self.fifo_fh = open(self.fifo, "wb", buffering=0)
        self.log("FIFO 已连通")

    def _pending_match(self):
        """
        非阻塞地取一条弹幕并尝试匹配。命中则返回 MatchResult。

        这是「亚秒级插播」的关键：它在**喂流的过程中**被调用，而不是等一个片段播完。
        第一版把整条 255 秒的主列表一次性写完才返回，导致这 255 秒内弹幕根本没机会
        被检查——插播完全失效。实测踩到了。
        """
        while True:
            try:
                dm = self.danmaku_q.get_nowait()
            except queue.Empty:
                return None
            r = self.matcher.match(dm.text)
            if r:
                self.log(f"💬 命中「{dm.text}」→ {r.entry_id}（{r.question}，得分 {r.score}）")
                return r
            self.log(f"… 未命中「{dm.text}」")

    def _feed(self, ts: Path, label: str, start_offset: int = 0,
              interruptible: bool = True, check_danmaku: bool = True) -> tuple[str, int]:
        """
        分块把 TS 写进 FIFO。返回 (状态, 结束偏移)。

        状态：
          'done'    写完整段
          'interrupt' 被弹幕打断（命中已存入 self._pending_hit），偏移可用于续播
          'timeout' 到时间上限
          'error'   片段不存在 / FIFO 断开

        interruptible=False 用于插播的 FAQ 片段——**插播过程中不再打断自己**，
        否则连续弹幕会让 FAQ 永远播不完。
        """
        ts = Path(ts)
        if not ts.is_file():
            self.log(f"⚠️  片段不存在：{ts}（跳过）")
            return "error", start_offset
        size = ts.stat().st_size
        pos = start_offset
        try:
            with open(ts, "rb") as f:
                if start_offset:
                    f.seek(start_offset)
                while self.running:
                    if interruptible and self._deadline and time.time() >= self._deadline:
                        return "timeout", pos
                    buf = f.read(CHUNK)
                    if not buf:
                        return "done", size
                    self.fifo_fh.write(buf)
                    pos += len(buf)
                    if interruptible and check_danmaku:
                        hit = self._pending_match()
                        if hit:
                            self._pending_hit = hit
                            return "interrupt", pos
                return "done", pos
        except (BrokenPipeError, OSError) as e:
            self.log(f"⚠️  FIFO 写入中断（{type(e).__name__}），ffmpeg 可能已退出")
            return "error", pos

    # ── 弹幕线程 ──────────────────────────────────────────────────

    def _danmaku_reader(self):
        for line in sys.stdin:
            if not self.running:
                break
            dm = parse_line(line)
            if dm is None:
                continue
            if dm.dropped:
                self.log(f"🚫 丢弃弹幕「{dm.raw[:24]}」：{dm.dropped}")
                continue
            self.danmaku_q.put(dm)

    # ── 主循环 ────────────────────────────────────────────────────

    def run(self, max_seconds: float = 0):
        if self.fifo.exists():
            self.fifo.unlink()
        os.mkfifo(self.fifo)

        self._ensure_ffmpeg()
        self._open_fifo()

        def on_sigint(sig, frm):
            self.log("收到中断信号，收尾…")
            self.running = False
        signal.signal(signal.SIGINT, on_sigint)
        signal.signal(signal.SIGTERM, on_sigint)

        mode = f"块模式({len(self.chunks)} 块)" if self.chunks else "单文件模式"
        self.log(f"开始播放  主列表={self.main.name} {mode}"
                 f"  输出={self.output}"
                 + (f"  上限 {max_seconds:.0f}s" if max_seconds else ""))

        if not self.main_ts.is_file():
            raise SystemExit(f"主列表不存在：{self.main_ts}")

        t0 = time.time()
        self._deadline = (t0 + max_seconds) if max_seconds else None
        total = self.main_ts.stat().st_size

        while self.running:
            self._ensure_ffmpeg()          # 挂了就拉起来

            # ① 插播：优先处理上一轮喂流时被打断暂存的命中
            if self._pending_hit is not None:
                hit, self._pending_hit = self._pending_hit, None
                self._insert(hit)
                continue

            # ② 取下一块主内容
            if self.chunks:
                cur = self.chunks[self.chunk_idx]
                self.chunk_idx += 1
                if self.chunk_idx >= len(self.chunks):
                    self.chunk_idx = 0
                    self.stats["main_clips"] += 1
                    self.log(f"   ↳ 主列表播完一轮（已播 {self.stats['main_clips']} 遍）")
                start = 0
            else:
                if self._main_offset >= total:
                    self._main_offset = 0
                    self.stats["main_clips"] += 1
                    self.log(f"   ↳ 主列表播完一轮（已播 {self.stats['main_clips']} 遍）")
                cur, start = self.main_ts, self._main_offset

            state, pos = self._feed(cur, cur.name, start_offset=start)

            if state == "done":
                if not self.chunks:
                    self._main_offset = total
            elif state == "interrupt":
                # 块模式下直接丢弃当前块余下内容，下一块从 IDR 开始 → 拼接干净
                if not self.chunks:
                    self._main_offset = pos
                if self._pending_hit is not None:
                    hit, self._pending_hit = self._pending_hit, None
                    self._insert(hit)
            elif state == "timeout":
                self.log(f"到达时间上限 {max_seconds:.0f}s，停止")
                break
            else:                                   # error
                time.sleep(1)

        self.shutdown()

    def _insert(self, hit):
        """插播一条 FAQ 片段。插播期间不再被打断，否则连续弹幕会让它永远播不完。"""
        if not hit.ts:
            self.log(f"   ↳ {hit.entry_id} 无 TS 片段，跳过插播")
            return
        clip = Path(self.matcher.index_path).parent / hit.ts
        self.stats["inserts"] += 1
        t0 = time.time()
        state, _ = self._feed(clip, f"FAQ {hit.entry_id}",
                              interruptible=False, check_danmaku=False)
        self.log(f"   ↳ 插播 {hit.entry_id} 完成（{time.time()-t0:.1f}s，"
                 f"累计 {self.stats['inserts']} 次）")

    def shutdown(self):
        self.running = False
        try:
            if self.fifo_fh:
                self.fifo_fh.close()
        except Exception:
            pass
        if self.proc and self.proc.poll() is None:
            self.log("关闭 ffmpeg…")
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.matcher.save_state()
        try:
            if self.fifo.exists():
                self.fifo.unlink()
        except OSError:
            pass
        el = time.time() - self.stats["started_at"]
        self.log(f"结束。运行 {el/60:.1f} 分钟  主列表 {self.stats['main_clips']} 遍"
                 f"  插播 {self.stats['inserts']} 次"
                 + (f"  ffmpeg 重启 {self.stats['restarts']} 次"
                    if self.stats["restarts"] else ""))


def main():
    ap = argparse.ArgumentParser(description="直播播放器（常驻 ffmpeg + FIFO）")
    ap.add_argument("--profile", help="数字人档案 id（仅用于提示，不影响播放）")
    ap.add_argument("--main", required=True,
                    help="主播放列表：**按关键帧切好的 TS 目录**（推荐，拼接零瑕疵），"
                         "或单个 TS 文件（能跑，但中断点可能落在 GOP 中间）")
    ap.add_argument("--output", required=True,
                    help="local:/path/out.flv 或 rtmp://推流地址")
    ap.add_argument("--index", default=str(DEFAULT_INDEX))
    ap.add_argument("--min-weak-score", type=int, default=6,
                    help="中/弱关键词命中的累计分阈值（须与 matcher.py 保持一致）")
    ap.add_argument("--min-interval", type=float, default=30.0)
    ap.add_argument("--max-seconds", type=float, default=0, help="到点自动停（测试用）")
    ap.add_argument("--no-danmaku", action="store_true", help="不读 stdin，纯跑主列表")
    ap.add_argument("--reset-cooldown", action="store_true",
                    help="忽略索引里持久化的冷却状态（调试用）")
    ap.add_argument("--fifo", default=str(FIFO_PATH))
    a = ap.parse_args()

    m = FaqMatcher(a.index, a.min_weak_score, a.min_interval)
    if a.reset_cooldown:
        for e in m.entries.values():
            e["last_played_at"] = None
        m.last_insert_at = 0
        print("（已重置冷却状态）")
    print(f"匹配器：{m.summary()}")
    print(f"  profile={m.profile_id}  弱阈值={a.min_weak_score}  全局间隔={a.min_interval}s")

    p = Player(Path(a.main), a.output, m, fifo=Path(a.fifo))

    if not a.no_danmaku:
        t = threading.Thread(target=p._danmaku_reader, daemon=True)
        t.start()
        print("弹幕输入：stdin（每行一条；JSON 或纯文本都可）")
    else:
        print("弹幕输入：已关闭（--no-danmaku）")

    try:
        p.run(max_seconds=a.max_seconds)
    except KeyboardInterrupt:
        p.shutdown()


if __name__ == "__main__":
    main()
