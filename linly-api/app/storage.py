"""上传与产物的落盘管理。

所有文件名都来自外部输入（task_id 是我们自己生成的，但 filename 来自 URL），
因此统一走 safe_join 做目录穿越防护。
"""
from __future__ import annotations

import base64
import binascii
import re
import time
from pathlib import Path

from . import config

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def safe_join(base: Path, filename: str) -> Path | None:
    """把 filename 拼到 base 下；名字非法或越界则返回 None。"""
    if not filename or not _SAFE_NAME.match(filename) or ".." in filename:
        return None
    candidate = (base / filename).resolve()
    try:
        candidate.relative_to(base.resolve())
    except ValueError:
        return None
    return candidate


def _normalise_suffix(suffix: str) -> str:
    suffix = suffix.lower()
    if not suffix.startswith("."):
        suffix = "." + suffix
    return suffix


def save_bytes(data: bytes, suffix: str, task_id: str, *, kind: str = "upload") -> Path:
    """把二进制内容写入 uploads 目录，返回落盘路径。"""
    suffix = _normalise_suffix(suffix)
    if suffix not in config.ALLOWED_IMAGE_SUFFIX:
        raise ValueError(f"不支持的图片格式 {suffix}，允许：{sorted(config.ALLOWED_IMAGE_SUFFIX)}")
    if len(data) > config.MAX_IMAGE_BYTES:
        raise ValueError(f"图片超过大小上限 {config.MAX_IMAGE_BYTES // 1024 // 1024}MB")

    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.UPLOAD_DIR / f"{kind}-{task_id}-{int(time.time() * 1000)}{suffix}"
    dest.write_bytes(data)
    return dest


def save_base64_image(b64: str, task_id: str) -> Path:
    """解码 base64 图片并落盘。容忍带 data URI 前缀的输入。"""
    payload = b64.strip()
    if payload.startswith("data:"):
        _, _, payload = payload.partition(",")
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"image_base64 解码失败：{exc}") from exc
    return save_bytes(data, ".png", task_id, kind="b64")


def result_path(task_id: str, suffix: str = ".mp4") -> Path:
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return config.OUTPUT_DIR / f"{task_id}{_normalise_suffix(suffix)}"


def resolve_result(filename: str) -> Path | None:
    """解析产物文件名；不在 outputs 目录内或后缀不允许则返回 None。"""
    path = safe_join(config.OUTPUT_DIR, filename)
    if path is None or not path.is_file():
        return None
    if path.suffix.lower() not in config.ALLOWED_RESULT_SUFFIX:
        return None
    return path


def delete_quietly(path: Path | None) -> None:
    """删除文件，失败不抛异常（清理路径不应影响主流程）。"""
    if path is None:
        return
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        pass
