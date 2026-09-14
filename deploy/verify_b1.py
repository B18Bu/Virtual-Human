"""阶段 B1：验证 TTS 通路（Edge-TTS）+ 音频归一化。

不加载任何模型权重，用来确认「文本 → 音频」以及 mp3→16k 单声道 wav 的转换。
"""
import logging
import sys
import time
import wave

sys.path.insert(0, "/root/autodl-tmp/linly-api")
from app import config  # noqa: E402
from app.adapter import build_pipeline  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

p = build_pipeline()
print("pipeline:", p.name, "| ready:", p.is_ready())

work = config.OUTPUT_DIR / "verify"
work.mkdir(parents=True, exist_ok=True)

text = "大家好，这是一次语音合成测试，用于验证文本到语音的通路是否正常。"
t0 = time.time()
engine, raw = p._synthesize(text, None, work, lambda f, s: print(f"  [{f:.2f}] {s}"))
print(f"  TTS 引擎 = {engine}")
print(f"  原始产物 = {raw.name}  {raw.stat().st_size} bytes")
print(f"  文件头   = {raw.read_bytes()[:3]!r}  （'ID3' 即 MP3，证明扩展名不可信）")

wav = p._to_wav(raw, work / "speech.wav")
with wave.open(str(wav)) as w:
    print(f"  归一化后 = {wav.name}  {wav.stat().st_size} bytes")
    print(f"  采样率={w.getframerate()} Hz  声道={w.getnchannels()}  时长={w.getnframes()/w.getframerate():.2f}s")
print(f"  总耗时 {time.time() - t0:.1f}s")
