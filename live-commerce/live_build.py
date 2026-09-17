#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
批量渲染 + 拼接播放列表

把 script.json 里的每一段话术，用指定数字人档案渲染成 mp4，再拼成一条可循环
播放的长视频。

关键设计：
  * **可断点续跑**——已渲染的段落（build/<segment_id>.mp4 存在且非空）直接跳过。
    25 段要跑十几分钟，中途断了不用从头来。
  * **渲染完立刻落盘**——6006 的产物有 6 小时 TTL，不能攒着最后一起下。
  * **串行提交**——服务是单 worker（MAX_WORKERS=1），并发提交只会排队并可能 503。
  * **拼接优先免重编码**——同一条管线产出的流参数一致，concat demuxer + `-c copy`
    即可；失败才回退重编码。

用法：
    python live_build.py -i script.json --profile xiaomei
    python live_build.py -i script.json --profile xiaomei --limit 3   # 先试 3 段
    python live_build.py -i script.json --profile xiaomei --loop 3    # 循环 3 遍
"""
import argparse
import array
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# 复用 dh.py 里已验证的「档案 → voice 串 / 上传」逻辑，避免两处实现漂移
sys.path.insert(0, "/root/autodl-tmp/digital-humans")
try:
    import dh
except ImportError as e:
    raise SystemExit(f"无法导入 dh.py（应位于 /root/autodl-tmp/digital-humans/dh.py）：{e}")

HERE = Path(__file__).resolve().parent
FFMPEG = "/root/autodl-tmp/conda/envs/linly/bin/ffmpeg"
FFPROBE = "/root/autodl-tmp/conda/envs/linly/bin/ffprobe"


def probe_duration(p: Path) -> float:
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(p)], capture_output=True, text=True, timeout=60)
        return float(out.stdout.strip())
    except Exception:
        return 0.0


def has_audio(p: Path) -> bool:
    """
    量 PCM 峰值判断音轨是否真有声音。

    必须真的解码成样本来看：EdgeTTS 的 VOLUME 参数是反的（TTS/EdgeTTS.py:146
    做 volume = 100 - volume），传 0 会得到一条**格式合法、长度正常、内容全零**
    的静音轨——ffprobe 报告一切正常，只有量样本才发现。
    """
    try:
        raw = subprocess.run(
            [FFMPEG, "-v", "error", "-i", str(p), "-f", "s16le", "-ac", "1",
             "-ar", "16000", "-"], capture_output=True, timeout=120).stdout
        if not raw:
            return False
        samples = array.array("h")                  # s16le 有符号 16 位
        samples.frombytes(raw[: len(raw) // 2 * 2])
        return len(samples) > 0 and max(abs(s) for s in samples) > 100
    except Exception:
        return False


def render_one(segment: dict, prof: dict, pdir: Path, out: Path) -> Path:
    img = pdir / prof["avatar"]["image"]
    vstr = dh.voice_string(prof, pdir)
    task = dh._post_multipart(
        f"{dh.API_BASE}/api/v1/tasks/upload",
        {"text": segment["text"], "mode": prof["avatar"]["engine"], "voice": vstr},
        {"image": img},
    )
    tid = task["task_id"]
    t0 = time.time()
    while True:
        st = dh._get_json(f"{dh.API_BASE}/api/v1/tasks/{tid}")
        s = st["status"]
        if s == "succeeded":
            break
        if s in ("failed", "canceled"):
            raise RuntimeError(f"任务{s}：{st.get('error')}")
        if time.time() - t0 > 900:
            raise RuntimeError("渲染超时（15 分钟）")
        time.sleep(3)

    # 立刻下载——产物 6 小时 TTL。
    # ⚠️ 不能自己拼 `{API_BASE}/api/v1/files/{tid}.mp4`：产物下载要签名，
    # 自拼的 URL 没有签名会 404。用 dh.local_result_url() 从 result_url 换 host，
    # 路径与签名（查询串）都照搬。
    req = urllib.request.Request(dh.local_result_url(st))
    with urllib.request.urlopen(req, timeout=300) as r:
        out.write_bytes(r.read())
    return out


def concat(list_file: Path, out: Path) -> bool:
    """优先免重编码；流参数不一致时回退重编码。返回是否用了重编码。"""
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
           "-f", "concat", "-safe", "0", "-i", str(list_file),
           "-c", "copy", str(out)]
    if subprocess.run(cmd).returncode == 0 and out.is_file() and out.stat().st_size > 0:
        return False
    print("   ↻ 免重编码拼接失败，回退重编码（较慢）…")
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
           "-f", "concat", "-safe", "0", "-i", str(list_file),
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", str(out)]
    if subprocess.run(cmd).returncode != 0:
        raise RuntimeError("重编码拼接也失败了")
    return True


def main():
    ap = argparse.ArgumentParser(description="批量渲染直播播放列表")
    ap.add_argument("-i", "--input", default="script.json")
    ap.add_argument("--profile", required=True, help="数字人档案 id")
    ap.add_argument("--limit", type=int, default=0, help="只渲染前 N 段（试跑用）")
    ap.add_argument("--loop", type=int, default=1, help="拼接后循环几遍")
    ap.add_argument("--outdir", default=None, help="产物目录，默认 build/<profile>/")
    ap.add_argument("--force", action="store_true", help="忽略已有产物，全部重渲")
    a = ap.parse_args()

    script = json.loads(Path(a.input).read_text(encoding="utf-8"))
    segments = script.get("segments", [])
    if not segments:
        raise SystemExit("script.json 里没有 segments")
    if a.limit:
        segments = segments[:a.limit]

    pid = a.profile
    pdir = Path("/root/autodl-tmp/digital-humans") / pid
    if not (pdir / "profile.json").is_file():
        raise SystemExit(f"档案不存在：{pdir}（先用 dh.py create 建）")
    prof = json.loads((pdir / "profile.json").read_text(encoding="utf-8"))

    outdir = Path(a.outdir) if a.outdir else (HERE / "build" / pid)
    segdir = outdir / "segments"
    segdir.mkdir(parents=True, exist_ok=True)

    print(f"数字人档案：{pid}（{prof.get('display_name','')}）")
    print(f"引擎：{prof['avatar']['engine']}   形象：{prof['avatar']['image']}")
    print(f"待渲染：{len(segments)} 段   产物目录：{outdir}\n")

    done, skipped, failed = [], 0, []
    t_start = time.time()
    for i, seg in enumerate(segments, 1):
        sid = seg.get("id") or f"seg{i:03d}"
        dst = segdir / f"{sid}.mp4"
        tag = f"[{i:>2}/{len(segments)}] {sid:<18}"

        if dst.is_file() and dst.stat().st_size > 0 and not a.force:
            d = probe_duration(dst)
            print(f"  ⏭  {tag} 已存在  {d:.1f}s  {dst.stat().st_size//1024}KB")
            skipped += 1
            seg["video"] = str(dst.relative_to(outdir))
            done.append(seg)
            continue

        try:
            t0 = time.time()
            render_one(seg, prof, pdir, dst)
            d, sz = probe_duration(dst), dst.stat().st_size
            ok_audio = has_audio(dst)
            flag = "✅" if sz > 100_000 and ok_audio else "⚠️ "
            extra = "" if ok_audio else "  ← 音轨疑似静音！"
            print(f"  {flag} {tag} {time.time()-t0:5.1f}s  {d:5.1f}s  {sz//1024:>4}KB{extra}")
            seg["video"] = str(dst.relative_to(outdir))
            seg["rendered_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            done.append(seg)
        except Exception as e:
            print(f"  ❌ {tag} 失败：{e}")
            failed.append((sid, str(e)))

    if not done:
        raise SystemExit("\n没有任何可用片段，终止。")

    # ── 拼接 ──
    print(f"\n拼接 {len(done)} 段…")
    list_file = outdir / "concat.txt"
    lines = []
    for _ in range(max(1, a.loop)):
        for seg in done:
            p = (outdir / seg["video"]).resolve()
            lines.append(f"file '{p}'")          # concat demuxer 要求转义单引号路径
    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    playlist = outdir / "playlist.mp4"
    reencoded = concat(list_file, playlist)
    total = probe_duration(playlist)
    mb = playlist.stat().st_size / 1024 / 1024

    script.setdefault("meta", {})["built_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    script["segments"] = done
    (outdir / "script.built.json").write_text(
        json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{'─'*66}")
    print(f"✅ 播放列表：{playlist}")
    print(f"   时长 {total/60:.1f} 分钟（{total:.0f} 秒）  体积 {mb:.1f} MB"
          + ("  [重编码]" if reencoded else "  [免重编码]"))
    print(f"   片段目录：{segdir}")
    print(f"   concat 清单：{list_file}（可直接给 OBS / ffmpeg 用）")
    print(f"   耗时：{(time.time()-t_start)/60:.1f} 分钟"
          + (f"（跳过 {skipped} 段已渲染）" if skipped else ""))
    if failed:
        print(f"\n⚠️  {len(failed)} 段失败，未计入播放列表：")
        for sid, err in failed:
            print(f"   {sid}：{err[:90]}")
    print(f"\n推流示例（自行替换地址）：")
    print(f"   {FFMPEG} -re -stream_loop -1 -i {playlist} -c copy -f flv rtmp://<你的推流地址>")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
