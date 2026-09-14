"""数字人功能大盘点：逐项实测 Linly-Talker 的常见能力，输出通过/不通过。

覆盖：
  A. 文字转语音 TTS      —— Edge-TTS（默认音色 / 指定音色）/ CosyVoice SFT / GPT-SoVITS 克隆
  B. 口型驱动            —— SadTalker / MuseTalk
  C. 语音识别 ASR        —— Whisper（走 WebUI，服务端不含 ASR）
  D. 多轮对话            —— 单轮 / 多轮上下文是否保持（走 WebUI）

为什么两套入口都要测：`linly-api`（6006）收文本、不含 ASR；ASR 与对话只在
Gradio WebUI（6008）里。所以 TTS/口型走 6006，ASR/对话走 6008。

用法：
    export PATH=/root/autodl-tmp/conda/envs/linly/bin:$PATH
    /root/autodl-tmp/conda/envs/linly/bin/python -u /root/autodl-tmp/deploy/audit_capabilities.py [--with-lipsync]

不加 --with-lipsync 就跳过口型驱动（那两项各要 30~60 秒）。
结论行以 '>>>' 开头；有任一项不通过则退出码为 1。
"""
import base64
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _probe import audio_rms, video_checks  # noqa: E402

API = "http://127.0.0.1:6006"
WEB = "http://127.0.0.1:6008"
API_KEY = Path("/root/autodl-tmp/linly-api/.api_key").read_text().strip()
CRED = Path("/root/autodl-tmp/linly-webui/.webui_credentials").read_text().strip()
WUSER, _, WPASS = CRED.partition(":")

IMAGE = Path("/root/autodl-tmp/Linly-Talker/inputs/girl.png")
# 有声的中文语音素材（RMS≈3000）。**别用静音的 wav**，否则 Whisper 返回空串、
# TTS 克隆也会「成功」但听不出问题。
SPEECH = Path("/root/autodl-tmp/linly-webui/outputs/test_question.wav")
PROMPT_TEXT = "大家好 我是数字人小美 很高兴认识你"
OUT = Path("/root/autodl-tmp/linly-webui/outputs")
TEXT = "大家好，我是数字人小美，很高兴认识你。"

WITH_LIPSYNC = "--with-lipsync" in sys.argv
results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f">>> {'✅' if ok else '❌'} {name:<26s} {detail}", flush=True)


def api(method: str, path: str, body: bytes | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(f"{API}{path}", data=body, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    req.add_header("X-API-Key", API_KEY)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def run_task(payload: dict, timeout: int = 900) -> tuple[bool, Path | None, str]:
    """提交一个任务并轮询到结束；成功则下载产物。"""
    t0 = time.time()
    st, raw = api("POST", "/api/v1/tasks", json.dumps(payload).encode())
    if st not in (200, 202):
        return False, None, f"提交失败 HTTP {st}: {raw[:200]}"
    tid = json.loads(raw)["task_id"]
    info = {}
    while time.time() - t0 < timeout:
        _, raw = api("GET", f"/api/v1/tasks/{tid}")
        info = json.loads(raw)
        if info["status"] in ("succeeded", "failed", "canceled"):
            break
        time.sleep(3)
    if info.get("status") != "succeeded":
        return False, None, f"{info.get('status')}: {info.get('error')}"
    # result_url 是公网地址，容器内解析不到，只取 path 走回环
    path = urllib.parse.urlparse(info["result_url"]).path
    st, blob = api("GET", path)
    ext = Path(path).suffix or ".mp4"
    dst = OUT / f"audit_{tid}{ext}"
    dst.write_bytes(blob)
    return True, dst, f"{len(blob)/1024:.0f} KB, {time.time()-t0:.1f}s"


# ---------------------------------------------------------------- A. TTS
print("\n===== A. 文字转语音 (TTS) =====", flush=True)

# A1 Edge-TTS 默认音色
ok, p, d = run_task({"text": TEXT, "image_base64": base64.b64encode(IMAGE.read_bytes()).decode(),
                     "mode": "sadtalker", "voice": ""})
if ok and p:
    rms, peak = audio_rms(p)
    # 用「产物有声」来间接证明 TTS 出了声（TTS 单独跑没有独立接口，靠整条链路体现）
    record("Edge-TTS 默认音色", rms > 50, f"音轨 RMS={rms:.0f}")
else:
    record("Edge-TTS 默认音色", False, d)

# A2 Edge-TTS 指定音色
# 注意音色要挑**服务端真能用**的：实测 zh-CN-YunxiNeural 在 edge-tts 7.2.8 上
# 直连也报 NoAudioReceived（微软服务侧该音色不可用），换 Yunyang 正常。
# 这类失败与我们无关，报错里也看不出来，所以验收固定用一个可用音色。
OK_VOICE = "zh-CN-YunyangNeural"
ok, p, d = run_task({"text": TEXT, "image_base64": base64.b64encode(IMAGE.read_bytes()).decode(),
                     "mode": "sadtalker", "voice": f"edge:{OK_VOICE}"})
if ok and p:
    rms, _ = audio_rms(p)
    record("Edge-TTS 指定音色", rms > 50, f"{OK_VOICE}, 音轨 RMS={rms:.0f}")
else:
    record("Edge-TTS 指定音色", False, d)

# A3 CosyVoice SFT（会连带加载 BERT+HuBERT，约 1.2G 显存）
ok, p, d = run_task({"text": TEXT, "image_base64": base64.b64encode(IMAGE.read_bytes()).decode(),
                     "mode": "sadtalker", "voice": "cosyvoice:中文女"})
if ok and p:
    rms, _ = audio_rms(p)
    record("CosyVoice SFT", rms > 50, f"中文女, 音轨 RMS={rms:.0f}")
else:
    record("CosyVoice SFT", False, d[:200])

# A4 GPT-SoVITS 声音克隆
ok, p, d = run_task({"text": TEXT, "image_base64": base64.b64encode(IMAGE.read_bytes()).decode(),
                     "mode": "sadtalker", "voice": f"gptsovits:{SPEECH}|{PROMPT_TEXT}"})
if ok and p:
    rms, _ = audio_rms(p)
    record("GPT-SoVITS 克隆", rms > 50, f"参考音频 {SPEECH.name}, 音轨 RMS={rms:.0f}")
else:
    record("GPT-SoVITS 克隆", False, d[:200])

# ---------------------------------------------------------------- B. 口型驱动
if WITH_LIPSYNC:
    print("\n===== B. 口型驱动 =====", flush=True)
    for mode in ("sadtalker", "wav2lip", "musetalk"):
        ok, p, d = run_task({"text": TEXT, "image_base64": base64.b64encode(IMAGE.read_bytes()).decode(),
                             "mode": mode})
        if ok and p:
            checks, summary = video_checks(p, min_bytes=100 * 1024)
            bad = [k for k, (o, _) in checks.items() if not o]
            record(f"口型驱动 {mode}", not bad, ("全部通过 | " if not bad else "未过：" + "、".join(bad) + " | ") + summary)
        else:
            record(f"口型驱动 {mode}", False, d[:200])
else:
    print("\n（跳过 B. 口型驱动，加 --with-lipsync 可测）", flush=True)

# ---------------------------------------------------------------- C/D. WebUI
print("\n===== C. 语音识别 / D. 多轮对话（走 WebUI 6008）=====", flush=True)
try:
    from gradio_client import Client, handle_file
    client = Client(WEB, auth=(WUSER, WPASS))

    # C1 ASR
    t = time.time()
    asr_text = str(client.predict(handle_file(str(SPEECH)), api_name="/wrapper_1")).strip()
    record("Whisper 语音识别", bool(asr_text), f"{asr_text!r} ({time.time()-t:.1f}s)")

    # D. 多轮对话
    #
    # ⚠️ 两个必须注意的测试陷阱（都实际踩过）：
    # ① 断言要有区分度。先前用「颜色」测，两轮答案措辞雷同，答对了也说明不了什么。
    # ② **必须先清会话**。Qwen 在服务端有自己的 `self.history`（`LLM/Qwen.py:34`），
    #    UI 传的 history=[] 只清空界面记录，模型内部还记着上一轮测试的内容——
    #    不清的话「对照组」会把上轮的记忆当成自己的，测出来全是假阳性。
    SYS = "你是一个简洁的助手，回答不超过 20 字。"
    SECRET = "7391"

    client.predict(api_name="/clear_session")  # 清服务端 LLM 历史，否则下面全是脏的

    # D0 对照组：会话刚清空，问「我让你记的数字」应当答不出
    _, h0 = client.predict(SYS, "我刚才让你记的数字是多少？只回答数字。", [], api_name="/chat_response")
    a0 = h0[-1][1] if h0 else ""
    record("对话 无历史对照", SECRET not in a0, f"清会话后问同一问题 → {a0!r}")

    # D1/D2 多轮记忆：跑 3 次取成功率，而不是单次抽样。
    # 单次判定会骗人：Qwen-1.8B（就这么大）的记忆本身不稳，实测数字类 3/3、
    # 姓名类 1/3，单跑一次可能全绿也可能全红。判据定为「≥2/3」，
    # 既能拦住「history 根本没接上」这种真故障（那会是 0/3），又不会因小模型
    # 偶发胡诌而误报。想提升稳定性只能换更大的 LLM——本地只有 1.8B 这一个。
    rounds = 3
    hits = 0
    last_pair = ("", "")
    for i in range(rounds):
        client.predict(api_name="/clear_session")
        h: list = []
        _, h = client.predict(SYS, f"请记住这个数字：{SECRET}。", h, api_name="/chat_response")
        a1 = h[-1][1] if h else ""
        _, h = client.predict(SYS, "我刚才让你记的数字是多少？只回答数字。", h,
                              api_name="/chat_response")
        a2 = h[-1][1] if h else ""
        last_pair = (a1, a2)
        hits += SECRET in a2
        print(f">>>   多轮第 {i+1} 次：第1轮 {a1!r} → 第2轮 {a2!r}", flush=True)

    record("对话 轮次累计", len(h) >= 2, f"history 共 {len(h)} 轮")
    record("对话 多轮上下文", hits >= 2, f"记住 {hits}/{rounds} 次（判据 ≥2/3）")
except Exception as exc:  # noqa: BLE001
    record("WebUI 连接", False, f"{type(exc).__name__}: {str(exc)[:160]}")

# ---------------------------------------------------------------- 汇总
bad = [n for n, ok, _ in results if not ok]
print(f"\n>>> 汇总：{len(results)-len(bad)}/{len(results)} 项通过", flush=True)
if bad:
    print(f">>> 未通过：{'、'.join(bad)}", flush=True)
sys.exit(1 if bad else 0)
