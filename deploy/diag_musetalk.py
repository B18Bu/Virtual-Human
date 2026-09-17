"""诊断（历史脚本，结论已被推翻）：MuseTalk 产出的视频为什么没有口型动作。

> ⚠️ **2026-09-14 复核：这个脚本当时给出的「MuseTalk 没有口型动作」是误判。**
> 它跑在 TTS 静音 bug 修复之前，喂进去的是**静音音频**——静音当然不动嘴。
> 修复后同一个流程产出的口型正常（DWPose 唇部关键点与音频包络在 0 滞后相关 r=0.35）。
> 本脚本保留作排查过程记录，**结论不要引用**；要看最新结论见交接文档 5.8。

不改项目源码——在运行时把 TFG.MuseTalk 命名空间里的 get_image_blending 包一层，
记录每个 res_frame（VAE 解出的口型帧）的统计量，判断「生成的口型帧到底有没有变化」。
"""
import os, sys
import numpy as np

PROJ = "/root/autodl-tmp/Linly-Talker"
os.chdir(PROJ)
sys.path.insert(0, PROJ)

import TFG.MuseTalk as MT  # noqa: E402

_orig = MT.get_image_blending
stats = []


def wrapped(ori_frame, res_frame, bbox, mask, mask_crop_box):
    a = np.asarray(res_frame)
    stats.append((float(a.mean()), float(a.std()), float(a.max()), tuple(a.shape)))
    return _orig(ori_frame, res_frame, bbox, mask, mask_crop_box)


MT.get_image_blending = wrapped

AVATAR = "/root/autodl-tmp/linly-api/outputs/work/f3e03b2c2be4/f3e03b2c2be4-avatar.mp4"
AUDIO = "/root/autodl-tmp/linly-api/outputs/work/f3e03b2c2be4/speech.wav"

m = MT.MuseTalk_RealTime()
m.init_model()
m.prepare_material(AVATAR, bbox_shift=0, progress=lambda *a, **k: None)
out = m.inference_noprepare(AUDIO, AVATAR, bbox_shift=0, fps=25, progress=lambda *a, **k: None)

print(">>> 产物:", out, os.path.getsize(out) if out and os.path.exists(out) else "N/A")
print(">>> res_frame 个数:", len(stats))
for i in (0, 1, 2, len(stats) // 2, len(stats) - 2, len(stats) - 1):
    if 0 <= i < len(stats):
        print(f">>>   [{i:3d}] mean={stats[i][0]:8.3f} std={stats[i][1]:8.3f} max={stats[i][2]:8.1f} shape={stats[i][3]}")
if len(stats) > 1:
    arr = np.array([s[:3] for s in stats])
    print(f">>> res_frame 逐帧均值序列的前 12 个: {np.round(arr[:12, 0], 3)}")
    print(f">>>   mean 的极差={arr[:,0].ptp():.4f}  std 的极差={arr[:,1].ptp():.4f}")
