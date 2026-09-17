"""诊断 3（历史脚本，结论已被推翻）：用**有声**音频重跑 UNet 插桩，看条件是否被响应。

> ⚠️ **2026-09-14 复核：本脚本「UNet 对条件几乎无响应」的结论是错的。**
> 它用的指标是「逐批输出 std 的极差」——**不同内容可以有完全相同的 std**，
> 这个量本来就不适合判断「模型有没有响应条件」。
> 换成**固定同一 latent、只换条件**的对照实验后：真音频 vs 全零条件，
> 输出 latent 相对差 6.6%、解码后下半脸像素差 3~4/255（静帧基线 0.01）——
> **条件确实在驱动输出**。要看最新结论见交接文档 5.8。

（诊断 2 用的是静音音频，结论不可信——静音本来就不会驱动口型。）
"""
import os, sys
import numpy as np
PROJ = "/root/autodl-tmp/Linly-Talker"
os.chdir(PROJ); sys.path.insert(0, PROJ)
import TFG.MuseTalk as MT  # noqa: E402

W = "/root/autodl-tmp/linly-api/outputs/work/a7600a964bf0/"
AVATAR = W + "a7600a964bf0-avatar.mp4"
AUDIO  = W + "speech.wav"

m = MT.MuseTalk_RealTime()
m.init_model()

# ① 音频特征本身是否随时间变化
feat = m.audio_processor.audio2feat(AUDIO)
f = np.asarray(feat, dtype=np.float32)
print(f">>> 音频特征 shape={f.shape} 时间步均值极差={f.reshape(f.shape[0],-1).mean(axis=1).ptp():.4f}")

# ② UNet 的输入条件 vs 输出
unet = m.unet.model
orig = unet.forward
log = []
def wrapped(sample, timestep, encoder_hidden_states=None, **kw):
    out = orig(sample, timestep, encoder_hidden_states=encoder_hidden_states, **kw)
    log.append((float(encoder_hidden_states.float().std()),
                float(out.sample.float().std()),
                float(out.sample.float().mean())))
    return out
unet.forward = wrapped
m.prepare_material(AVATAR, bbox_shift=0, progress=lambda *a, **k: None)
m.inference_noprepare(AUDIO, AVATAR, bbox_shift=0, fps=25, progress=lambda *a, **k: None)

a = np.array(log)
print(f">>> UNet 调用 {len(a)} 次")
print(f">>> 条件 std   : 首={a[0,0]:.4f} 末={a[-1,0]:.4f} 极差={a[:,0].ptp():.4f}")
print(f">>> 输出 std   : 首={a[0,1]:.4f} 末={a[-1,1]:.4f} 极差={a[:,1].ptp():.6f}")
print(f">>> 输出 mean  : 首={a[0,2]:.4f} 末={a[-1,2]:.4f} 极差={a[:,2].ptp():.6f}")
