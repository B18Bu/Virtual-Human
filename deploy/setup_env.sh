#!/bin/bash
# Linly-Talker 环境安装（Phase 1）——只依赖网络，不依赖项目源码。
#
# 频道选择依据（已实测）：
#   - tuna pytorch 镜像 0.43s，官方 conda.anaconda.org 无代理直连 2.4s
#   - tuna 没有 nvidia 镜像（404），nvidia 频道只能走官方源
#   - 国内源在关闭学术加速后快 4.4 倍，故此处一律 unset 代理
#
# 空间策略：pkgs_dirs 与 envs_dirs 同在数据盘 → conda 走硬链接，省一倍空间。
set -uo pipefail

export PYTHONIOENCODING=utf-8
export PIP_CACHE_DIR=/root/autodl-tmp/.cache/pip
source /root/miniconda3/etc/profile.d/conda.sh
unset http_proxy https_proxy

TUNA_PYTORCH=https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud/pytorch/
TUNA_MAIN=https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/main/
NVIDIA=https://conda.anaconda.org/nvidia

step() { echo; echo "############ $* ############"; echo; }
fail() { echo "!!!! 失败：$* !!!!"; exit 1; }

step "1/5 安装 PyTorch 2.4.1 + CUDA 12.1"
conda install -n linly -y \
  pytorch==2.4.1 torchvision==0.19.1 torchaudio==2.4.1 pytorch-cuda=12.1 \
  -c "$TUNA_PYTORCH" -c "$NVIDIA" -c "$TUNA_MAIN" --override-channels \
  || fail "PyTorch 安装失败"

step "2/5 安装 ffmpeg 4.2.2（项目要求 >= 4.2.2）"
conda install -n linly -y -q ffmpeg==4.2.2 \
  -c "$TUNA_MAIN" --override-channels \
  || fail "ffmpeg 安装失败"

step "3/5 升级 pip 并配置国内源 + 缓存目录"
conda run -n linly python -m pip install --upgrade pip || fail "pip 升级失败"
conda run -n linly pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
conda run -n linly pip config set global.cache-dir /root/autodl-tmp/.cache/pip
conda run -n linly pip config list

step "4/5 tensorboard（项目依赖之一）"
conda run -n linly pip install tb-nightly -i https://mirrors.aliyun.com/pypi/simple || echo "!! tb-nightly 安装失败，非致命，记录待补"

step "5/5 FastAPI 服务自身依赖"
conda run -n linly pip install -r /root/autodl-tmp/linly-api/requirements_api.txt || fail "API 依赖安装失败"

step "环境自检"
conda run -n linly python -c "
import sys, platform
print('python', sys.version.split()[0], platform.machine())
import torch
print('torch', torch.__version__, '| cuda', torch.version.cuda)
print('cuda_available', torch.cuda.is_available(), '（无卡模式应为 False）')
"
conda run -n linly ffmpeg -version 2>/dev/null | head -n 1 || echo "!! ffmpeg 不可用"
conda run -n linly python -c "import fastapi, uvicorn; print('fastapi', fastapi.__version__, '| uvicorn', uvicorn.__version__)"

echo
echo "SETUP_ENV_DONE"
