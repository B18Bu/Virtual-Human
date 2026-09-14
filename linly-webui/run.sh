#!/bin/bash
# 启动 Linly-Talker 自带的 Gradio WebUI（数字人对话交互界面），监听 6008。
#
# 与 linly-api 的分工：
#   6006 → FastAPI 服务（异步任务制，适合程序调用与批量生成）
#   6008 → Gradio WebUI（人在浏览器里点着用：语音对话 / 多轮对话 / 实时对话）
#
# 两者共用同一张卡上的模型，显存不够时优先停掉这个（它加载的模型更多）。
set -euo pipefail

export PYTHONIOENCODING=utf-8
export LINLY_WEBUI_PORT="${LINLY_WEBUI_PORT:-6008}"

mkdir -p /root/autodl-tmp/logs

source /root/miniconda3/etc/profile.d/conda.sh
conda activate linly

cd /root/autodl-tmp/linly-webui
exec python -u launch.py
