"""请求 / 响应数据模型。"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from . import config


class TaskStatus(str, Enum):
    QUEUED = "queued"        # 已入队，等待 GPU 空闲
    RUNNING = "running"      # 正在生成
    SUCCEEDED = "succeeded"  # 成功，result_url 可用
    FAILED = "failed"        # 失败，error 字段含原因
    CANCELED = "canceled"    # 用户取消


Mode = Literal["auto", "musetalk", "sadtalker", "wav2lip"]


class TaskCreateJSON(BaseModel):
    """JSON 方式提交（图片以 base64 或公网 URL 提供）。"""

    text: str = Field(..., description="要播报的中文文本")
    image_base64: str = Field(..., description="人像图片的 base64（可带 data URI 前缀）")
    mode: Mode = Field(default=config.DEFAULT_MODE, description="形象驱动方式，auto 表示按显存自动选择")
    voice: str | None = Field(default=None, description="TTS 音色标识，留空用默认音色")

    @field_validator("text")
    @classmethod
    def _check_text(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("text 不能为空")
        if len(v) > config.MAX_TEXT_CHARS:
            raise ValueError(f"text 长度超过上限 {config.MAX_TEXT_CHARS}")
        return v


class TaskCreateResponse(BaseModel):
    task_id: str
    status: TaskStatus
    queue_position: int
    poll_url: str


class TaskInfo(BaseModel):
    task_id: str
    status: TaskStatus
    mode: str
    text: str
    progress: float = Field(ge=0.0, le=1.0)
    stage: str = ""
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    elapsed_seconds: float | None = None
    result_url: str | None = None
    result_bytes: int | None = None
    error: str | None = None


class VoiceStatus(str, Enum):
    ASR = "asr"              # 切片 + 语音识别中
    REVIEWING = "reviewing"  # 等人工校对标注（可提交校对后开训）
    TRAINING = "training"    # 训练中（几十分钟量级）
    DONE = "done"            # 完成，可用 finetuned:<id> 调用
    FAILED = "failed"


class VoiceCreateResponse(BaseModel):
    voice_id: str
    status: VoiceStatus
    poll_url: str
    transcript_url: str
    message: str


class VoiceInfo(BaseModel):
    voice_id: str
    status: VoiceStatus
    stage: str = ""
    progress: float = Field(ge=0.0, le=1.0)
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    elapsed_seconds: float | None = None
    source_name: str = ""
    segments: int = 0
    auto_train: bool = False
    epochs_s1: int = 0
    epochs_s2: int = 0
    has_transcript: bool = False
    model_ready: bool = False
    usage: str = ""
    error: str | None = None


class TranscriptBody(BaseModel):
    """提交人工校对后的标注。每行 `文件名<TAB>文本`。"""

    transcript: str = Field(..., description="校对后的全文，每行「文件名<TAB>文本」")


class TranscriptResponse(BaseModel):
    voice_id: str
    segments: int
    transcript: str


class HealthResponse(BaseModel):
    status: str
    gpu_available: bool
    gpu_name: str | None = None
    vram_gb: float | None = None
    deploy_scope: str
    pipeline_ready: bool
    pipeline_impl: str
    queue_depth: int
    max_workers: int
    detail: dict[str, Any] = Field(default_factory=dict)
