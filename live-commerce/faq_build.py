#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
FAQ 片段预渲染 + 建索引

把 script.json 里的 faq 段落渲染成独立片段，转成 MPEG-TS（供 FIFO 无缝喂流），
并生成匹配用的 faq_index.json。

为什么从 script.json 取而不是直接从 products.json：
    script.json 里的段落**已经过 compliance 检查**。直接从商品库生成会绕过合规闸门，
    可能出现「违规话术被预渲染成片段反复播放」——比实时生成还危险。

用法：
    python faq_build.py -i script.json --profile xiaomei
    python faq_build.py -i script.json --profile xiaomei --force      # 全部重渲
    python faq_build.py -i script.json --list                         # 只看索引
"""
import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import compliance
from live_build import render_one, FFMPEG, probe_duration, has_audio

HERE = Path(__file__).resolve().parent
DIGITAL_HUMANS = Path("/root/autodl-tmp/digital-humans")


# ── 关键词抽取 ────────────────────────────────────────────────────────

# 电商直播间的通用同义表述。命中任一成员就把整组加进去。
# 这张表是匹配质量的主要来源——比分词靠谱，也更好维护。
SYNONYM_GROUPS = [
    ["敏感肌", "敏感", "过敏", "泛红", "刺激", "耐受", "痘痘肌"],
    ["多少钱", "价格", "几块", "多少米", "贵不贵", "优惠", "划算", "便宜", "活动价"],
    ["怎么用", "用法", "使用方法", "怎么涂", "步骤", "怎么吃", "怎么喝", "怎么戴"],
    ["多久", "多长时间", "几天", "多长", "什么时候", "见效"],
    ["孕妇", "怀孕", "孕期", "哺乳", "宝妈"],
    ["保存", "存放", "保质期", "过期", "怎么放", "储存", "冷藏"],
    ["能用多久", "用多久", "能用多长", "能用几次", "耐用"],
    ["适合", "适合谁", "什么人", "人群", "能用吗", "可以用吗"],
    ["成分", "配方", "含什么", "有什么", "添加"],
    ["产地", "哪里产", "哪里出", "来自"],
    ["规格", "容量", "尺寸", "多重", "多大", "多少克", "多少毫升"],
    ["发货", "物流", "什么时候发", "几天到", "包邮"],
    ["退换", "退款", "退货", "七天", "售后"],
    ["怎么洗", "能洗吗", "清洗", "打理", "保养"],
    ["味道", "好吃吗", "好喝吗", "口感", "香不香", "苦不苦"],
]

# 疑问句的常见外壳，抽关键词时去掉
STOP_PATTERNS = [
    r"^有朋友问", r"^请问", r"^问一下", r"^想问", r"^我想问",
    r"吗$", r"呢$", r"吧$", r"啊$", r"的$", r"了$",
    r"[?？。，,、！!]+",
]
_STOP_RE = re.compile("|".join(STOP_PATTERNS))
# 高频虚词 / 通用疑问碎片，出现在关键词里就丢掉。
#
# 「会不会」「不会」这类要特别小心：它们是**通用疑问外壳**，几乎任何问题都能套。
# 实测教训——「大热天躺上去会不会捂汗」靠 `会不会`+`不会` 命中了「会不会太软」，
# 是典型的假阳性。而计划文档 §7 明确写了**误匹配比漏答更伤**，所以宁可把它们
# 整个拉黑，让这类弹幕走「未命中」而不是「答非所问」。
_STOPWORDS = {"这个", "那个", "一下", "可以", "能不能", "是不是", "有没有",
              "怎么", "什么", "多少", "用吗", "能用", "的话", "还是",
              "会不会", "不会", "会不", "行不行", "好不好", "对不对",
              "怎么样", "如何", "要不要", "有没有用"}


def _normalize_for_kw(s: str) -> str:
    return compliance.normalize(s)[0]


def _content_words(text: str) -> list[str]:
    """
    jieba 分词后只留实词（长度 ≥2，非虚词）。

    为什么要分词：中文定长滑窗 n-gram 会产出大量跨词边界的碎片
    （「多高的人合适」→「高的人合」「的人合」「人合」），观众打「的人」
    就会误匹配。jieba 已在环境里（0.42.1），直接用。
    """
    try:
        import jieba
        jieba.setLogLevel(60)          # 关掉「Building prefix dict…」的日志噪音
        words = list(jieba.cut(text))
    except Exception:
        return []                      # jieba 不可用时静默降级，前缀 n-gram 仍能兜
    out = []
    for w in words:
        w = w.strip()
        if len(w) >= 2 and w not in _STOPWORDS and not compliance.normalize(w)[0].isdigit():
            if w not in out:
                out.append(w)
    return out


def extract_keywords(question: str, manual: list[str] | None = None,
                     max_prefix: int = 6) -> tuple[list[str], list[str], list[str]]:
    """
    从问题里抽匹配关键词，返回 (强, 中, 弱)。

    **为什么要分档**：`会不会太软` 用 jieba 会切出 `不会`——观众打一句
    「不会吧」就会误匹配到「会不会太软」这条。可另一方面，`咖啡豆` 单独出现
    （弹幕「咖啡豆还是粉」）明明是极强的信号，却因为分低被阈值挡掉。
    两者都是 jieba 实词，靠长度区分不了，只能靠**分档**：

      * **强**：人工指定 + 同义词组。单独命中即判匹配。
      * **中**：jieba 实词。词义完整，权重 2×长度。
      * **弱**：问题本体 + 前缀 n-gram。碎片化，权重 1×长度。

    优先级：人工 > 同义词 > 问题本体 > jieba 实词 > 前缀 n-gram

    ⚠️ **不做**「去掉被包含的短词」的收敛。第一版做了，结果「敏感肌能用吗」
    抽出来的词里没有「敏感肌」——被核心短语包含而被删掉，而那恰恰最有价值。
    短词管召回（观众只打「敏感肌」也要能中），长词管精度，都留，排序交给长度加权。

    ⚠️ **不用定长滑窗**。只取**前缀** n-gram：中文疑问句的前缀天然是完整语义
    单元（「内芯能洗吗」→「内芯」「内芯能」都合理），中间片段（「芯能」）没意义。
    """
    strong: list[str] = []
    medium: list[str] = []
    weak: list[str] = []

    def add(lst, k):
        k = k.strip()
        if k and k not in lst:
            lst.append(k)

    # ① 人工指定（最高优先级，来自 products.json 的 faq[].keywords）→ 强
    for k in (manual or []):
        add(strong, _normalize_for_kw(k))

    # ② 同义词组扩展 → 强
    nq = _normalize_for_kw(question)
    for group in SYNONYM_GROUPS:
        if any(_normalize_for_kw(g) in nq for g in group):
            for g in group:
                add(strong, _normalize_for_kw(g))

    # ③ 问题本体（去掉疑问外壳）→ 弱
    core = _STOP_RE.sub("", nq).strip()
    if len(core) >= 2:
        add(weak, core)

    # ④ jieba 实词 → 中（词义完整，比前缀碎片可靠）
    for w in _content_words(core):
        add(medium, _normalize_for_kw(w))

    # ⑤ 前缀 n-gram（4→2 字）→ 弱，补 jieba 切碎的领域词（如「敏感肌」）
    added = 0
    for n in (4, 3, 2):
        if added >= max_prefix or len(core) < n:
            continue
        g = core[:n]
        if g in _STOPWORDS:
            continue
        before = len(weak)
        add(weak, g)
        if len(weak) > before:
            added += 1

    # 高优先级已有的词不再重复出现在低档里
    medium = [w for w in medium if w not in strong]
    weak = [w for w in weak if w not in strong and w not in medium]
    return strong, medium, weak


# ── 主流程 ───────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="FAQ 预渲染与索引")
    ap.add_argument("-i", "--input", default="script.json")
    ap.add_argument("--profile", default=None)
    ap.add_argument("--outdir", default=None, help="默认 build/faq/")
    ap.add_argument("--force", action="store_true", help="忽略已有产物全部重渲")
    ap.add_argument("--list", action="store_true", help="只打印现有索引")
    a = ap.parse_args()

    outdir = Path(a.outdir) if a.outdir else (HERE / "build" / "faq")
    index_file = outdir / "faq_index.json"

    if a.list:
        if not index_file.is_file():
            raise SystemExit(f"索引不存在：{index_file}")
        idx = json.loads(index_file.read_text(encoding="utf-8"))
        print(f"索引：{index_file}")
        print(f"构建于 {idx['built_at']}  档案={idx.get('profile_id')}  {len(idx['entries'])} 条\n")
        for e in idx["entries"]:
            kw = "、".join(e["keywords"][:6])
            print(f"  {e['id']:<16}{e['duration']:5.1f}s  Q={e['question']}")
            print(f"  {'':<16}关键词: {kw}{'…' if len(e['keywords']) > 6 else ''}")
        return

    if not a.profile:
        raise SystemExit("--profile 必填（要哪个数字人档案来念）")

    script = json.loads(Path(a.input).read_text(encoding="utf-8"))
    faq_segs = [s for s in script.get("segments", []) if s.get("type") == "faq"]
    if not faq_segs:
        raise SystemExit(f"{a.input} 里没有 type=='faq' 的段落")

    # 商品库里的 keywords 字段（若用户手工加了）
    manual_kw: dict[str, list[str]] = {}
    pfile = script.get("meta", {}).get("products_file")
    if pfile and Path(pfile).is_file():
        prods = json.loads(Path(pfile).read_text(encoding="utf-8")).get("products", [])
        for p in prods:
            for i, f in enumerate(p.get("faq", []), 1):
                if f.get("keywords"):
                    manual_kw[f"{p['sku']}-faq{i}"] = f["keywords"]

    # 合规复检：script.json 已查过，但它是可手改的文件，这里再兜一道
    for s in faq_segs:
        vs = [v for v in compliance.check(s["text"]) if v.severity == compliance.BLOCK]
        if vs:
            raise SystemExit(
                f"❌ 段落 {s['id']} 含必改级违规 {[v['phrase'] for v in vs]}，拒绝渲染。\n"
                f"   这是防线：违规话术一旦渲成片段会被反复播放。请修 script.json 后重跑。")

    pdir = DIGITAL_HUMANS / a.profile
    if not (pdir / "profile.json").is_file():
        raise SystemExit(f"档案不存在：{pdir}")
    prof = json.loads((pdir / "profile.json").read_text(encoding="utf-8"))

    outdir.mkdir(parents=True, exist_ok=True)
    segdir = outdir / "clips"
    segdir.mkdir(parents=True, exist_ok=True)

    print(f"档案 {a.profile}（{prof.get('display_name','')}）  {len(faq_segs)} 条 FAQ")
    print(f"产物目录 {outdir}\n")

    entries, failed = [], []
    for i, seg in enumerate(faq_segs, 1):
        sid = seg["id"]
        mp4 = segdir / f"{sid}.mp4"
        ts = segdir / f"{sid}.ts"
        tag = f"[{i:>2}/{len(faq_segs)}] {sid:<16}"

        if not a.force and mp4.is_file() and ts.is_file() and mp4.stat().st_size > 0:
            d = probe_duration(mp4)
            print(f"  ⏭  {tag} 已存在 {d:5.1f}s")
        else:
            try:
                t0 = time.time()
                render_one({**seg, "text": seg["text"]}, prof, pdir, mp4)
                d = probe_duration(mp4)
                ok = has_audio(mp4)
                print(f"  {'✅' if ok else '⚠️ '} {tag} {time.time()-t0:5.1f}s "
                      f"{d:5.1f}s {mp4.stat().st_size//1024:>4}KB"
                      + ("" if ok else "  ← 音轨疑似静音！"))
            except Exception as e:
                print(f"  ❌ {tag} 渲染失败：{e}")
                failed.append((sid, str(e)))
                continue

        # 转 MPEG-TS（-c copy，与 mp4 同码流，供 FIFO 喂流）
        if a.force or not ts.is_file() or ts.stat().st_size == 0:
            r = subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(mp4),
                                "-c", "copy", "-f", "mpegts", str(ts)],
                               capture_output=True, text=True)
            if r.returncode != 0:
                print(f"     ⚠️  转 TS 失败，该条只能走 mp4 路径：{r.stderr[:120]}")

        strong, medium, weak = extract_keywords(seg.get("q", ""), manual_kw.get(sid))
        entries.append({
            "id": sid,
            "sku": seg.get("sku"),
            "question": seg.get("q", ""),
            "answer_text": seg["text"],
            "keywords": strong,          # 强：人工 + 同义词，单独命中即可
            "medium_keywords": medium,   # 中：jieba 实词，权重 2×长度
            "weak_keywords": weak,       # 弱：本体 + 前缀，权重 1×长度
            "mp4": f"clips/{sid}.mp4",
            "ts": f"clips/{sid}.ts" if ts.is_file() and ts.stat().st_size else None,
            "duration": round(probe_duration(mp4), 2),
            "cooldown_seconds": 120,
            "play_count": 0,
            "last_played_at": None,
        })

    if not entries:
        raise SystemExit("\n没有任何片段，终止。")

    index = {
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "profile_id": a.profile,
        "source_script": str(Path(a.input).resolve()),
        "entries": entries,
    }
    index_file.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

    total = sum(e["duration"] for e in entries)
    print(f"\n{'─'*66}")
    print(f"✅ 索引：{index_file}")
    print(f"   {len(entries)} 条片段，总时长 {total:.0f} 秒")
    print(f"   片段目录：{segdir}")
    if failed:
        print(f"\n⚠️  {len(failed)} 条失败：")
        for sid, err in failed:
            print(f"   {sid}：{err[:90]}")
    print(f"\n下一步：python matcher.py --selftest      # 试匹配")
    print(f"        python player.py --profile {a.profile}  # 起播放器")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
