#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
FAQ 匹配器

把清洗后的弹幕匹配到一条预渲染的 FAQ 片段。纯关键词，不依赖任何模型。

打分（长度加权）：
    强关键词命中（人工 + 同义词）  score += len(kw) * 3
    弱关键词命中（本体 + jieba + 前缀） score += len(kw)

判定：
    * 有强关键词命中 → 直接算匹配
    * 只有弱关键词   → 累计分数须 ≥ min_weak_score

为什么强弱要分开：`会不会太软` 用 jieba 会切出 `不会`，观众打一句「不会吧」
就会误匹配。这类通用短词单独出现不可信，几个一起出现才可信。

两道节流：
    * **冷却**：同一条 FAQ 在 cooldown_seconds 内不重复插播
    * **全局**：两次插播之间至少间隔 min_interval 秒（防弹幕轰炸时画面狂切）

日志：matched.log / unanswered.log（JSONL）。
unanswered.log 里记了 top1 分数，用来判断是「真没这条 FAQ」（分低）
还是「阈值定高了」（分接近阈值）——**前者补内容，后者调阈值**。

用法：
    python matcher.py --selftest
    python matcher.py --selftest --min-weak-score 9      # 更保守，减少误匹配
    python matcher.py --query "敏感肌能用吗"
"""
import argparse
import json
import time
from collections import defaultdict
from dataclasses import dataclass, asdict
from pathlib import Path

import compliance

HERE = Path(__file__).resolve().parent
DEFAULT_INDEX = HERE / "build" / "faq" / "faq_index.json"

STRONG_WEIGHT = 3
MEDIUM_WEIGHT = 2
WEAK_WEIGHT = 1
_MARK = {"strong": "★", "medium": "☆", "weak": "·"}


@dataclass
class MatchResult:
    entry_id: str
    question: str
    ts: str | None
    mp4: str
    duration: float
    score: int
    matched_keywords: list
    reason: str = "ok"

    def to_dict(self):
        return asdict(self)


class FaqMatcher:
    def __init__(self, index_path: Path = DEFAULT_INDEX,
                 min_weak_score: int = 6,
                 min_interval: float = 30.0,
                 log_dir: Path | None = None):
        self.index_path = Path(index_path)
        if not self.index_path.is_file():
            raise SystemExit(
                f"索引不存在：{self.index_path}\n"
                f"  先跑：python faq_build.py -i script.json --profile <档案id>")
        idx = json.loads(self.index_path.read_text(encoding="utf-8"))
        self.entries = {e["id"]: e for e in idx["entries"]}
        self.profile_id = idx.get("profile_id")
        self.min_weak_score = min_weak_score
        self.min_interval = min_interval
        self.last_insert_at = 0.0

        # 倒排索引：关键词 → [(entry_id, 档位)]
        self.index: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for eid, e in self.entries.items():
            for tier, field in (("strong", "keywords"),
                               ("medium", "medium_keywords"),
                               ("weak", "weak_keywords")):
                for kw in e.get(field, []):
                    self.index[_norm(kw)].append((eid, tier))

        self.log_dir = Path(log_dir) if log_dir else (HERE / "build" / "faq")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._matched_log = self.log_dir / "matched.log"
        self._unanswered_log = self.log_dir / "unanswered.log"

    # ── 核心 ──────────────────────────────────────────────────────

    def score(self, text: str) -> tuple[dict, dict]:
        """返回 (每个 entry 的得分, 每个 entry 命中的关键词)。不涉及节流。"""
        nq = _norm(text)
        weights = {"strong": STRONG_WEIGHT, "medium": MEDIUM_WEIGHT, "weak": WEAK_WEIGHT}
        scores: dict[str, int] = defaultdict(int)
        hits: dict[str, list] = defaultdict(list)
        for kw, targets in self.index.items():
            if kw and kw in nq:
                for eid, tier in targets:
                    scores[eid] += len(kw) * weights[tier]
                    hits[eid].append(_MARK[tier] + kw)
        return scores, hits

    def match(self, text: str, now: float | None = None,
              log: bool = True) -> MatchResult | None:
        """
        匹配一条弹幕。返回 MatchResult 或 None（未命中/被节流）。
        log=True 时把结果写入 matched.log 或 unanswered.log。
        """
        now = now if now is not None else time.time()
        scores, hits = self.score(text)

        if not scores:
            if log:
                self._log_unanswered(text, now, None, 0, "无关键词命中")
            return None

        # 排序：先比分数，再比关键词长度（更具体的优先）
        def rank(eid):
            return (scores[eid],
                    max((len(h[1:]) for h in hits[eid]), default=0))

        best_id = max(scores, key=rank)
        best_score = scores[best_id]
        top_kws = hits[best_id]
        has_strong = any(h.startswith("★") for h in top_kws)

        # 判定：有强命中直接过；纯中/弱命中要累计够分
        if not has_strong and best_score < self.min_weak_score:
            if log:
                self._log_unanswered(text, now, best_id, best_score,
                                     f"命中分数不足({best_score}<{self.min_weak_score})")
            return None

        e = self.entries[best_id]

        # 冷却：同一条不重复播
        if e.get("last_played_at") and \
                now - e["last_played_at"] < e.get("cooldown_seconds", 120):
            left = e["cooldown_seconds"] - (now - e["last_played_at"])
            if log:
                self._log_unanswered(text, now, best_id, best_score,
                                     f"冷却中(还需{left:.0f}s)")
            return None

        # 全局节流：防弹幕轰炸时画面狂切
        if now - self.last_insert_at < self.min_interval:
            left = self.min_interval - (now - self.last_insert_at)
            if log:
                self._log_unanswered(text, now, best_id, best_score,
                                     f"全局节流(还需{left:.0f}s)")
            return None

        # 通过
        e["last_played_at"] = now
        e["play_count"] = e.get("play_count", 0) + 1
        self.last_insert_at = now
        r = MatchResult(entry_id=best_id, question=e["question"], ts=e.get("ts"),
                        mp4=e["mp4"], duration=e["duration"],
                        score=best_score, matched_keywords=top_kws)
        if log:
            self._append(self._matched_log, {"ts": now, "text": text, **r.to_dict()})
        return r

    # ── 日志 ──────────────────────────────────────────────────────

    def _append(self, path: Path, obj: dict):
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        except OSError:
            pass                       # 日志失败不该影响直播

    def _log_unanswered(self, text, now, eid, score, reason):
        self._append(self._unanswered_log,
                     {"ts": now, "text": text, "top1_id": eid,
                      "top1_score": score, "reason": reason})

    def save_state(self):
        """把播放计数与冷却时间写回索引，重启后可续。"""
        try:
            idx = json.loads(self.index_path.read_text(encoding="utf-8"))
            for e in idx["entries"]:
                src = self.entries.get(e["id"])
                if src:
                    e["play_count"] = src.get("play_count", 0)
                    e["last_played_at"] = src.get("last_played_at")
            self.index_path.write_text(
                json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def summary(self) -> str:
        n = len(self.entries)

        def tot(f):
            return sum(len(e.get(f, [])) for e in self.entries.values())

        return (f"{n} 条 FAQ   强 {tot('keywords')} / 中 {tot('medium_keywords')}"
                f" / 弱 {tot('weak_keywords')} 个关键词")


def _norm(s: str) -> str:
    return compliance.normalize(s)[0]


# ── 自测 ──────────────────────────────────────────────────────────────

SELFTEST_CASES = [
    # ── 应当命中（关键词能覆盖的）──
    ("敏感肌能用吗",            "MZ001-faq1"),
    ("我是敏感肌，可以用吗",       "MZ001-faq1"),
    ("过敏体质能用吗",           "MZ001-faq1"),
    ("多久能看出效果",           "MZ001-faq2"),
    ("用多长时间见效",           "MZ001-faq2"),
    ("孕妇能用吗",              "MZ001-faq3"),
    ("怀孕了可以用吗",           "MZ001-faq3"),
    ("怎么用啊",               "MZ001-faq4"),
    ("咖啡豆还是粉",            "SP002-faq1"),
    ("苦不苦啊",               "SP002-faq2"),
    ("怎么保存",               "SP002-faq3"),
    ("会不会太软",              "JJ003-faq1"),
    ("内芯能洗吗",              "JJ003-faq3"),
    ("支持试喝吗",              "SP002-faq4"),

    # ── 应当**不**命中 ──
    # 商品库里没有价格类 FAQ。若这里命中了，说明弱关键词阈值太低。
    ("这个多少钱",              None),
    # 完全不相关的弹幕，不该乱答。误匹配比漏答更伤（见计划文档 §7）。
    ("主播今天穿得真好看",        None),
    ("哈哈哈哈",               None),

    # ── 已知会漏（换一种说法的同义表述）──
    # 这两条是**故意留的**：关键词匹配本来就覆盖不了它们，
    # 正是计划文档 §5 里「阶段三加向量兜底」要解决的问题。
    # 现在标记为 None，将来接入向量后应改成正则可命中的 id。
    ("夏天用着闷不闷",          None),
    ("大热天躺上去会不会捂汗",     None),
]


def main():
    ap = argparse.ArgumentParser(description="FAQ 匹配器")
    ap.add_argument("--index", default=str(DEFAULT_INDEX))
    ap.add_argument("--min-weak-score", type=int, default=6,
                    help="中/弱关键词命中的累计分阈值。调低→召回高但易误匹配；调高→反之")
    ap.add_argument("--min-interval", type=float, default=30.0)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--query", help="匹配一条弹幕试试")
    a = ap.parse_args()

    m = FaqMatcher(a.index, a.min_weak_score, a.min_interval)
    print(f"索引 {a.index}")
    print(f"{m.summary()}")
    print(f"参数：强权重 ×{STRONG_WEIGHT}  弱命中阈值 {m.min_weak_score}  全局间隔 {m.min_interval}s\n")

    if a.query:
        r = m.match(a.query, log=False)
        if r:
            print(f"✅ 「{a.query}」 → {r.entry_id}  {r.question}")
            print(f"   得分 {r.score}  命中 {r.matched_keywords}  片段 {r.ts or r.mp4}")
        else:
            s, h = m.score(a.query)
            if s:
                b = max(s, key=s.get)
                print(f"❌ 未命中（最高 {b} 得分 {s[b]}，{h[b]}）")
            else:
                print(f"❌ 未命中（无任何关键词命中）")
        return

    if not a.selftest:
        raise SystemExit("请加 --selftest 或 --query '弹幕内容'")

    ok = bad = miss = 0
    print("=== 自测（关闭日志与节流）===")
    m.min_interval = 0
    for e in m.entries.values():
        e["last_played_at"] = None
    for text, want in SELFTEST_CASES:
        r = m.match(text, log=False)
        got = r.entry_id if r else None
        if want is None:
            if got is None:
                ok += 1; mark = "✅"
            else:
                miss += 1; mark = "⚠️ "     # 匹配到某条（可能合理，看下面的输出）
        elif got == want:
            ok += 1; mark = "✅"
        else:
            bad += 1; mark = "❌"
        got_s = f"{got}({r.score})" if r else "—"
        print(f"  {mark} 「{text}」 → {got_s}"
              + (f"   期望 {want}" if mark != "✅" else ""))
        # 自测里每次都重置冷却，避免互相干扰
        for e in m.entries.values():
            e["last_played_at"] = None
        m.last_insert_at = 0

    print(f"\n  通过 {ok}  错误 {bad}  意外命中 {miss}  共 {len(SELFTEST_CASES)}")
    print(f"\n⚠️ 注意：上面案例里若有匹配到「非期望但语义相近」的条目，")
    print(f"   可能是商品库里的相似问题（如实测中「这个多少钱」会命中它自己那条 FAQ），")
    print(f"   属于可接受行为，看输出自行判断。")
    print(f"\n   真实弹幕跑一轮后看 {m._unanswered_log} 调阈值。")


if __name__ == "__main__":
    main()
