#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
弹幕输入与清洗

⚠️ **本模块刻意不内置任何平台的弹幕抓取实现。**

抖音/淘宝/视频号的弹幕获取只有两条路：官方开放平台（需企业资质+审核），
或第三方逆向库（有封号风险、频繁失效、可能违反平台协议）。后者不该由本系统
替你决策。所以这里只定义**统一输入接口**，平台接入做成可替换的插件：

    stdin / 文件 tail  →  HTTP 回调  →  WebSocket
    （阶段一，测试用）    （阶段二）     （阶段三，接真实源）

接入方只需把弹幕按统一格式喂进来：

    {"ts": 1789564800.123, "user": "张三", "text": "敏感肌能用吗", "source": "douyin"}

外部程序接的话，往 `POST /danmaku` 发同样的 JSON 即可。
"""
import json
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, asdict, field

import compliance

# ── 清洗规则 ──────────────────────────────────────────────────────────

_RE_MENTION = re.compile(r"@[^\s@]{1,20}")                 # @昵称
_RE_URL = re.compile(r"(https?://|www\.)\S+", re.I)
_RE_WECHAT = re.compile(r"(微信|weixin|vx|v信|薇信|威信)\s*[:：]?\s*[A-Za-z0-9_\-]{4,}", re.I)
_RE_QQ = re.compile(r"(qq|扣扣)\s*[:：]?\s*\d{5,}", re.I)
_RE_PHONE = re.compile(r"1[3-9]\d{9}")
_RE_REPEAT = re.compile(r"(.)\1{4,}")                      # 刷屏：同字重复 5 次以上
# emoji / 符号（保留中文、字母、数字、常用标点）
_RE_JUNK = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF"
    "←-⇿☀-⛿✀-➿️⬀-⯿]+")
# 「有内容」的判据：至少含一个中日韩汉字或字母数字
_RE_HAS_CONTENT = re.compile(r"[一-鿿A-Za-z0-9]")

MAX_LEN = 60          # 超过这个长度基本是复制粘贴的广告/小作文
MIN_LEN = 2


@dataclass
class Danmaku:
    ts: float
    user: str
    text: str                       # 清洗后的文本
    raw: str = ""                   # 原文，便于排查
    source: str = "unknown"
    dropped: str = ""               # 非空表示被丢弃的原因

    def to_dict(self):
        return asdict(self)


def clean(raw: str) -> tuple[str, str]:
    """
    清洗一条弹幕。返回 (清洗后文本, 丢弃原因)。
    丢弃原因非空表示这条不该进匹配器。
    """
    if not raw or not raw.strip():
        return "", "空"

    # 引流广告整条丢弃，而不是剥掉联系方式后留下残渣
    # （「加微信 abc123 有优惠」剥完只剩「加有优惠」，匹配它毫无意义）
    if _RE_WECHAT.search(raw) or _RE_QQ.search(raw) or _RE_PHONE.search(raw):
        return "", "引流广告"

    t = raw
    t = _RE_MENTION.sub(" ", t)
    t = _RE_URL.sub(" ", t)
    t = _RE_JUNK.sub(" ", t)
    t = unicodedata.normalize("NFKC", t)     # 全角 → 半角
    t = _RE_REPEAT.sub(r"\1\1", t)
    t = re.sub(r"\s+", "", t).strip()

    if not t:
        return "", "表情/符号"
    if len(t) < MIN_LEN:
        return "", "过短"
    if len(t) > MAX_LEN:
        return "", f"过长({len(t)}字，疑似广告)"
    # 至少要有一个汉字或字母数字——纯标点/纯符号没有匹配价值
    if not _RE_HAS_CONTENT.search(t):
        return "", "无有效字符"
    if t.isdigit():
        return "", "纯数字"

    # 违规内容不响应（复用合规规则）
    vs = [v for v in compliance.check(t) if v.severity == compliance.BLOCK]
    if vs:
        return "", f"含违规词({vs[0].phrase})"

    return t, ""


def parse_line(line: str, source: str = "stdin") -> Danmaku | None:
    """
    解析一行输入。支持两种格式：
      * JSON：{"text": "...", "user": "...", "ts": ...}
      * 纯文本：直接当作弹幕内容（方便调试：echo '敏感肌能用吗' | ...）
    """
    line = line.strip()
    if not line:
        return None
    user, ts, raw = "", time.time(), line
    if line.startswith("{"):
        try:
            d = json.loads(line)
            raw = d.get("text") or d.get("content") or ""
            user = d.get("user") or d.get("nickname") or ""
            ts = float(d.get("ts") or ts)
            source = d.get("source") or source
        except json.JSONDecodeError:
            pass                       # 不是合法 JSON，按纯文本处理

    text, dropped = clean(raw)
    return Danmaku(ts=ts, user=user, text=text, raw=raw,
                   source=source, dropped=dropped)


# ── 输入源 ────────────────────────────────────────────────────────────

def iter_stdin():
    """从 stdin 逐行读。管道或交互输入都行。"""
    for line in sys.stdin:
        dm = parse_line(line)
        if dm:
            yield dm


def iter_file(path: str, from_end: bool = True):
    """tail -f 语义。from_end=False 时从头读（补播历史弹幕）。"""
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        if from_end:
            f.seek(0, 2)
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.2)
                continue
            dm = parse_line(line)
            if dm:
                yield dm


# ── 自测 ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cases = [
        ("敏感肌能用吗", "正常"),
        ("@小助手 敏感肌能用吗", "去 @昵称"),
        ("这个多少钱？ http://taobao.com/xxx", "去 URL"),
        ("加微信 abc123456 有优惠", "广告 → 丢弃"),
        ("买它买它买它！！！", "正常"),
        ("哈哈哈哈哈哈哈哈哈", "刷屏重复 → 压缩"),
        ("😍😍😍😍", "纯表情 → 丢弃"),
        ("a", "过短 → 丢弃"),
        ("123456789", "纯数字 → 丢弃"),
        ("这个产品根治痘痘吗", "含违规词 → 丢弃"),
        ("这个产品怎么样啊我觉得还行吧", "正常"),
        ("１００％能用吗", "全角归一化"),
        ('{"user":"张三","text":"孕妇可以用吗"}', "JSON 格式"),
    ]
    print("=== 弹幕清洗 ===")
    for raw, note in cases:
        dm = parse_line(raw)
        if dm is None:
            print(f"  （空行）"); continue
        if dm.dropped:
            print(f"  🚫 {note:<16} 「{dm.raw[:26]}」 → 丢弃：{dm.dropped}")
        else:
            print(f"  ✅ {note:<16} 「{dm.raw[:26]}」 → 「{dm.text}」")
