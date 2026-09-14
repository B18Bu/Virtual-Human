"""Linly-Talker 依赖环境体检：按模块分组导入，最后测 MuseTalk 的关键点链路。

直接写文件而不是 heredoc —— 后台化时 heredoc 的 stdin 会丢。
"""
import importlib
import sys

GROUPS = {
    "深度学习栈": ["torch", "torchvision", "torchaudio", "mmcv", "mmcv.ops",
                   "mmengine", "mmdet", "mmpose"],
    "SadTalker 链路": ["basicsr", "gfpgan", "facexlib", "face_alignment",
                       "kornia", "yacs", "scipy", "skimage", "sklearn"],
    # 注意：WeTextProcessing 的发行名与导入名不同，导入名是 tn（见交接文档坑 12）
    "GPT-SoVITS / TTS": ["pytorch_lightning", "lightning", "onnxruntime",
                         "pyopenjtalk", "jieba_fast", "cn2an", "pypinyin",
                         "g2p_en", "LangSegment", "conformer", "tn"],
    "ASR / LLM": ["funasr", "modelscope", "whisper", "transformers", "diffusers",
                  "accelerate", "openai", "tiktoken", "edge_tts"],
    "音视频 / 图像": ["librosa", "soundfile", "cv2", "imageio", "moviepy",
                      "pydub", "resampy", "ffmpeg"],
    "服务侧": ["fastapi", "uvicorn", "pydantic", "httpx", "gradio"],
}

bad_total = 0
for name, mods in GROUPS.items():
    bad = []
    for m in mods:
        try:
            importlib.import_module(m)
        except Exception as e:  # noqa: BLE001 - 体检就是要把失败原因打出来
            bad.append(f"{m}({type(e).__name__}: {str(e)[:70]})")
    status = f"{len(mods)-len(bad)}/{len(mods)} OK"
    print(f"{name:18s} {status}" + (f"   失败: {bad}" if bad else ""), flush=True)
    bad_total += len(bad)

print(f"\n合计失败模块数: {bad_total}", flush=True)

# MuseTalk 真实调用点：DW Pose 关键点检测链路
sys.path.insert(0, "/root/autodl-tmp/Linly-Talker/Musetalk")
try:
    import musetalk.utils.preprocessing as pp  # noqa: F401
    print("MuseTalk 关键点链路 (musetalk.utils.preprocessing) 导入 OK", flush=True)
except Exception as e:  # noqa: BLE001
    print(f"MuseTalk preprocessing 导入失败: {type(e).__name__}: {str(e)[:200]}", flush=True)
