"""API Key 鉴权。

公网地址是裸奔的（AutoDL 映射不提供任何鉴权），没有这一层就等于把 GPU 送人白嫖。
"""
from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, status

from . import config

_cached_key: str | None = None


def load_or_create_api_key() -> str:
    """优先取环境变量，其次取磁盘缓存，都没有则生成并落盘（0600）。"""
    global _cached_key
    if _cached_key:
        return _cached_key

    if config.API_KEY_ENV:
        _cached_key = config.API_KEY_ENV.strip()
        return _cached_key

    if config.API_KEY_FILE.exists():
        key = config.API_KEY_FILE.read_text(encoding="utf-8").strip()
        if key:
            _cached_key = key
            return _cached_key

    key = secrets.token_urlsafe(32)
    config.API_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.API_KEY_FILE.write_text(key, encoding="utf-8")
    config.API_KEY_FILE.chmod(0o600)
    _cached_key = key
    return key


async def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    """FastAPI 依赖项：校验 X-API-Key 请求头。"""
    expected = load_or_create_api_key()
    if not x_api_key or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效或缺失的 X-API-Key 请求头",
            headers={"WWW-Authenticate": "X-API-Key"},
        )
