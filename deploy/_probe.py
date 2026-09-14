"""产物探测的公共实现，供 verify_b3.py / verify_c.py 共用。

单独放一个模块是因为「怎么判断一支 mp4 是真视频」这件事必须只有一处实现：
两个验收脚本各写一份的话，解析口径迟早会漂移（已经踩过一次——用
`default=nw=1` 手工分行时，`codec_name` 出现在 `codec_type` **之前**，
按后者分桶会把第一条流的编码名记到错误的位置）。
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

# 中心区域（画面正中 50% x 50%）相邻帧平均灰度差的下限，低于它即认为「画面没动」。
# 只看中心：人像在 640x1024 的画面里只占中间一小块，全幅均值会把口型与微表情
# 的变化稀释掉。
#
# 阈值来自实测（2026-09-14）而不是估计：
#   · 静帧基线 0.01——把产物首帧用同样参数（h264 / 20fps / 同分辨率同时长）编成
#     静帧视频再测，libx264 对完全相同的输入帧几乎输出相同的帧；
#   · 真实产出 1.31 与 2.56（两次不同文本/不同韵律的生成）。
# 取 0.5：高于静帧基线 50 倍，又低于实测最低的真视频 2.6 倍，两头都有余量。
MOTION_EPS = 0.5

# 音轨 RMS 下限（int16 满量程 32767）。**静音音轨的 RMS 是 0.0**，正常中文语音
# 大约在 1000~5000，所以这个阈值只是用来拦「完全没声音」，不做音质判断。
# 加这条是因为踩过：Edge-TTS 的 VOLUME 参数是反的（100-传入值），传 0 得到 -100% 静音，
# 而 ffprobe 对这种音轨完全无感，五道检查全过。
AUDIO_RMS_EPS = 50.0


def ffprobe_bin() -> str:
    """ffprobe 不在系统 PATH 里（装在 conda 环境内），按 PATH → 解释器同级 的顺序找。"""
    return shutil.which("ffprobe") or str(Path(sys.executable).parent / "ffprobe")


def ffmpeg_bin() -> str:
    return shutil.which("ffmpeg") or str(Path(sys.executable).parent / "ffmpeg")


def audio_rms(path: Path) -> tuple[float, int]:
    """返回音轨的（RMS, 峰值），按 int16 满量程 32767 计。

    **为什么必须量这个**：一条静音音轨在 ffprobe 眼里完全正常——编码是 aac、
    采样率对、声道对、时长吻合，只有解出 PCM 量波形才看得出来。曾经就因为只校验
    「存在音频流」，让一批「长度正常但完全无声」的产物通过了验收。
    """
    import numpy as np

    proc = subprocess.run(
        [ffmpeg_bin(), "-v", "error", "-i", str(path),
         "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
        capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        return 0.0, 0
    a = np.frombuffer(proc.stdout[: len(proc.stdout) // 2 * 2], dtype=np.int16)
    if a.size == 0:
        return 0.0, 0
    return float(a.std()), int(abs(a).max())


def probe(path: Path) -> dict:
    """返回 ffprobe 的 JSON（含 format 与 streams）。"""
    out = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    return json.loads(out.stdout or "{}")


def motion_report(path: Path) -> tuple[float, float, int]:
    """返回（中心区域相邻帧最大平均灰度差, 首末帧中心区域差, 帧数）。只解码不看音频。"""
    import cv2
    import numpy as np

    cap = cv2.VideoCapture(str(path))
    frames = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frames.append(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.int16))
    cap.release()
    if len(frames) < 2:
        return 0.0, 0.0, len(frames)

    h, w = frames[0].shape[:2]
    box = (slice(h // 4, 3 * h // 4), slice(w // 4, 3 * w // 4))
    diffs = [float(np.abs(a[box] - b[box]).mean()) for a, b in zip(frames, frames[1:])]
    return max(diffs), float(np.abs(frames[0][box] - frames[-1][box]).mean()), len(frames)


def video_checks(path: Path, *, min_bytes: int, min_side: int = 256) -> tuple[dict, str]:
    """对一支产物跑齐「非空壳」判定，返回（每项结论, 供打印的一行摘要）。

    每项结论是 (是否通过, 说明)；调用方决定怎么呈现与汇总。
    """
    info = probe(path)
    streams = {s["codec_type"]: s for s in info.get("streams", [])}
    fmt = info.get("format", {})
    video, audio = streams.get("video", {}), streams.get("audio", {})
    size = path.stat().st_size
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    nb_frames = int(video.get("nb_frames") or 0)
    duration = float(fmt.get("duration") or 0)
    motion, head_tail, _ = motion_report(path)
    rms, peak = audio_rms(path) if "codec_name" in audio else (0.0, 0)

    summary = " | ".join(
        f"{k}={v}" for k, v in (
            ("video", video.get("codec_name")), ("width", width), ("height", height),
            ("r_frame_rate", video.get("r_frame_rate")), ("nb_frames", nb_frames),
            ("audio", audio.get("codec_name")), ("sample_rate", audio.get("sample_rate")),
            ("channels", audio.get("channels")), ("duration", fmt.get("duration")),
            ("size", size), ("audio_rms", f"{rms:.1f}"),
        )
    )
    checks = {
        ">100KB": (size > min_bytes, f"{size} > {min_bytes}"),
        "有视频流": ("codec_name" in video, video.get("codec_name", "无")),
        "有音频流": ("codec_name" in audio, audio.get("codec_name", "无")),
        "音轨非静音": (rms > AUDIO_RMS_EPS, f"RMS {rms:.1f} > {AUDIO_RMS_EPS}，峰值 {peak}"),
        "分辨率非空壳": (
            width >= min_side and height >= min_side and nb_frames > 1,
            f"{width}x{height}, {nb_frames} 帧, {duration:.2f}s",
        ),
        "画面在动": (motion > MOTION_EPS, f"中心区域最大差 {motion:.2f}（首末帧 {head_tail:.2f}）> {MOTION_EPS}"),
    }
    return checks, summary
