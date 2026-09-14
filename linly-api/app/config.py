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
    for d in (BASE_DIR, OUTPUT_DIR, UPLOAD_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
