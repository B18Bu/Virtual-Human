"""全局配置。

所有路径默认落在数据盘 /root/autodl-tmp 下，系统盘零写入。
"""
from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------- 路径
BASE_DIR = Path(os.getenv("LINLY_API_BASE", "/root/autodl-tmp/linly-api"))
PROJECT_DIR = Path(os.getenv("LINLY_PROJECT_DIR", "/root/autodl-tmp/Linly-Talker"))

OUTPUT_DIR = Path(os.getenv("LINLY_OUTPUT_DIR", str(BASE_DIR / "outputs")))
UPLOAD_DIR = Path(os.getenv("LINLY_UPLOAD_DIR", str(BASE_DIR / "uploads")))
LOG_DIR = Path(os.getenv("LINLY_LOG_DIR", "/root/autodl-tmp/logs"))

# ---------------------------------------------------------------- 服务
HOST = os.getenv("LINLY_HOST", "0.0.0.0")
# AutoDL 仅把 6006 / 6008 映射公网，FastAPI 必须占用 6006。
# 该端口与 Linly-Talker 的 configs.py 无关——本服务不启动 Gradio，不改动项目源码。
PORT = int(os.getenv("LINLY_PORT", "6006"))

# 对外暴露的服务地址（AutoDL 控制台「自定义服务」入口）
PUBLIC_URL = os.getenv("AutoDLService6006URL", "")

# ---------------------------------------------------------------- 鉴权
API_KEY_ENV = os.getenv("LINLY_API_KEY", "")
API_KEY_FILE = BASE_DIR / ".api_key"

# ---------------------------------------------------------------- 任务队列
# 单卡只能串行跑 GPU 推理，worker 数固定为 1，多余请求排队。
MAX_WORKERS = int(os.getenv("LINLY_MAX_WORKERS", "1"))
MAX_QUEUE_SIZE = int(os.getenv("LINLY_MAX_QUEUE_SIZE", "64"))
# 任务记录在内存中的保留时长（秒），超时后自动清理
TASK_TTL_SECONDS = int(os.getenv("LINLY_TASK_TTL", str(6 * 3600)))

# ---------------------------------------------------------------- 上传限制
MAX_IMAGE_BYTES = int(os.getenv("LINLY_MAX_IMAGE_BYTES", str(10 * 1024 * 1024)))
MAX_TEXT_CHARS = int(os.getenv("LINLY_MAX_TEXT_CHARS", "1000"))
# 整个请求体的上限。图片 base64 后膨胀约 4/3，这里留够余量再兜一层，
# 防止有人用一个超大 JSON 请求把进程内存打爆。
MAX_REQUEST_BYTES = int(os.getenv("LINLY_MAX_REQUEST_BYTES", str(MAX_IMAGE_BYTES * 2)))
ALLOWED_IMAGE_SUFFIX = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
ALLOWED_RESULT_SUFFIX = {".mp4", ".wav", ".png", ".jpg", ".jpeg"}

# ---------------------------------------------------------------- 音色训练
# 训练用的音频素材。训练要的是「1~30 分钟干净人声」，比图片大得多，
# 所以单独一套后缀白名单与体积上限（默认 100MB，够放 10 分钟无损 wav）。
ALLOWED_AUDIO_SUFFIX = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".wma"}
MAX_AUDIO_BYTES = int(os.getenv("LINLY_MAX_AUDIO_BYTES", str(100 * 1024 * 1024)))

# 训练流水线所在（**项目仓库之外**，理由见该目录 pipeline.py 的文件头注释）
TRAIN_SCRIPT = os.getenv("LINLY_TRAIN_SCRIPT", "/root/autodl-tmp/voice-train/pipeline.py")
TRAIN_PYTHON = os.getenv("LINLY_TRAIN_PYTHON",
                         "/root/autodl-tmp/conda/envs/linly/bin/python")
# 训练中间产物（切片/特征/ckpt）体积可观，放数据盘
TRAIN_WORK_DIR = Path(os.getenv("LINLY_TRAIN_WORK_DIR", str(BASE_DIR / "voice_work")))
# 训练好的音色最终落在这里，供 adapter 加载。
# ⚠️ 这个路径**不能含 "pretrained" 子串**：VITS/GPT_SoVITS.py:351 用
#    `if ("pretrained" not in sovits_path): del vq_model.enc_q` 做**路径子串匹配**，
#    微调产物本就不含 enc_q，路径里带上它会让判断走错分支。
VOICES_DIR = Path(os.getenv("LINLY_VOICES_DIR", "/root/autodl-tmp/voices"))

# 训练任务记录保留时长。训练产物是**目录**且比一次口播宝贵，默认留 7 天。
TRAIN_TTL_SECONDS = int(os.getenv("LINLY_TRAIN_TTL", str(7 * 24 * 3600)))
# 同时只允许一个训练（单卡，且训练与推理互斥）
TRAIN_MAX_CONCURRENCY = int(os.getenv("LINLY_TRAIN_MAX_CONCURRENCY", "1"))
# 默认训练轮数
TRAIN_EPOCHS_S1 = int(os.getenv("LINLY_TRAIN_EPOCHS_S1", "8"))
TRAIN_EPOCHS_S2 = int(os.getenv("LINLY_TRAIN_EPOCHS_S2", "8"))

# ---------------------------------------------------------------- 模型
# auto: 按显存自动选择；其余为强制指定
DEFAULT_MODE = os.getenv("LINLY_DEFAULT_MODE", "auto")
# 启动时是否预加载模型。False 时首个任务会承担加载耗时。
PRELOAD_ON_STARTUP = os.getenv("LINLY_PRELOAD", "0") == "1"

# 硬件能力探测门槛（GB），与部署提示词第三节表格一致
VRAM_FULL = 11.0    # >= 11G 可启用 MuseTalk 实时对话
VRAM_MID = 6.0      # 6~10G 跳过 MuseTalk；< 6G 仅 Wav2Lip + EdgeTTS


def ensure_dirs() -> None:
    """确保所有可写目录存在。"""
    for d in (BASE_DIR, OUTPUT_DIR, UPLOAD_DIR, LOG_DIR,
              TRAIN_WORK_DIR, VOICES_DIR):
        d.mkdir(parents=True, exist_ok=True)
