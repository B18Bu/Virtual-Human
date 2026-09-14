#!/usr/bin/env python3
"""Linly-Talker 已归位权重的完整性验收（对照 ModelScope 官方清单）

和 verify_weights.sh 的区别：那个是「拉取后、归位前」用来摸清真实目录结构的侦察工具；
这个是在**归位之后**，把仓库里实际生效的权重逐个做 SHA256 校验，回答
「现在跑起来用到的每个权重文件，是不是都和官方快照逐字节一致」。

用法：
    /root/autodl-tmp/conda/envs/linly/bin/python deploy/verify_installed.py [--no-hash]

默认全量 SHA256（18G 约 75 秒）。加 --no-hash 只比大小，秒出，用于快速回归。
"""
import hashlib
import json
import os
import sys
import time

MANIFEST = "/root/autodl-tmp/deploy/ms_manifest.json"
CACHE = "/root/autodl-tmp/models/Kedreamix--Linly-Talker/snapshots/master"
PROJ = "/root/autodl-tmp/Linly-Talker"

# 清单路径前缀 -> 归位后的实际前缀（顺序敏感：长的、特殊的必须排在前面）
RULES = [
    ("checkpoints/CosyVoice_ckpt/CosyVoice-ttsfrd/", f"{PROJ}/CosyVoice/pretrained_models/CosyVoice-ttsfrd/"),
    ("GPT_SoVITS/pretrained_models/",                f"{PROJ}/GPT_SoVITS/pretrained_models/"),
    ("checkpoints/",                                 f"{PROJ}/checkpoints/"),
    ("MuseTalk/",                                    f"{PROJ}/Musetalk/models/"),
    ("gfpgan/",                                      f"{PROJ}/gfpgan/"),
    ("Qwen/",                                        f"{PROJ}/Qwen/"),
    ("Whisper/",                                     f"{PROJ}/Whisper/"),
    ("FunASR/",                                      f"{PROJ}/FunASR/"),
]

# 这些不参与运行，留在快照缓存里即可，不计入验收
NOT_DEPLOYED = {".gitattributes", ".gitmodules", "README.md", "configuration.json"}


def installed_path(p: str):
    for pre, repl in RULES:
        if p.startswith(pre):
            return repl + p[len(pre):]
    return None


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    do_hash = "--no-hash" not in sys.argv
    man = json.load(open(MANIFEST))
    blobs = [f for f in man["Data"]["Files"]
             if f.get("Type") == "blob" or f.get("Mode") != "16384"]

    ok, missing, sizebad, hashbad, skipped = [], [], [], [], []
    used, total = 0, 0
    t0 = time.time()

    for f in blobs:
        p = f["Path"]
        if p in NOT_DEPLOYED:
            skipped.append(p)
            continue
        target = installed_path(p)
        if target is None:
            skipped.append(p)
            continue
        used += 1
        total += f.get("Size", 0)
        if not os.path.exists(target):
            missing.append((p, target))
            continue
        if os.path.getsize(target) != f.get("Size", -1):
            sizebad.append((p, target, f.get("Size"), os.path.getsize(target)))
            continue
        if do_hash and sha256(target) != f.get("Sha256"):
            hashbad.append((p, target))
            continue
        ok.append(p)
        n = len(ok)
        if n % 25 == 0:
            print(f"  ...已校验 {n}/{used}  用时 {time.time() - t0:.0f}s", flush=True)

    print()
    print("=" * 68)
    print(f"参与验收的文件：{used} 个，{total / 1024 ** 3:.2f} GB")
    print(f"  通过                : {len(ok)}")
    print(f"  缺失                : {len(missing)}")
    print(f"  大小不符            : {len(sizebad)}")
    print(f"  SHA256 不符         : {len(hashbad)}   {'（本次未校验哈希）' if not do_hash else ''}")
    print(f"  无需归位（跳过）    : {len(skipped)}  {skipped}")
    print(f"耗时 {time.time() - t0:.0f}s")
    print("=" * 68)

    for p, t in missing:
        print(f"  [缺失]   {p}\n           期望位置 {t}")
    for p, t, e, a in sizebad:
        print(f"  [大小]   {p}\n           期望 {e} 实际 {a} ({t})")
    for p, t in hashbad:
        print(f"  [哈希]   {p}\n           {t}")

    good = not (missing or sizebad or hashbad)
    print()
    print("✅ 全部权重与官方快照一致，可以进入依赖安装阶段。" if good
          else "❌ 存在不一致，上面已逐条列出。")
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
