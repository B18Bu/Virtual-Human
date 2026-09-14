"""在 6008 端口启动 Linly-Talker 自带的 Gradio WebUI（数字人对话交互界面）。

为什么不直接 `python webui.py`：

1. **端口冲突**。`webui.py:15` 是 `from configs import *`，端口来自 `configs.py:2` 的
   `port = 6006`；而 6006 已经被本项目的 FastAPI 服务独占（AutoDL 只映射 6006/6008，
   两个端口都得用上），且改 `configs.py` 属部署硬性约束第 3 条明令禁止。
   → 这里在 `webui.py` 执行那行之前先把 `configs.port` 改掉，`import *` 自然就拿到新值，
     **不需要改任何项目源码**。

2. **鉴权**。6008 同样映射到公网，而 Gradio 默认**没有任何鉴权**——裸奔等于把 GPU
   免费送人。`webui.py` 调 `demo.launch()` 时没传 `auth`，所以在启动前把
   `gr.Blocks.launch` 包一层注入 auth（`TabbedInterface` 不覆盖 launch，包父类即可）。
"""
import os
import runpy
import secrets
import sys
from pathlib import Path

PROJ = Path(os.getenv("LINLY_PROJECT_DIR", "/root/autodl-tmp/Linly-Talker"))
PORT = int(os.getenv("LINLY_WEBUI_PORT", "6008"))
# configs.py 里 ip = '127.0.0.1'，只改 port 的话 Gradio 只绑回环，公网入口访问不到
# （AutoDL 的映射是打到容器网卡上的）。要对外就必须绑 0.0.0.0。
HOST = os.getenv("LINLY_WEBUI_HOST", "0.0.0.0")
CRED_FILE = Path(__file__).resolve().parent / ".webui_credentials"

# 平台注入了非法的 OMP_NUM_THREADS=0（见交接文档坑 3），且 nproc 报 224 而 cgroup 只给 25 核
os.environ["OMP_NUM_THREADS"] = os.getenv("OMP_NUM_THREADS_OVERRIDE", "8")
os.environ["MKL_NUM_THREADS"] = os.getenv("MKL_NUM_THREADS_OVERRIDE", "8")
os.environ["PYTHONIOENCODING"] = "utf-8"
os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")

# 让 Gradio 把「自己对外是什么地址」钉死成公网入口。
#
# 不设的话，Gradio 走 route_utils.get_root_url() 推断，优先级是：
#   ① root_path 是完整 URL → 直接用它；② x-forwarded-host 头；③ 请求自身的 URL。
# AutoDL 的网关**不转发 x-forwarded-host**，于是落到 ③，前端拿到的
# window.gradio_config.root 会变成 `http://localhost:6008` —— 浏览器随后就把
# 登录请求 POST 到**用户自己电脑的 6008**，表现是「登录页一直转、怎么也进不去」。
# 设成完整 URL 后命中 ①，与请求头无关，无论从公网还是内网访问都正确。
os.environ.setdefault("GRADIO_ROOT_PATH", os.getenv("AutoDLService6008URL", ""))

# 项目里多处用裸命令名调 ffmpeg（交接文档坑 13），只认 PATH
env_bin = str(Path(sys.executable).parent)
os.environ["PATH"] = env_bin + os.pathsep + os.environ.get("PATH", "")

os.chdir(PROJ)
sys.path.insert(0, str(PROJ))

# ---------------------------------------------------------------- ① 端口
import configs  # noqa: E402

_original_port, _original_ip = configs.port, configs.ip
configs.port, configs.ip = PORT, HOST
print(
    f"[launcher] configs.port: {_original_port} -> {configs.port}"
    f" | configs.ip: {_original_ip!r} -> {configs.ip!r}"
    "（webui 的 `from configs import *` 会拿到新值）"
)

# ---------------------------------------------------------------- ② 鉴权
import secrets as _secrets  # noqa: E402
import string  # noqa: E402

import gradio as gr  # noqa: E402

# 生成密码用的字符集：**刻意剔除了易混字符**。
# 起因：先前用 secrets.token_urlsafe() 生成的密码里同时有字母 `o` 和数字 `0`，
# 用户照着抄必然看错，登录一直 400。这类密码是给人念/抄的，可读性优先于字符集大小。
# 去掉 0/O、1/l/I、2/Z、5/S、8/B 之后仍有 57 个字符，16 位约 93 bit，够用。
_ALPHABET = "".join(
    c for c in string.ascii_letters + string.digits
    if c not in "0O1lI2Z5S8B"
)


def _gen_password(n: int = 16) -> str:
    return "".join(_secrets.choice(_ALPHABET) for _ in range(n))


if CRED_FILE.is_file():
    user, _, password = CRED_FILE.read_text().strip().partition(":")
else:
    user = os.getenv("LINLY_WEBUI_USER", "linly")
    # 想自定义就设 LINLY_WEBUI_PASSWORD，或删掉凭据文件重启即可重新生成。
    password = os.getenv("LINLY_WEBUI_PASSWORD") or _gen_password()
    CRED_FILE.write_text(f"{user}:{password}")
    CRED_FILE.chmod(0o600)

_orig_launch = gr.Blocks.launch


def _launch_with_auth(self, *args, **kwargs):
    kwargs.setdefault("auth", (user, password))
    kwargs.setdefault("auth_message", "Linly-Talker 数字人对话界面 —— 请输入用户名密码")
    return _orig_launch(self, *args, **kwargs)


gr.Blocks.launch = _launch_with_auth

print("=" * 68)
print(f"[launcher] 地址   : http://0.0.0.0:{PORT}")
print(f"[launcher] 公网   : {os.getenv('AutoDLService6008URL', '（未设置 AutoDLService6008URL）')}")
print(f"[launcher] 用户名 : {user}")
print(f"[launcher] 密码   : {password}")
print(f"[launcher] 凭据文件: {CRED_FILE}")
print(f"[launcher] root_path: {os.environ.get('GRADIO_ROOT_PATH') or '（空，将按请求头推断）'}")
# 单独再打一行「用户名:密码」，方便直接复制；密码字符集已剔除易混字符
print(f"[launcher] 登录用 : {user} / {password}")
print(f"[launcher] 换密码 : 设 LINLY_WEBUI_PASSWORD 或删掉凭据文件后重启")
print("=" * 68)

# ---------------------------------------------------------------- ③ 跑 webui
runpy.run_path(str(PROJ / "webui.py"), run_name="__main__")
