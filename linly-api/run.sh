#!/bin/bash
# 启动 Linly-Talker FastAPI 服务。
#
# 为什么是 6006：AutoDL 只把 6006 / 6008 映射到公网，对外服务必须占 6006。
# 为什么 workers=1：任务队列与模型都常驻进程内存，多进程会各自持有一份模型，
#   既爆显存又会破坏「单卡串行」的调度前提。
set -euo pipefail

export PYTHONIOENCODING=utf-8
export PIP_CACHE_DIR=/root/autodl-tmp/.cache/pip
export LINLY_PORT="${LINLY_PORT:-6006}"

# 平台给容器注入了 OMP_NUM_THREADS=0 / MKL_NUM_THREADS=0（登录 shell 里也是），
# 0 对 libgomp 是非法值，每个 Python 进程都会打印 "libgomp: Invalid value..."，
# 且若库改去自动探测会看到宿主机的 224 核（cgroup 实际只给 25 核）而严重超订。
# 这里显式钉死，避免推理时线程抖动。
export OMP_NUM_THREADS="${OMP_NUM_THREADS_OVERRIDE:-8}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS_OVERRIDE:-8}"

mkdir -p /root/autodl-tmp/logs

source /root/miniconda3/etc/profile.d/conda.sh
conda activate linly

cd /root/autodl-tmp/linly-api
exec python -m uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "${LINLY_PORT}" \
  --workers 1 \
  --timeout-keep-alive 75
