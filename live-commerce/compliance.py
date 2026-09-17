#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
直播话术合规检查（《广告法》《反不正当竞争法》相关）

电商直播最容易翻车的地方不是画面，是话术里的绝对化用语和虚假功效宣称。
本模块在「生成」和「渲染」两个环节各拦一道。

设计要点：
  * **按商品类目分档**——「治疗」对药品合法，对普通化妆品违法，不能一刀切
  * **归一化匹配**——连「最 好」「最-好」「１００％」这类规避写法一起抓
  * **区分 block / warn**——block 必须改，warn 提示人工确认（避免全角半角之类的假阳性）
"""
import re
import unicodedata
from dataclasses import dataclass, asdict

BLOCK = "block"
WARN = "warn"


@dataclass
class Violation:
    phrase: str          # 命中的词
    matched: str         # 原文里实际命中的片段
    category: str        # 违规类别
    severity: str        # block / warn
    position: int        # 在归一化文本里的位置
    advice: str          # 改写建议

    def to_dict(self):
        return asdict(self)


# ── 规则表 ────────────────────────────────────────────────────────────
# severity 说明：
#   block = 明确违法，必须改
#   warn  = 高风险或依赖上下文，建议人工确认

RULES: list[tuple[str, str, str, str]] = [
    # (命中词, 类别, 级别, 建议)

    # —— 绝对化用语（《广告法》第九条明令禁止）——
    ("最好",      "绝对化用语", BLOCK, "改为「很受好评」「口碑不错」"),
    ("最佳",      "绝对化用语", BLOCK, "改为「表现出色」"),
    ("最优",      "绝对化用语", BLOCK, "改为「更优」"),
    ("最强",      "绝对化用语", BLOCK, "改为「性能出色」"),
    ("最便宜",    "绝对化用语", BLOCK, "改为「价格实惠」"),
    ("最低价",    "绝对化用语", BLOCK, "改为「活动价」"),
    ("最高级",    "绝对化用语", BLOCK, "改为「高品质」"),
    ("最先进",    "绝对化用语", BLOCK, "改为「较新」"),
    ("最流行",    "绝对化用语", BLOCK, "改为「很受欢迎」"),
    ("最受欢迎",  "绝对化用语", BLOCK, "改为「很多人选择」"),
    ("最新科技",  "绝对化用语", BLOCK, "改为「新工艺」"),
    ("史上最低",  "绝对化用语", BLOCK, "删除"),
    ("第一",      "绝对化用语", WARN,  "若是名次义则违法；若是「第一步」这类序数义可保留，请人工确认"),
    ("全网销量",  "绝对化用语", WARN,  "销量类表述需有可核查依据，否则删除"),
    ("销量冠军",  "绝对化用语", BLOCK, "改为「销量不错」"),
    ("排名第一",  "绝对化用语", BLOCK, "删除"),
    ("遥遥领先",  "绝对化用语", BLOCK, "改为「表现不错」"),
    ("独一无二",  "绝对化用语", BLOCK, "改为「有特色」"),
    ("绝无仅有",  "绝对化用语", BLOCK, "改为「比较少见」"),
    ("国家",      "绝对化用语", WARN,  "「国家级」违法；单纯提及国家可保留，请人工确认"),
    ("国家级",    "绝对化用语", BLOCK, "删除"),
    ("世界级",    "绝对化用语", BLOCK, "删除"),
    ("顶级",      "绝对化用语", BLOCK, "改为「高端」"),
    ("极致",      "绝对化用语", BLOCK, "改为「出色」"),
    ("极品",      "绝对化用语", BLOCK, "改为「优质」"),
    ("王牌",      "绝对化用语", BLOCK, "删除"),
    ("巅峰",      "绝对化用语", WARN,  "高风险表述，建议删除"),
    ("百分百",    "绝对化用语", WARN,  "成分含量陈述（「百分百纯棉」）属实可用；功效类（「百分百有效」）违法，请人工确认"),
    ("100%",      "绝对化用语", WARN,  "成分含量陈述（「100% 纯棉」）属实可用；功效类（「100% 有效」）违法，请人工确认"),
    ("百分之百",  "绝对化用语", WARN,  "成分含量陈述属实可用；功效类违法，请人工确认"),

    # —— 限时/限价类（需真实可查，否则构成虚假宣传）——
    ("史上最低价", "虚假宣传", BLOCK, "删除或改为「本次活动价」"),
    ("仅此一天",   "虚假宣传", WARN,  "限时表述必须真实，否则违法"),
    ("最后一天",   "虚假宣传", WARN,  "限时表述必须真实，否则违法"),
    ("错过不再",   "虚假宣传", WARN,  "限时表述必须真实，否则违法"),
    ("亏本",       "虚假宣传", WARN,  "「亏本甩卖」需真实，否则违法"),
    ("清仓",       "虚假宣传", WARN,  "需与实际库存一致"),

    # —— 虚假承诺 ——
    ("保证",   "虚假承诺", WARN,  "「保证」类承诺需有依据，建议改为「正常情况下」"),
    ("零风险", "虚假承诺", BLOCK, "改为「风险较低」"),
    ("无风险", "虚假承诺", BLOCK, "改为「风险较低」"),
    ("稳赚",   "虚假承诺", BLOCK, "删除"),
    ("包过",   "虚假承诺", BLOCK, "删除"),
    ("永久",   "虚假承诺", WARN,  "「永久有效」类表述建议改为明确期限"),
    ("终身",   "虚假承诺", WARN,  "明确期限后再使用"),
    ("万能",   "虚假承诺", BLOCK, "改为「适用面较广」"),

    # —— 医疗/功效（对普通食品、化妆品是重灾区）——
    ("根治",     "医疗功效", BLOCK, "普通商品禁用，改为「帮助改善」"),
    ("治愈",     "医疗功效", BLOCK, "普通商品禁用"),
    ("治疗",     "医疗功效", BLOCK, "普通商品禁用，改为「护理」"),
    ("疗效",     "医疗功效", BLOCK, "普通商品禁用"),
    ("痊愈",     "医疗功效", BLOCK, "普通商品禁用"),
    ("药到病除", "医疗功效", BLOCK, "普通商品禁用"),
    ("包治",     "医疗功效", BLOCK, "普通商品禁用"),
    ("抗癌",     "医疗功效", BLOCK, "普通商品禁用"),
    ("降血压",   "医疗功效", BLOCK, "普通商品禁用"),
    ("降血糖",   "医疗功效", BLOCK, "普通商品禁用"),
    ("壮阳",     "医疗功效", BLOCK, "普通商品禁用"),
    ("特效",     "医疗功效", BLOCK, "普通商品禁用"),
    ("全效",     "医疗功效", WARN,  "高风险表述"),
    ("排毒",     "医疗功效", BLOCK, "普通商品禁用"),
    ("消炎",     "医疗功效", BLOCK, "普通商品禁用"),
    ("杀菌",     "医疗功效", WARN,  "非消毒产品禁用；确有检测报告的需标明"),
    ("祛斑",     "医疗功效", WARN,  "特殊化妆品需注册，普通化妆品禁用"),
    ("美白",     "医疗功效", WARN,  "特殊化妆品需注册证号，否则禁用"),
    ("生发",     "医疗功效", WARN,  "特殊化妆品需注册证号"),
    ("减肥",     "医疗功效", WARN,  "保健食品需蓝帽子，普通食品禁用"),
    ("瘦身",     "医疗功效", WARN,  "普通食品禁用"),
    ("抗衰老",   "医疗功效", WARN,  "化妆品可用「抗皱」，慎用「抗衰老」"),

    # —— 金融/收益（若卖理财或加盟）——
    ("保本",     "金融收益", BLOCK, "删除"),
    ("保收益",   "金融收益", BLOCK, "删除"),
    ("稳赚不赔", "金融收益", BLOCK, "删除"),
    ("高回报",   "金融收益", BLOCK, "删除"),
    ("躺赚",     "金融收益", BLOCK, "删除"),
]

# 归一化时被抹掉的字符（用于抓「最 好」「最-好」「最＊好」这类规避写法）
_STRIP = re.compile(r"[\s\-_*·•．.、，,~～|/\\]")


def normalize(text: str) -> tuple[str, list[int]]:
    """
    归一化文本，同时返回每个归一化字符在原文中的下标。

    做了三件事：全角转半角、英文小写、抹掉分隔符。
    返回 (归一化文本, 原文字符下标表)，这样命中位置能映射回原文，方便高亮。
    """
    out_chars, out_idx = [], []
    for i, ch in enumerate(text):
        ch = unicodedata.normalize("NFKC", ch)      # 全角 → 半角
        if _STRIP.match(ch):
            continue
        for c in ch.lower():
            out_chars.append(c)
            out_idx.append(i)
    return "".join(out_chars), out_idx


# 归一化后的规则表（模块加载时算一次）
_NORM_RULES = [(normalize(p)[0], p, cat, sev, adv) for p, cat, sev, adv in RULES]


def check(text: str, product_category: str | None = None) -> list[Violation]:
    """
    检查一段话术。

    product_category 用于给医疗功效类的建议加类目前缀（措辞更具体），
    规则本身是保守的——对普通商品一律按最严处理。

    重叠命中会去重：长词优先。否则「国家级认证」会同时报出
    「国家级」（block）和「国家」（warn），产生噪音。
    """
    norm, idx_map = normalize(text)
    found: list[Violation] = []
    claimed: list[tuple[int, int]] = []          # 已占用的归一化区间

    # 长词优先，保证「史上最低价」胜过「最低价」、「国家级」胜过「国家」
    for norm_phrase, phrase, cat, sev, advice in sorted(
            _NORM_RULES, key=lambda r: -len(r[0])):
        if not norm_phrase:
            continue
        start = 0
        while (pos := norm.find(norm_phrase, start)) >= 0:
            end = pos + len(norm_phrase)
            start = pos + 1
            if any(pos < c_end and end > c_start for c_start, c_end in claimed):
                continue                          # 与已命中的长词重叠，跳过
            claimed.append((pos, end))
            orig_pos = idx_map[pos] if pos < len(idx_map) else 0
            orig_end = idx_map[min(end - 1, len(idx_map) - 1)] + 1
            note = advice
            if product_category and cat == "医疗功效" and \
                    product_category in ("普通食品", "化妆品", "普通化妆品"):
                note = f"[{product_category}] " + note
            found.append(Violation(phrase=phrase, matched=text[orig_pos:orig_end],
                                   category=cat, severity=sev,
                                   position=orig_pos, advice=note))
            break                                 # 同一词只报第一处，避免刷屏

    return sorted(found, key=lambda v: (v.severity != BLOCK, v.position))


def check_many(texts: list[str], product_category: str | None = None) -> dict[int, list[Violation]]:
    return {i: v for i, t in enumerate(texts) if (v := check(t, product_category))}


def summarize(violations: list[Violation]) -> str:
    if not violations:
        return "无违规"
    blocks = [v for v in violations if v.severity == BLOCK]
    warns = [v for v in violations if v.severity == WARN]
    parts = []
    if blocks:
        parts.append(f"{len(blocks)} 处必改：" + "、".join(v.phrase for v in blocks))
    if warns:
        parts.append(f"{len(warns)} 处待确认：" + "、".join(v.phrase for v in warns))
    return "；".join(parts)


# ── 自测 ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    cases = [
        ("这款精华效果最好，全网销量第一！", "化妆品"),
        ("敏感肌也能用，保证根治痘痘，绝对无风险。", "化妆品"),
        ("今天最后一天，史上最低价，错过不再有。", "普通食品"),
        ("这是第一步，先把脸洗干净，然后涂上精华。", "化妆品"),   # 应无 block
        ("我们的咖啡豆产自云南，100% 阿拉比卡。", "普通食品"),      # 100% 命中
        ("最 好 的 选 择", "化妆品"),                               # 规避写法
        ("１００％ 纯棉，国家级认证。", "家居"),                    # 全角
    ]
    for text, cat in cases:
        vs = check(text, cat)
        print(f"\n[{cat}] {text}")
        if not vs:
            print("  ✅ 无违规")
        for v in vs:
            icon = "🛑" if v.severity == BLOCK else "⚠️ "
            print(f"  {icon} {v.category:<6} 「{v.matched}」 → {v.advice}")
