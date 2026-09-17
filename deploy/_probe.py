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

# 「画面在动」的判据：把画面切成 MOTION_GRID x MOTION_GRID 块，取**全片变化最大的
# 那一块**的相邻帧平均灰度差，低于 MOTION_EPS 即认为画面没动。
#
# **为什么用分块而不是原来的「中心 50%x50%」**（2026-09-14 修正）：
# 中心区判据实质上在测「全画面抖动」，对**只重绘嘴部**的引擎（MuseTalk / Wav2Lip）
# 天生不利——嘴在 640x1024 里只是一小块，区域均值把它按面积稀释掉了。实测同一个
# 真在动的 MuseTalk 产物，中心区判据只有 0.27，而它嘴部的真实运动是 2.7。分块等于
# 把「面积」这个无关变量归一化掉，测的是「有没有任何一处真的在动」。
#
# 阈值来自实测（2026-09-14，静止背景为同一张 640x1024 人像）而不是估计：
#   · 静帧基线 0.056——首帧用同样参数编成同时长静帧视频，libx264 对相同输入几乎输出相同帧；
#   · 静音期的 MuseTalk 产物 0.513——**嘴闭着不动但画面本身合法**，必须判否（见交接文档 5.9）；
#   · 真实产出：MuseTalk 2.71 / Wav2Lip 5.77 / SadTalker 10.72。
# 取 1.0：高于静帧基线 18 倍、高于「静音期静嘴」2 倍，又低于实测最低的真产出 2.7 倍。
MOTION_EPS = 1.0

# 分块网格边长（8 → 64 块，640x1024 时每块 80x128 px）。口型只影响脸部一小块，
# 网格太粗会把嘴淹回去，太细则块内像素太少、均值噪声变大。
MOTION_GRID = 8

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
    """返回（最大分块相邻帧平均灰度差, 首末帧同一分块差, 帧数）。只解码不看音频。

    第一个返回值是「全片任何一块里出现过的最大帧间变化」，就是「画面在动」的依据；
    第二个是首末帧在**同一块**上的差，用来说明动作是否贯穿全片（只做参考不做判据）。
    """
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
    # 用取整切分而不是 h//grid，保证最后一行/列不留缝隙（不整除时也不丢像素）
    ys = [round(k * h / MOTION_GRID) for k in range(MOTION_GRID + 1)]
    xs = [round(k * w / MOTION_GRID) for k in range(MOTION_GRID + 1)]

    best, best_box = 0.0, None
    for i in range(MOTION_GRID):
        for j in range(MOTION_GRID):
            box = (slice(ys[i], ys[i + 1]), slice(xs[j], xs[j + 1]))
            if ys[i] == ys[i + 1] or xs[j] == xs[j + 1]:
                continue  # 画面小于网格时会出现空块（验收产物边长 ≥256，正常不会走到）
            diffs = [float(np.abs(a[box] - b[box]).mean()) for a, b in zip(frames, frames[1:])]
            m = max(diffs)
            if m > best:
                best, best_box = m, box

    head_tail = 0.0
    if best_box is not None:
        head_tail = float(np.abs(frames[0][best_box] - frames[-1][best_box]).mean())
    return best, head_tail, len(frames)


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
        "画面在动": (
            motion > MOTION_EPS,
            f"最大分块帧间差 {motion:.2f}（首末帧同块 {head_tail:.2f}）> {MOTION_EPS}",
        ),
    }
    return checks, summary
