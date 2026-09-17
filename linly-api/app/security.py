"""API Key 鉴权 + 产物下载的短期签名。

公网地址是裸奔的（AutoDL 映射不提供任何鉴权），没有这一层就等于把 GPU 送人白嫖。

产物下载走**能力 URL + 短期签名**而非 X-API-Key：`<video src>` 和 `<a href download>`
是浏览器发起的裸导航，**带不上自定义请求头**，用头部鉴权会把在线播放和下载按钮一起弄坏。
签名放在查询串里，浏览器能直接播，链接过期即作废。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
from pathlib import Path

from fastapi import Header, HTTPException, status

from . import config

_cached_key: str | None = None
_cached_url_secret: str | None = None

# 签名是 SHA256 的十六进制表示，恒为 64 个字符。先过正则再比较有两个好处：
#   ① 堵住 compare_digest 对**非 ASCII 输入抛 TypeError** 的坑——查询串里
#      任何人都能塞 `?s=%E4%B8%AD`，不拦就是未捕获异常 → 500 + traceback 进日志；
#   ② 长度不等直接判否，不把长度信息泄进时序。
_HEX_SIG = re.compile(r"^[0-9a-f]{64}$")

# 签名消息带版本前缀：将来若要往消息里加字段（绑定任务/租户等），
# 可以上 v2 而不必让所有在途 URL 一次性失效。
_SIG_VERSION = "v1"


def _const_eq(a: str, b: str) -> bool:
    """定时安全比较。

    ⚠️ `compare_digest` 收 `str` 时**要求纯 ASCII**，遇到非 ASCII 是**抛 TypeError**
    而不是返回 False。HTTP 头走 latin-1 解码、查询串可解出任意 Unicode，都能触发。
    统一编码成 bytes 再比，把它变回「返回布尔」的语义。
    """
    return hmac.compare_digest(
        a.encode("utf-8", "surrogateescape"),
        b.encode("utf-8", "surrogateescape"),
    )


def _load_or_create(path: Path, env_value: str) -> str:
    """环境变量 → 磁盘（0600）→ 生成落盘。

    生成时用 `O_CREAT|O_EXCL` 独占创建，**建完再回读一次**：多 worker 并发首次启动
    时若各自生成，会得到不同密钥。本服务 `run.sh` 钉死 `--workers 1`，属纵深防御——
    密钥分叉的症状是「随机 401 / 随机下载失败」，极难排查，不如从源头堵掉。
    """
    if env_value.strip():
        return env_value.strip()
    if path.exists():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing

    candidate = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass  # 别的进程刚建好，下面回读它那份
    else:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(candidate)
    return path.read_text(encoding="utf-8").strip() or candidate


def load_or_create_api_key() -> str:
    """API Key。优先取环境变量，其次取磁盘缓存，都没有则生成并落盘（0600）。"""
    global _cached_key
    if _cached_key is None:
        _cached_key = _load_or_create(config.API_KEY_FILE, config.API_KEY_ENV)
    return _cached_key


def url_secret() -> str:
    """产物签名的 HMAC 密钥。

    ⚠️ 这个文件**必须进 `.gitignore` 与 `sync_to_repo.py` 的排除表**——它一旦进了
    公开仓库，任何人都能给任意文件名签发合法 URL，签名机制形同虚设。
    """
    global _cached_url_secret
    if _cached_url_secret is None:
        _cached_url_secret = _load_or_create(config.URL_SECRET_FILE, config.URL_SECRET_ENV)
    return _cached_url_secret


def preload_secrets() -> None:
    """在 lifespan 里预加载，别在请求处理函数里惰性加载。

    惰性加载有两个问题：① 是事件循环里的阻塞文件 IO；② 万一密钥文件被删，会**静默
    重新生成**，让所有在途 URL 集体失效。启动时加载则是一次性、可见的。
    """
    load_or_create_api_key()
    url_secret()


# --------------------------------------------------------------------- 签名
def sign_result(filename: str, exp: int) -> str:
    """对「文件名 + 过期时间」签名。

    **必须把 exp 签进去**——只签文件名等于没签：拿到任意一个合法 URL 后，
    把 `e=` 改成很大的数就能无限续命，签名照样通过。
    """
    msg = f"{_SIG_VERSION}\n{filename}\n{exp}".encode("utf-8")
    return hmac.new(url_secret().encode("utf-8"), msg, hashlib.sha256).hexdigest()


def verify_result(filename: str, exp: int, sig: str) -> bool:
    """验签。格式不符、签名不匹配、已过期，**一律返回 False，绝不抛异常**。"""
    if not _HEX_SIG.match(sig or ""):
        return False
    if not _const_eq(sig, sign_result(filename, exp)):
        return False
    return exp >= int(time.time())


# --------------------------------------------------------------------- 依赖项
def is_valid_api_key(x_api_key: str | None) -> bool:
    """只判断，不抛异常。供既想放行又想复用的场景（如产物下载的签名兜底）。"""
    if not x_api_key:
        return False
    return _const_eq(x_api_key, load_or_create_api_key())


async def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    """FastAPI 依赖项：校验 X-API-Key 请求头。"""
    if not is_valid_api_key(x_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效或缺失的 X-API-Key 请求头",
            headers={"WWW-Authenticate": "X-API-Key"},
        )
