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
    # webui.py 的全局函数在这一刻已经建好，而 `__main__` 还是它（runpy 的临时模块），
    # 这是换掉 TTS_response 全局的唯一时机——再晚 launch() 就阻塞了。
    _patch_tts_response_global()
    # 界面组件也都建好了，此刻补挂「角色 → 音色」同步事件，随 launch 一起进 config。
    _register_character_voice_sync(self)
    return _orig_launch(self, *args, **kwargs)


gr.Blocks.launch = _launch_with_auth

# ---------------------------------------------------------------- ②b 容错：gr.Warning 的 duration 被误当垃圾桶
#
# **这是踩过的坑（2026-09-14）**：在 WebUI 里「不先录音就点语音识别」，页面只显示
# 「错误」两个字。真正的错因有两层：
#   ① `asr.transcribe(None)` → whisper 内部 `torch.from_numpy(None)` →
#      `TypeError: expected np.ndarray (got NoneType)`；
#   ② 但用户看不到它——`webui.py:59` 的兜底分支写成了 `gr.Warning("ASR Error: ", e)`，
#      而 `gr.Warning(message, duration=10, visible=True)` 的**第二个位置参数是 duration**
#      （要数字）。异常对象被塞进 duration → pydantic 校验失败 → **异常处理分支自己抛异常**，
#      于是页面只剩 Gradio 的通用文案「错误」，作者本来写好的提示
#      （"音频还未传入，请重新点击一下语音识别即可"）**永远执行不到**。
#
# 同一个文件里还有一处同样的写法：`webui.py:127` 的
# `gr.Warning("无克隆环境或模型权重，无法克隆声音", e)`（GPT-SoVITS 克隆路径）。
#
# 修法：把 duration 位置上的**非数字**实参当成消息的一部分（这正是作者的本意——
# 想把错误内容一起显示出来），时长用默认值。只在我们自己的启动器里生效，
# 项目源码零改动。
_orig_warning, _orig_info, _orig_error = gr.Warning, gr.Info, gr.Error


def _tolerate_duration_as_message(func):
    def wrapper(message="", *args, **kwargs):
        if args and not isinstance(args[0], (int, float, type(None))):
            message = f"{message}{args[0]}"
            args = args[1:]
        return func(message, *args, **kwargs)

    wrapper.__name__ = getattr(func, "__name__", "wrapper")
    wrapper.__doc__ = getattr(func, "__doc__", None)
    return wrapper


gr.Warning = _tolerate_duration_as_message(_orig_warning)
gr.Info = _tolerate_duration_as_message(_orig_info)
gr.Error = _tolerate_duration_as_message(_orig_error)
print("[launcher] 已给 gr.Warning / gr.Info / gr.Error 加 duration 容错（见本文件注释）")

# ---------------------------------------------------------------- ②c 语音音色 / TTS 静默失败
#
# 两个都是项目源码里的坑（2026-09-14 实测定位），这里在启动器里绕过去，webui.py 一行不改。
#
# 【坑 A】选「男性角色」不换声音
#   `webui.py:290-307` 想表达「下拉框没选 → 用角色的默认音色」：
#       if character == '男性角色': default_voice = 'zh-CN-YunyangNeural'
#       ...
#       voice = default_voice if not voice else voice
#   但那个 `voice` 下拉框（`webui.py:401`）出厂值就是 `zh-CN-XiaoxiaoNeural`（女声），
#   **永远不为空** → `not voice` 恒为假 → `default_voice` 成了**死代码**。
#   后果：选「男性角色」只把源图换成 `inputs/boy.png`，声音仍是晓晓。
#   实测铁证：产物音轨音高中位数 **255.7 Hz**，与 `zh-CN-XiaoxiaoNeural` 逐帧一致
#   （男声 `zh-CN-YunyangNeural` 是 130 Hz），且用的确实是 boy.png。
#   修法：角色选「男性角色」而音色还停在**出厂默认值**时，替成该角色的默认音色。
#   代价：真想「男角色 + 晓晓音色」会拿到云扬——这个组合本身自相矛盾，可接受。
#
# 【坑 B】合成失败被静默吞掉
#   `webui.py:106-110` 的 Edge-TTS 分支用 try/except 包住，except 里跑 CLI 兜底，
#   **`os.system` 的返回码没人看**，函数照样 `return save_path`。
#   实测：`NoAudioReceived` 发生时 `answer.wav` 被截成 **0 字节**，界面毫无提示；
#   若失败没有截断文件，拿到的是**上一轮的音频**——听起来就是「换了音色却没变」。
#   修法：调用前后比对答案文件的 mtime 与大小，没产出本次的有效音频就抛 `gr.Error`，
#   让失败**可见**（宁可报错，也不要静默产出错音色的视频）。
import functools  # noqa: E402
import time as _time  # noqa: E402

_VOICE_LABEL = "Voice 声音选择"
# 「朗读：」直读模式的触发标记（可用 LINLY_READ_ALOUD_PREFIX 覆盖，多个用 | 分隔）
_READ_ALOUD_MARKERS = tuple(
    m for m in (os.getenv("LINLY_READ_ALOUD_PREFIX", "朗读：|朗读:|念：|[朗读]") .split("|")) if m
)


def _read_aloud_text(raw):
    """文本以标记开头 → 返回去掉标记的正文（空正文返回 None）。"""
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    for marker in _READ_ALOUD_MARKERS:
        if s.startswith(marker):
            body = s[len(marker):].strip()
            return body or None
    return None

_CHARACTER_LABEL = "角色选择"
_VOICE_FACTORY_DEFAULT = "zh-CN-XiaoxiaoNeural"  # `webui.py:401` 里写死的出厂值
_CHARACTER_VOICE_DEFAULT = {
    "男性角色": "zh-CN-YunyangNeural",   # 云扬
    "女性角色": "zh-CN-XiaoxiaoNeural",  # 晓晓
    "自定义角色": "zh-CN-XiaoxiaoNeural",
}


def _register_character_voice_sync(self):
    """坑 A 的正式修法：让「角色选择」顺手把「Voice 声音选择」下拉框也切过去。

    **为什么不是在 `human_response` 里改**：第一版补丁只包了那个视频按钮，结果
    「生成音频」按钮照样是女声——因为 `webui.py:544` 那个按钮签的是 `TTS_response`，
    而 `TTS_response` **根本没接 `character` 参数**，选「男性角色」对它毫无作用。
    （用户实测反馈：点「生成音频」仍是女声。）
    改成让角色去驱动下拉框之后：界面显示 = 实际使用，且**所有**路径
    （视频按钮 / 生成音频 / 后续对话）都自动跟着走；用户想覆盖，事后自己选一次即可。
    """
    radios, drops = [], []
    for blk in self.blocks.values():
        label = getattr(blk, "label", None)
        if label == _CHARACTER_LABEL and isinstance(blk, gr.Radio):
            radios.append(blk)
        elif label == _VOICE_LABEL and isinstance(blk, gr.Dropdown):
            drops.append(blk)
    if not radios or not drops:
        print(f"[launcher] 角色→音色同步：没找到组件（角色={len(radios)} 个，音色={len(drops)} 个），跳过")
        return

    def _on_character(character):
        want = _CHARACTER_VOICE_DEFAULT.get(character)
        print(f"[launcher] 角色「{character}」→ 音色下拉框切到 {want}", flush=True)
        return [want] * len(drops)

    # **每个角色单选框都要挂**：界面上其实有两个「角色选择」（不同标签页各一个），
    # 只挂最后一个的话，在另一个标签页里选角色是不会触发的——实测踩过。
    #
    # 组件是在 `with gr.Blocks() as demo:` 里建的；此刻注册事件需要把 blocks 上下文放回去，
    # 否则 `set_event_trigger` 找不到 root_block。
    import gradio.context as _gr_ctx

    prev = _gr_ctx.Context.root_block
    _gr_ctx.Context.root_block = self
    try:
        for radio in radios:
            radio.change(fn=_on_character, inputs=[radio], outputs=drops)
    finally:
        _gr_ctx.Context.root_block = prev
    print(
        f"[launcher] 已给 {len(radios)} 个「角色选择」挂上 → {len(drops)} 个 Voice 下拉框的同步事件",
        flush=True,
    )


def _guard_tts(fn):
    """坑 B：合成没产出本次音频就抛错，不再静默返回成功。"""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        t0 = _time.time()
        out = fn(*args, **kwargs)
        if isinstance(out, str) and out.endswith((".wav", ".mp3")):
            exists = os.path.exists(out)
            size = os.path.getsize(out) if exists else -1
            fresh = exists and size > 1024 and os.path.getmtime(out) >= t0 - 2
            if not fresh:
                raise gr.Error(
                    f"语音合成失败：{out} 没有产出本次的音频（{size} 字节）。"
                    "常见原因是所选音色当前不可用，换一个音色重试即可。"
                )
        return out

    wrapper._linly_patched = True
    return wrapper


_orig_button_click = gr.Button.click


def _click_with_fixes(self, *args, **kwargs):
    """按 `inputs` 的 label（而不是位置）定位组件，给装错的地方打补丁。"""
    fn = kwargs.get("fn", args[0] if args else None)
    inputs = kwargs.get("inputs", args[1] if len(args) > 1 else None)
    if fn is not None and not getattr(fn, "_linly_patched", False):
        # **按对象身份认人，不按函数名**：webui.py 里的 TTS_response / Talker_response_img
        # 等都被 `@calculate_time` 包过，而那个装饰器**没有用 functools.wraps**
        # （src/cost_time.py），函数名会变成 'wrapper' —— 按名字匹配必然落空，实测踩过。
        # 接线发生在 webui.py 执行期间，此刻 `__main__` 就是它自己，所以能取到原始对象。
        mod = sys.modules.get("__main__")
        known = {}
        for _nm in ("human_response", "TTS_response"):
            _obj = getattr(mod, _nm, None)
            if _obj is not None:
                known[id(_obj)] = _nm
        name = known.get(id(fn), "")
        patched = _guard_tts(fn) if name == "TTS_response" else None
        if patched is not None:
            if "fn" in kwargs or not args:
                kwargs["fn"] = patched
            else:
                args = (patched,) + tuple(args[1:])
    return _orig_button_click(self, *args, **kwargs)


gr.Button.click = _click_with_fixes


def _patch_llm_read_aloud():
    """「朗读：<正文>」直接念，跳过 LLM。

    **为什么拦在 LLM 这一层**：WebUI 三条路径（个性化角色互动 / 多轮智能对话 / MuseTalk 实时对话）
    不管走哪个按钮，最后合成的**都是「LLM 的回答」**（`webui.py:198-202`）——
    你输入的是问题，念出来的是回答。所以在 `generate()` 上拦一道，让「朗读：」后面的正文
    直接充当回答，就等于「照着稿子念」，且三条路径一起生效。

    **为什么是文字标记而不是勾选框**：界面在 launch 前就已构建完毕，此刻往布局里插一个新组件
    再把三条事件链全部重新接线，风险远大于收益（接错一处整个页面就废）。文字标记零风险，
    且在任何入口都可用（包括语音识别转出来的文字）。
    """
    import inspect

    import LLM as _llm_pkg  # noqa: E402

    patched = []
    for _, obj in vars(_llm_pkg).items():
        if not inspect.isclass(obj) or not obj.__module__.startswith("LLM"):
            continue
        gen = obj.__dict__.get("generate")
        if gen is None or getattr(gen, "_linly_patched", False):
            continue

        @functools.wraps(gen)
        def wrapper(self, question=None, *a, __gen=gen, **kw):
            body = _read_aloud_text(question)
            if body:
                print(f"[launcher] 直读模式：跳过 LLM，按原文合成 {len(body)} 字", flush=True)
                return body
            return __gen(self, question, *a, **kw)

        wrapper._linly_patched = True
        obj.generate = wrapper
        patched.append(obj.__name__)
    print(f"[launcher] 「朗读：」直读模式已挂上（{len(patched)} 个 LLM 类）", flush=True)


def _patch_tts_response_global():
    """间接调用 TTS_response 的路径（human_response / Talker_response_img 等）走全局查找，
    所以把模块全局换掉即可生效——这一步必须在 webui.py 执行期间做（此时 `__main__` 就是它）。"""
    mod = sys.modules.get("__main__")
    fn = getattr(mod, "TTS_response", None)
    if callable(fn) and not getattr(fn, "_linly_patched", False):
        mod.TTS_response = _guard_tts(fn)
        print("[launcher] TTS_response：已加「合成失败不再静默」检查", flush=True)
    else:
        print(f"[launcher] TTS_response：没找到可补丁的目标（{type(fn).__name__}），跳过", flush=True)

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

# ---------------------------------------------------------------- ③ 「朗读：」直读
# 放在这里是因为它要 `import LLM`（需要前面已 chdir + sys.path），
# 而且必须在 webui.py 开跑之前完成（它一 import 就绑定 llm 类）。
_patch_llm_read_aloud()

# ---------------------------------------------------------------- ④ 跑 webui
runpy.run_path(str(PROJ / "webui.py"), run_name="__main__")
