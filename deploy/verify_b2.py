"""阶段 B2（重测）：逐个点亮重模块并实测显存代价。

结果行统一用 '>>>' 前缀，避免被日志过滤器误伤。
"""
import logging
import subprocess
import sys
import time

sys.path.insert(0, "/root/autodl-tmp/linly-api")
from app.adapter import build_pipeline  # noqa: E402

logging.basicConfig(level=logging.WARNING)  # 压低日志，只留我们要的数据


def used() -> int:
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
        capture_output=True, text=True,
    ).stdout.strip()
    return int(out)


p = build_pipeline()  # chdir + sys.path，不加载权重
base = used()
print(f">>> 基线（仅进程启动）        : {base} MiB", flush=True)

for name in ("torch", "VITS", "TFG"):
    t0 = time.time()
    __import__(name)
    print(f">>> 导入 {name:6s} 后           : {used()} MiB   （本步 {time.time()-t0:.1f}s，累计 +{used()-base}）", flush=True)

from TFG import MuseTalk_RealTime, SadTalker  # noqa: E402,F401
print(f">>> 取到 TFG 两个类后          : {used()} MiB", flush=True)

m = MuseTalk_RealTime()
print(f">>> 构造 MuseTalk_RealTime 后  : {used()} MiB（该构造不加载权重）", flush=True)
m.init_model()
print(f">>> MuseTalk.init_model() 后   : {used()} MiB  ← MuseTalk 全套权重", flush=True)
