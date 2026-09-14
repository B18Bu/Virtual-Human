#!/bin/bash
# Linly-Talker 项目依赖安装（Phase 2）—— 必须在代码克隆完成之后执行。
#
# 用法：
#   bash setup_project.sh            # 基础依赖
#   bash setup_project.sh --musetalk # 额外装 MuseTalk 依赖（仅显存 >= 11G 时用）
#
# 本脚本明确不安装（按部署方案要求）：
#   - pytorch3d 与 TFG/requirements_nerf.txt（ER-NeRF 专用，编译风险高）
#   - CosyVoice 的 ttsfrd（只有 Python 3.8 的 Linux wheel）
set -uo pipefail

WITH_MUSETALK=0
[ "${1:-}" = "--musetalk" ] && WITH_MUSETALK=1

export PYTHONIOENCODING=utf-8
export PIP_CACHE_DIR=/root/autodl-tmp/.cache/pip
source /root/miniconda3/etc/profile.d/conda.sh
unset http_proxy https_proxy

ROOT=${LINLY_PROJECT_DIR:-/root/autodl-tmp/Linly-Talker}
step() { echo; echo "############ $* ############"; echo; }

cd "$ROOT" || { echo "!! $ROOT 不存在 —— 代码尚未克隆，先完成克隆再跑本脚本"; exit 1; }

step "0/6 确认实际存在的 requirements 文件（以实际文件名为准，不猜、不新建）"
ls -la requirements*.txt 2>/dev/null || echo "(根目录下没有 requirements*.txt)"

if [ -f requirements_webui.txt ]; then
  REQ=requirements_webui.txt
elif [ -f requirements.txt ]; then
  REQ=requirements.txt
  echo "!! 未找到 requirements_webui.txt，改用 requirements.txt —— 需在报告中记录此偏差"
else
  echo "!! 根目录没有任何 requirements*.txt。实际内容如下，请人工确认后再继续："
  ls -1
  exit 2
fi
echo ">> 将使用：$REQ"

step "1/6 安装主依赖（$REQ）"
conda run -n linly pip install -r "$REQ" || echo "!! $REQ 安装存在失败项，记录待查"

step "2/6 安装语音克隆依赖 VITS/requirements.txt"
if [ -f VITS/requirements.txt ]; then
  conda run -n linly pip install -r VITS/requirements.txt || echo "!! VITS 依赖安装存在失败项"
else
  echo "!! VITS/requirements.txt 不存在，跳过（记录待查）"
fi

step "3/6 安装音频处理依赖 sox / libsox-dev（apt，非 Python 包）"
sudo apt-get install -y sox libsox-dev > /dev/null 2>&1 \
  && echo ">> sox 安装完成" \
  || echo "!! sox 安装失败，可能需要 sudo 权限或网络（记录待查）"
command -v sox > /dev/null && sox --version

if [ "$WITH_MUSETALK" -eq 1 ]; then
  step "4/6 安装 MuseTalk 依赖（mmcv/mmdet/mmpose，编译耗时较长）"
  conda run -n linly pip install --no-cache-dir -U openmim || echo "!! openmim 安装失败"
  conda run -n linly mim install mmengine || echo "!! mmengine 失败"
  conda run -n linly mim install "mmcv==2.1.0" || echo "!! mmcv 失败"
  conda run -n linly mim install "mmdet>=3.1.0" || echo "!! mmdet 失败"
  conda run -n linly mim install "mmpose>=1.1.0" || echo "!! mmpose 失败"
else
  step "4/6 跳过 MuseTalk 依赖（未传 --musetalk）"
  echo ">> MuseTalk 的 mmcv/mmdet/mmpose 编译风险高，仅在显存 >= 11G 确实需要时才装"
fi

step "5/6 依赖自检"
conda run -n linly python - <<'PY'
import importlib
mods = ["torch", "torchvision", "torchaudio", "gradio", "transformers",
        "librosa", "soundfile", "cv2", "PIL", "numpy"]
for m in mods:
    try:
        mod = importlib.import_module(m)
        print(f"  OK    {m:14s} {getattr(mod, '__version__', '?')}")
    except Exception as e:
        print(f"  MISS  {m:14s} {type(e).__name__}: {e}")
PY

step "6/6 项目语法自检（不启动服务）"
conda run -n linly python -m py_compile webui.py && echo ">> webui.py 语法通过" || echo "!! webui.py 编译失败，见上方报错"
conda run -n linly ffmpeg -version 2>/dev/null | head -n 1 || echo "!! ffmpeg 不可用（需 >= 4.2.2）"

echo
echo "SETUP_PROJECT_DONE"
