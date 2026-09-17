#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
数字人档案管理（Digital Human Profile）

仓库外工具，零侵入 Linly-Talker。把「确认好的形象 + 音色 + 人设」固化成一个
可复用、可校验、可迁移的档案。

用法：
    python dh.py create --id xiaomei --name "小美·美妆主播" \
        --image face.png --voice-id xiaomei_voice
    python dh.py list
    python dh.py show xiaomei
    python dh.py verify xiaomei
    python dh.py render xiaomei --text "欢迎来到直播间"
    python dh.py export xiaomei --with-weights --out /tmp/xiaomei.tar.gz
"""
import argparse
import base64
import hashlib
import json
import re
import shutil
import sys
import tarfile
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

DH_ROOT = Path("/root/autodl-tmp/digital-humans")
VOICES_ROOT = Path("/root/autodl-tmp/voices")
VOICE_WORK_ROOT = Path("/root/autodl-tmp/linly-api/voice_work")
API_BASE = "http://127.0.0.1:6006"
API_KEY_FILE = Path("/root/autodl-tmp/linly-api/.api_key")

VALID_ID = re.compile(r"^[A-Za-z0-9_]{1,32}$")
VALID_ENGINE = ("sadtalker", "musetalk", "wav2lip")
IMAGE_SUFFIX = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}


# ---------------- 基础工具 ----------------

def api_key() -> str:
    return API_KEY_FILE.read_text(encoding="utf-8").strip()


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def profile_dir(pid: str) -> Path:
    if not VALID_ID.match(pid or ""):
        raise SystemExit(f"非法档案名 {pid!r}：只允许字母/数字/下划线，1~32 位")
    return DH_ROOT / pid


def load_profile(pid: str) -> dict:
    p = profile_dir(pid) / "profile.json"
    if not p.is_file():
        raise SystemExit(f"档案不存在：{p}")
    return json.loads(p.read_text(encoding="utf-8"))


def save_profile(pid: str, prof: dict) -> None:
    prof["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S+08:00")
    (profile_dir(pid) / "profile.json").write_text(
        json.dumps(prof, ensure_ascii=False, indent=2), encoding="utf-8")


def wav_duration(p: Path) -> float:
    with wave.open(str(p), "rb") as f:
        return f.getnframes() / float(f.getframerate() or 1)


# ---------------- 参考音频自动挑选 ----------------

def pick_reference(voice_id: str):
    """
    从训练语料里挑一段 3~10 秒的切片 + 它的文本。

    补掉「音色不自包含」缺口：训练产物 meta.json 里只有 source_audio（原始长
    音频）和 work_dir，没有可直接当 prompt 的短片段及其文本，导致 finetuned:
    语法强制调用方每次自带参考音频。
    """
    work = VOICE_WORK_ROOT / voice_id
    slices_dir = work / "slicer_opt"
    tr = work / "transcript.txt"
    if not slices_dir.is_dir() or not tr.is_file():
        return None

    texts = {}
    for line in tr.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        name, _, txt = line.partition("\t")
        if name and txt.strip():
            texts[Path(name).stem] = txt.strip()
    if not texts:
        return None

    best = None  # (score, path, text)
    for w in sorted(slices_dir.glob("*.wav")):
        txt = texts.get(w.stem)
        if not txt:
            continue
        try:
            dur = wav_duration(w)
        except Exception:
            continue
        # GPT-SoVITS 硬约束：短于 3 秒或长于 10 秒会 raise OSError
        if 3.0 <= dur <= 10.0:
            score = abs(dur - 5.5)          # 偏好 4~7 秒
            if best is None or score < best[0]:
                best = (score, w, txt)
    return (best[1], best[2]) if best else None


# ---------------- 子命令 ----------------

def cmd_create(a):
    pid = a.id
    d = profile_dir(pid)
    if d.exists() and not a.force:
        raise SystemExit(f"档案已存在：{d}（要覆盖加 --force）")

    # ── 阶段一：全部校验，不落任何盘 ──
    # 校验必须在建目录之前做完，否则中途失败会留下空壳档案目录。

    src_img = Path(a.image).expanduser().resolve()
    if not src_img.is_file():
        raise SystemExit(f"形象图不存在：{src_img}")
    if src_img.suffix.lower() not in IMAGE_SUFFIX:
        raise SystemExit(f"形象图后缀不支持：{src_img.suffix}（允许 {sorted(IMAGE_SUFFIX)}）")
    try:
        from PIL import Image
        with Image.open(src_img) as im:
            w, h = im.size
    except Exception as e:
        raise SystemExit(f"读形象图失败：{e}")

    eng = a.voice_engine
    voice = {"engine": eng}
    ref_src = ref_text = None

    if eng == "finetuned":
        if not a.voice_id:
            raise SystemExit("--voice-engine finetuned 必须给 --voice-id")
        vd = VOICES_ROOT / a.voice_id
        gpt, sovits = vd / "gpt.ckpt", vd / "sovits.pth"
        if not (gpt.is_file() and sovits.is_file()):
            raise SystemExit(
                f"音色 {a.voice_id} 权重不全：\n  需要 {gpt}\n  和   {sovits}\n"
                f"  先训练，或查 GET /api/v1/voices/{a.voice_id}")
        voice.update(voice_id=a.voice_id,
                     weights={"gpt": str(gpt), "sovits": str(sovits)})
        if a.ref_audio:
            ref_src = Path(a.ref_audio).expanduser().resolve()
            if not ref_src.is_file():
                raise SystemExit(f"参考音频不存在：{ref_src}")
            ref_text = a.ref_text or ""
        else:
            got = pick_reference(a.voice_id)
            if got:
                ref_src, ref_text = got
                print(f"ℹ️  自动挑选参考片段：{ref_src.name} "
                      f"({wav_duration(ref_src):.1f}s)  「{ref_text[:24]}…」")
        if ref_src is None:
            raise SystemExit(
                "找不到可用的 3~10 秒参考音频。\n"
                "  自动挑选失败（训练语料缺失，或没有 3~10 秒的切片）\n"
                "  请手工指定：--ref-audio x.wav --ref-text '该音频的文字'")

    elif eng == "gptsovits":
        if not a.ref_audio:
            raise SystemExit("--voice-engine gptsovits 必须给 --ref-audio")
        ref_src = Path(a.ref_audio).expanduser().resolve()
        if not ref_src.is_file():
            raise SystemExit(f"参考音频不存在：{ref_src}")
        ref_text = a.ref_text or ""

    else:  # cosyvoice / edge
        default_spk = "中文女" if eng == "cosyvoice" else "zh-CN-XiaoxiaoNeural"
        voice.update(speaker=a.speaker or default_spk)

    # ── 阶段二：落盘 ──
    d.mkdir(parents=True, exist_ok=True)
    dst_img = d / f"avatar{src_img.suffix.lower()}"
    if src_img != dst_img:
        shutil.copy2(src_img, dst_img)

    if ref_src is not None:
        shutil.copy2(ref_src, d / "ref.wav")
        (d / "ref.txt").write_text(ref_text, encoding="utf-8")
        voice.update(ref_audio="ref.wav", ref_text=ref_text, lang=a.lang)

    prof = {
        "profile_id": pid,
        "display_name": a.name or pid,
        "version": 1,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "avatar": {
            "image": dst_img.name,
            "sha256": sha256(dst_img),
            "width": w, "height": h,
            "engine": a.engine,
            "preprocess": "full",   # 禁止改成 crop：会退化成 256x256 浮空脸
            "still_mode": False,
            "enhancer": False,
        },
        "voice": voice,
        "persona": {
            "system_prompt": a.system_prompt or "",
            "products_file": "products.json" if (d / "products.json").is_file() else None,
            "disclosure": a.disclosure or "",
        },
        "render": {"fps": 20, "size": 256},
    }
    save_profile(pid, prof)

    print(f"✅ 档案已建：{d}")
    print(f"   形象  {dst_img.name}  {w}x{h}  sha256={prof['avatar']['sha256'][:16]}…")
    vn = voice.get("voice_id") or voice.get("speaker") or voice.get("engine")
    print(f"   音色  {eng}  {vn}")
    if voice.get("ref_text"):
        print(f"   参考  ref.wav / {voice['ref_text'][:28]}")
    print(f"\n试片：python {Path(__file__).name} render {pid} --text '大家好'")


def cmd_list(a):
    if not DH_ROOT.is_dir():
        print("（还没有任何档案）")
        return
    rows = []
    for d in sorted(DH_ROOT.iterdir()):
        p = d / "profile.json"
        if p.is_file():
            try:
                rows.append(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                pass
    if not rows:
        print("（还没有任何档案）")
        return
    print(f"{'档案':<18}{'名称':<22}{'引擎':<11}{'音色':<24}形象")
    print("-" * 92)
    for r in rows:
        av, vo = r.get("avatar", {}), r.get("voice", {})
        vn = vo.get("voice_id") or vo.get("speaker") or vo.get("engine", "")
        print(f"{r['profile_id']:<18}{str(r.get('display_name',''))[:20]:<22}"
              f"{str(av.get('engine','')):<11}{str(vn)[:22]:<24}{av.get('image','')}")


def cmd_show(a):
    print(json.dumps(load_profile(a.id), ensure_ascii=False, indent=2))


def cmd_verify(a):
    prof = load_profile(a.id)
    d = profile_dir(a.id)
    ok = True

    img = d / prof["avatar"]["image"]
    if not img.is_file():
        print(f"❌ 形象图缺失：{img}"); ok = False
    elif sha256(img) != prof["avatar"]["sha256"]:
        print(f"❌ 形象图 sha256 不匹配（图被改过）：{img}"); ok = False
    else:
        print(f"✅ 形象图完好  {img.name}  {prof['avatar']['width']}x{prof['avatar']['height']}")
    if prof["avatar"].get("preprocess") != "full":
        print(f"⚠️  preprocess={prof['avatar'].get('preprocess')!r}（应为 'full'）")

    vo = prof.get("voice", {})
    if vo.get("engine") == "finetuned":
        for k, p in (vo.get("weights") or {}).items():
            if Path(p).is_file():
                print(f"✅ 音色权重 {k:<7}{p}")
            else:
                print(f"❌ 音色权重 {k} 缺失：{p}"); ok = False
    if vo.get("engine") in ("finetuned", "gptsovits"):
        ref = d / vo.get("ref_audio", "ref.wav")
        if ref.is_file():
            try:
                dur = wav_duration(ref)
                flag = "✅" if 3.0 <= dur <= 10.0 else "⚠️ "
                print(f"{flag} 参考音频  {ref.name}  {dur:.1f}s"
                      + ("" if 3.0 <= dur <= 10.0 else "（必须 3~10 秒）"))
            except Exception:
                print(f"✅ 参考音频  {ref.name}")
        else:
            print(f"❌ 参考音频缺失：{ref}"); ok = False

    sys.exit(0 if ok else 1)


def voice_string(prof: dict, d: Path) -> str:
    """把档案展开成 API 的 voice 串。"""
    vo = prof["voice"]
    eng = vo.get("engine", "edge")
    if eng == "finetuned":
        ref = (d / vo["ref_audio"]).resolve()
        return f"finetuned:{vo['voice_id']}|{ref}|{vo['ref_text']}|{vo.get('lang','中文')}"
    if eng == "gptsovits":
        ref = (d / vo["ref_audio"]).resolve()
        return f"gptsovits:{ref}|{vo.get('ref_text','')}|{vo.get('lang','中文')}"
    if eng == "cosyvoice":
        return f"cosyvoice:{vo.get('speaker','中文女')}"
    return f"edge:{vo.get('speaker','zh-CN-XiaoxiaoNeural')}"


def _post_multipart(url: str, fields: dict, files: dict) -> dict:
    """纯标准库 multipart 上传，避免依赖 requests。"""
    boundary = "----dh" + base64.b16encode(str(time.time_ns()).encode()).decode()
    buf = []
    for k, v in fields.items():
        buf.append(f"--{boundary}\r\n"
                   f'Content-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode("utf-8"))
    for k, p in files.items():
        p = Path(p)
        ctype = "image/png" if p.suffix.lower() == ".png" else "application/octet-stream"
        buf.append(f"--{boundary}\r\n"
                   f'Content-Disposition: form-data; name="{k}"; filename="{p.name}"\r\n'
                   f"Content-Type: {ctype}\r\n\r\n".encode("utf-8"))
        buf.append(p.read_bytes())
        buf.append(b"\r\n")
    buf.append(f"--{boundary}--\r\n".encode())
    body = b"".join(buf)

    req = urllib.request.Request(url, data=body, method="POST", headers={
        "X-API-Key": api_key(),
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    })
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"X-API-Key": api_key()})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def cmd_render(a):
    prof = load_profile(a.id)
    d = profile_dir(a.id)
    img = d / prof["avatar"]["image"]
    engine = a.engine or prof["avatar"]["engine"]
    vstr = voice_string(prof, d)

    task = _post_multipart(
        f"{API_BASE}/api/v1/tasks/upload",
        {"text": a.text, "mode": engine, "voice": vstr},
        {"image": img},
    )
    tid = task["task_id"]
    print(f"提交成功  task={tid}  引擎={engine}")
    print(f"  voice={vstr}")

    t0 = time.time()
    while True:
        st = _get_json(f"{API_BASE}/api/v1/tasks/{tid}")
        s = st["status"]
        print(f"  [{time.time()-t0:6.1f}s] {s:<10} {st.get('progress',0):.2f}  {st.get('stage','')}")
        if s == "succeeded":
            break
        if s in ("failed", "canceled"):
            raise SystemExit(f"任务{s}：{st.get('error')}")
        time.sleep(3)

    # result_url 是带公网域名的绝对 URL；走本地端点更快且不走公网
    out = Path(a.out).expanduser() if a.out else (d / "preview.mp4")
    req = urllib.request.Request(f"{API_BASE}/api/v1/files/{tid}.mp4")
    with urllib.request.urlopen(req, timeout=300) as r:
        out.write_bytes(r.read())

    size = out.stat().st_size
    print(f"\n✅ 产物  {out}  {size/1024:.0f} KB  耗时 {st.get('elapsed_seconds', time.time()-t0):.1f}s")
    if size < 200_000:
        print("   ⚠️ 偏小（正常 260~370KB）——检查 preprocess 是否被改成了 crop")


def cmd_export(a):
    d = profile_dir(a.id)
    prof = load_profile(a.id)
    out = Path(a.out).expanduser() if a.out else Path(f"/tmp/{a.id}-dh.tar.gz")
    out.parent.mkdir(parents=True, exist_ok=True)

    with tarfile.open(out, "w:gz") as tf:
        for f in (prof["avatar"]["image"], "ref.wav", "ref.txt", "products.json", "preview.mp4"):
            p = d / f
            if p.is_file():
                tf.add(p, arcname=f"{a.id}/{f}")
        tf.add(d / "profile.json", arcname=f"{a.id}/profile.json")
        if a.with_weights and prof.get("voice", {}).get("engine") == "finetuned":
            vd = VOICES_ROOT / prof["voice"]["voice_id"]
            for f in ("gpt.ckpt", "sovits.pth", "meta.json"):
                if (vd / f).is_file():
                    tf.add(vd / f, arcname=f"{a.id}/weights/{f}")

    print(f"✅ 已导出  {out}  {out.stat().st_size/1024/1024:.1f} MB")
    if not a.with_weights:
        print("   （未含音色权重；跨机器迁移请加 --with-weights）")


def main():
    ap = argparse.ArgumentParser(description="数字人档案管理")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="从形象图 + 音色建档案")
    c.add_argument("--id", required=True)
    c.add_argument("--name", default=None)
    c.add_argument("--image", required=True)
    c.add_argument("--engine", default="sadtalker", choices=list(VALID_ENGINE))
    c.add_argument("--voice-engine", default="finetuned",
                   choices=["finetuned", "gptsovits", "cosyvoice", "edge"])
    c.add_argument("--voice-id", default=None, help="finetuned 时的音色名")
    c.add_argument("--speaker", default=None, help="cosyvoice/edge 时的说话人")
    c.add_argument("--ref-audio", default=None, help="留空则从训练语料自动挑")
    c.add_argument("--ref-text", default=None)
    c.add_argument("--lang", default="中文")
    c.add_argument("--system-prompt", default=None)
    c.add_argument("--disclosure", default="本直播间由 AI 数字人主播")
    c.add_argument("--force", action="store_true")
    c.set_defaults(fn=cmd_create)

    p = sub.add_parser("list", help="列出所有档案")
    p.set_defaults(fn=cmd_list)

    for name, fn, hlp in (("show", cmd_show, "查看档案 JSON"),
                          ("verify", cmd_verify, "校验档案完整性")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("id")
        p.set_defaults(fn=fn)

    r = sub.add_parser("render", help="用档案出片")
    r.add_argument("id")
    r.add_argument("--text", required=True)
    r.add_argument("--engine", default=None, help="覆盖档案里的引擎")
    r.add_argument("--out", default=None)
    r.set_defaults(fn=cmd_render)

    e = sub.add_parser("export", help="打包档案以便迁移")
    e.add_argument("id")
    e.add_argument("--out", default=None)
    e.add_argument("--with-weights", action="store_true")
    e.set_defaults(fn=cmd_export)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
