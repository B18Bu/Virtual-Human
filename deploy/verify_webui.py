"""验证 Gradio WebUI 的对话链路：语音 + 图片 → ASR → 应答 → TTS → 数字人视频。

走的是**真实的 UI 端点**（`/human_response`），不是另起进程直调函数——这样验的才是
「用户在浏览器里点一下会发生什么」。

用法（WebUI 须已在 6008 监听）：
    /root/autodl-tmp/conda/envs/linly/bin/python -u /root/autodl-tmp/deploy/verify_webui.py [llm]

    不传 llm        → 用界面默认的「直接回复」（不加载 LLM，验管线本身）
    传 llm=Qwen     → 先切到 Qwen 再跑（验真 LLM）

结论行统一用 '>>>' 前缀，全通过时退出码 0。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _probe import video_checks  # noqa: E402

from gradio_client import Client, handle_file  # noqa: E402

BASE = "http://127.0.0.1:6008"
CRED = Path("/root/autodl-tmp/linly-webui/.webui_credentials").read_text().strip()
USER, _, PASSWORD = CRED.partition(":")

IMAGE = "/root/autodl-tmp/Linly-Talker/inputs/girl.png"
# 用户提问的语音素材。**必须是有声的**——曾经拿一个全静音的 wav 当素材，
# Whisper 老老实实返回空字符串，看起来像 ASR 坏了，其实是素材本身没声音。
# 这份是从已验收的产物里抽出来的真实中文语音（RMS≈3000）。
AUDIO = "/root/autodl-tmp/linly-webui/outputs/test_question.wav"
OUT = Path("/root/autodl-tmp/linly-webui/outputs")
LLM = sys.argv[1] if len(sys.argv) > 1 else None
SYSTEM = "你是一个友好的数字人助手，回答要简短口语化。"

fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f">>> 验收（{name:<14s}）: {'通过' if ok else '不通过'}   {detail}", flush=True)
    if not ok:
        fails.append(name)


print(f">>> 连接 WebUI {BASE}", flush=True)
t0 = time.time()
client = Client(BASE, auth=(USER, PASSWORD))
print(f">>> 已连接（{time.time()-t0:.1f}s）", flush=True)

if LLM:
    t = time.time()
    got = client.predict(LLM, api_name="/llm_model_change")
    print(f">>> 切换 LLM -> {LLM}：{got}（{time.time()-t:.1f}s，含加载权重）", flush=True)
    check("LLM 切换", str(got) == LLM or LLM in str(got), str(got))

# ① ASR：语音 → 文字
t = time.time()
asr_text = client.predict(handle_file(AUDIO), api_name="/wrapper_1")
print(f">>> ASR 结果      : {asr_text!r}（{time.time()-t:.1f}s）", flush=True)
check("ASR 出文字", bool(str(asr_text).strip()), str(asr_text)[:80])

# ② 对话：文字 → 应答，产出 history
#    （/human_response 读的是 history[-1]，必须先有这一轮，否则 IndexError）
t = time.time()
_, history = client.predict(SYSTEM, str(asr_text), [], api_name="/chat_response")
print(f">>> 对话 history  : {history}（{time.time()-t:.1f}s）", flush=True)
check("产生对话轮次", isinstance(history, list) and len(history) > 0, f"{len(history) if isinstance(history, list) else '?'} 轮")

# ③ 完整链路：用 history 里的应答 + 图片 + 语音 → 数字人视频
print(">>> 提交完整对话链路（ASR → 应答 → TTS → 口型驱动）…", flush=True)
t = time.time()
result = client.predict(
    handle_file(IMAGE),      # source_image
    history,                 # history ← 上一步的对话结果
    handle_file(AUDIO),      # question_audio
    "SadTalker",             # talker_method
    "zh-CN-XiaoxiaoNeural",  # voice
    0, 100, 0,               # rate / volume / pitch
    "FastSpeech2", "PWGan", "zh", False,   # am / voc / lang / male
    None, "", "中文", "中文", "凑四句一切", False,   # inp_ref…use_mic_voice
    "预训练音色", "中文女", "",  # mode_checkbox_group / sft_dropdown / prompt_text_cv
    None, None,              # prompt_wav_upload / prompt_wav_record
    0, 1.0,                  # seed / speed_factor
    "Edge-TTS", "自定义角色",  # tts_method / character
    "full",                  # preprocess_type ← 用 full，UI 默认的 crop 只会出 256x256 裁脸
    False, False, 2, 256, 0, "facevid2vid", 1, True, 20,
    #                 ↑ size_of_image 必须是**整数**：UI 里是 gr.Radio([256, 512])，
    #                   浏览器传的是 int；只有 API schema 把它序列化成了字符串。
    #                   传 "256"（字符串）会一路传到 cv2.resize(frame, ('256','256')) 崩掉，
    #                   报错是「Can't parse 'dsize'」，完全看不出是类型问题。
    api_name="/human_response",
)
elapsed = time.time() - t
print(f">>> 链路返回      : {result}（耗时 {elapsed:.1f}s）", flush=True)

video = result.get("video") if isinstance(result, dict) else result
check("产出视频", bool(video) and Path(video).is_file(), str(video))
if not (video and Path(video).is_file()):
    print(f">>> 验收结论      : 未通过：{fails}", flush=True)
    sys.exit(1)

OUT.mkdir(parents=True, exist_ok=True)
final = OUT / "webui_dialogue.mp4"
final.write_bytes(Path(video).read_bytes())
size = final.stat().st_size
print(f">>> 产物          : {final}  {size} bytes ({size/1024:.0f} KB)", flush=True)

checks, summary = video_checks(final, min_bytes=100 * 1024)
print(f">>> ffprobe       : {summary}", flush=True)
for name, (ok, detail) in checks.items():
    check(name, ok, detail)

print(f">>> 验收结论      : {'全部通过' if not fails else '未通过：' + '、'.join(fails)}", flush=True)
sys.exit(1 if fails else 0)
