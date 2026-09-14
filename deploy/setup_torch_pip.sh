#!/bin/bash
# PyTorch 2.4.1 + CUDA 12.1 安装（curl 取 wheel + pip 离线装）
#
# 为什么不用 conda：
#   无卡模式下本容器 cgroup 限额 memory.max = 2GB / cpu.max = 0.5 核。
#   conda 在 "Verifying transaction" 阶段要为整个事务在内存里建索引（匿名内存，
#   内核无法回收），7GB 的 PyTorch+CUDA 包必然触顶 → 进程被 SIGKILL（退出码 137）。
#   已实测两次，内存采样峰值精确顶到 2.00GB，确认非偶发。
#   pip 是流式下载 + 逐个解包，内存占用低一个量级。
#
# 为什么不让 pip 直接联网装（2026-09-13 有卡模式下实测定位）：
#   Aliyun CDN 对 HTTP/1.1 单连接限速到约 400 KB/s，同一 URL 走 HTTP/2 可达
#   14~16 MB/s。pip 的 urllib3 只支持 HTTP/1.1，因此换源也救不了：
#     官方 download.pytorch.org  196 KB/s
#     Aliyun pytorch-wheels      469 KB/s
#     同 URL curl --http2        14.41 MB/s
#     curl 强制 --http1.1        0.41 MB/s   ← 复现限速，根因确认
#   结论：用 curl --http2 并发把 wheel 拉到 /root/autodl-tmp/wheels，再让 pip
#   从本地文件装。纯 Python 小依赖（合计约 20MB）留给 pip 从 PyPI 解析。
#
# 版本严格钉死，与部署方案一致：torch 2.4.1 / torchvision 0.19.1 / torchaudio 2.4.1 / cu121。
set -uo pipefail

export PYTHONIOENCODING=utf-8
export PIP_CACHE_DIR=/root/autodl-tmp/.cache/pip
# pip 解包中转目录默认落在 /tmp（系统盘）。大 wheel 解包会产生 GB 级临时文件，
# 统一挪到数据盘，既遵守「系统盘不留产物」约束，又与 site-packages 同文件系统
# 走 rename 而非跨设备拷贝。
export TMPDIR=/root/autodl-tmp/.tmp
export WHEELDIR=/root/autodl-tmp/wheels
mkdir -p "$PIP_CACHE_DIR" "$TMPDIR"

source /root/miniconda3/etc/profile.d/conda.sh
conda activate linly
# 注意：不要在这里 source /etc/network_turbo。该加速代理的 no_proxy 已含
# aliyuncs.com，且其自带说明写明「开启后访问 pip 源会更慢」，与本题无关。

echo "=== 目标环境 ==="
which python
python -V

if ! ls "$WHEELDIR"/torch-*.whl > /dev/null 2>&1; then
  echo
  echo "=== 本地 wheel 缺失，先执行抓取 ==="
  bash "$(dirname "$0")/fetch_torch_wheels.sh" || { echo "!! wheel 抓取失败"; exit 1; }
fi

echo
echo "=== 从本地 wheel 安装（全部显式传路径，杜绝 pip 回落慢速 HTTP/1.1） ==="
pip install "$WHEELDIR"/*.whl --find-links="$WHEELDIR"
rc=$?
if [ $rc -ne 0 ]; then
  echo "!!!! pip 安装 PyTorch 失败，退出码 $rc !!!!"
  exit $rc
fi

echo
echo "=== 版本自检 ==="
python - <<'PY'
import torch
print("torch      ", torch.__version__)
print("torch.cuda ", torch.version.cuda)
print("cudnn      ", torch.backends.cudnn.version())
avail = torch.cuda.is_available()
print("cuda avail ", avail)
if avail:
    print("gpu        ", torch.cuda.get_device_name(0))
    print("VRAM_GB    ", round(torch.cuda.get_device_properties(0).total_memory / 1024**3, 1))
    print("capability ", torch.cuda.get_device_capability(0))
    # 不只是看 is_available，真跑一次卷积确认 cudnn 链路通
    import torch.nn as nn
    y = nn.Conv2d(3, 8, 3, padding=1).cuda()(torch.randn(2, 3, 64, 64, device="cuda"))
    print("cudnn 卷积  OK", tuple(y.shape))
else:
    print("!! cuda 不可用 —— 排查驱动 / wheel 版本")
import torchvision, torchaudio
print("torchvision", torchvision.__version__)
print("torchaudio ", torchaudio.__version__)
PY

echo
echo "TORCH_PIP_DONE"
