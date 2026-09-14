"""阶段 B3 / 验收清单第 5 项：经适配层跑一次完整的「图片 + 文本 → 口播视频」（SadTalker）。

这是第一支真视频，同时验证：
· 适配层的编排（TTS → 16k wav → 口型驱动 → 返回路径）
· 产物是**真视频而非空壳**——不只看字节数，还校验编码 / 分辨率 / 时长 / 音轨，
  以及帧与帧之间确有变化（静帧拼出来的 mp4 也可以很大，字节数单独说明不了问题）
· 部署提示词第一节第 5 条的「mp4 文件，且文件大于 100KB」

用法（必须是有卡模式）：
    export OMP_NUM_THREADS=8 MKL_NUM_THREADS=8 PYTHONIOENCODING=utf-8
    export PATH=/root/autodl-tmp/conda/envs/linly/bin:$PATH   # 脚本要调 ffprobe
    /root/autodl-tmp/conda/envs/linly/bin/python -u /root/autodl-tmp/deploy/verify_b3.py

结论行统一用 '>>>' 前缀，便于 `grep '^>>>'` 取用。全部通过时退出码为 0。
"""
import logging
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _probe import video_checks  # noqa: E402

sys.path.insert(0, "/root/autodl-tmp/linly-api")
from app.adapter import build_pipeline  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

# 部署提示词第一节第 5 条的阈值：100KB。按 KiB（1024）折算，与历来验收口径一致。
MIN_BYTES = 100 * 1024


def vram() -> str:
    return subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader"],
        capture_output=True, text=True,
    ).stdout.strip()


p = build_pipeline()
print(f">>> 起步显存 {vram()}", flush=True)

image = Path("/root/autodl-tmp/Linly-Talker/inputs/girl.png")
text = "大家好，我是数字人小美，很高兴认识你。"

t0 = time.time()
out = p.generate(
    image_path=image,
    text=text,
    mode="sadtalker",
    voice=None,
    progress_cb=lambda f, s: print(f">>> [{f:.2f}] {s}", flush=True),
    should_cancel=lambda: False,
)
elapsed = time.time() - t0
size = out.stat().st_size
print(f">>> 产物            : {out}", flush=True)
print(f">>> 产物大小        : {size} bytes ({size/1024:.0f} KB)  总耗时 {elapsed:.1f}s", flush=True)
print(f">>> 峰值后显存      : {vram()}", flush=True)

checks, summary = video_checks(out, min_bytes=MIN_BYTES)
print(f">>> ffprobe         : {summary}", flush=True)

failed = [name for name, (ok, _) in checks.items() if not ok]
for name, (ok, detail) in checks.items():
    print(f">>> 验收（{name:<6s}）: {'通过' if ok else '不通过'}   {detail}", flush=True)
print(f">>> 验收结论        : {'全部通过' if not failed else '未通过：' + '、'.join(failed)}", flush=True)
sys.exit(1 if failed else 0)
