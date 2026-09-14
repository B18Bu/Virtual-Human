"""诊断 2：插桩 UNet 的 forward，看音频条件与输出 latent 到底哪一环是常数。"""
import os, sys
import numpy as np
PROJ = "/root/autodl-tmp/Linly-Talker"
os.chdir(PROJ); sys.path.insert(0, PROJ)
import TFG.MuseTalk as MT  # noqa: E402

AVATAR = "/root/autodl-tmp/linly-api/outputs/work/f3e03b2c2be4/f3e03b2c2be4-avatar.mp4"
AUDIO  = "/root/autodl-tmp/linly-api/outputs/work/f3e03b2c2be4/speech.wav"

m = MT.MuseTalk_RealTime()
m.init_model()
unet = m.unet.model
orig = unet.forward
log = []

def wrapped(sample, timestep, encoder_hidden_states=None, **kw):
    out = orig(sample, timestep, encoder_hidden_states=encoder_hidden_states, **kw)
    log.append((
        float(sample.float().mean()), float(sample.float().std()),
        float(encoder_hidden_states.float().mean()), float(encoder_hidden_states.float().std()),
        float(out.sample.float().mean()), float(out.sample.float().std()),
    ))
    return out

unet.forward = wrapped
m.prepare_material(AVATAR, bbox_shift=0, progress=lambda *a, **k: None)
m.inference_noprepare(AUDIO, AVATAR, bbox_shift=0, fps=25, progress=lambda *a, **k: None)

a = np.array(log)
print(">>> UNet 调用次数:", len(a))
names = ["latent.mean","latent.std","cond.mean","cond.std","out.mean","out.std"]
for j, n in enumerate(names):
    print(f">>> {n:12s} 首={a[0,j]:9.4f} 末={a[-1,j]:9.4f} 极差={a[:,j].ptp():10.6f}")
