#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
直播话术生成

把商品库 → 一组可直接朗读的短口播文案（script.json）。

两个后端：
  llm      —— 走 OpenAI 兼容 API（DeepSeek / 通义 / 智谱 / 月之暗面 等）
  template —— 纯模板拼装，不需要任何 key

两者输出**同样的 schema**，所以可以先用 template 跑通渲染和推流链路，
配好 key 后无缝切到 llm。

合规：每条生成的话术都过 compliance.check()，block 级违规会自动重写一次，
仍不合规则拒收并报错——**不会把违规话术放进可渲染的 script.json**。

用法：
    python script_gen.py --list
    python script_gen.py --all --backend template --profile xiaomei -o script.json
    python script_gen.py --sku MZ001 --backend llm -o script.json
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import compliance
from compliance import BLOCK

HERE = Path(__file__).resolve().parent
DEFAULT_PRODUCTS = HERE / "products.example.json"
LLM_CONFIG_FILE = HERE / "llm_config.json"


# ── LLM 配置（绝不硬编码 key）────────────────────────────────────────

def load_llm_config() -> dict:
    """
    优先级：环境变量 > llm_config.json

    环境变量：LIVE_LLM_API_KEY / LIVE_LLM_BASE_URL / LIVE_LLM_MODEL
    """
    cfg = {"api_key": None, "base_url": None, "model": "deepseek-chat",
           "temperature": 0.7, "timeout": 120}
    if LLM_CONFIG_FILE.is_file():
        try:
            cfg.update(json.loads(LLM_CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception as e:
            print(f"⚠️  llm_config.json 解析失败，忽略：{e}")
    for env, key in (("LIVE_LLM_API_KEY", "api_key"),
                     ("LIVE_LLM_BASE_URL", "base_url"),
                     ("LIVE_LLM_MODEL", "model")):
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    return cfg


# ── 话术段落规划 ─────────────────────────────────────────────────────

def plan_segments(product: dict) -> list[dict]:
    """
    决定一个商品要生成哪几段口播。

    电商直播的经典节奏：开场 → 讲品 → 规格 → 答疑 → 促单。
    答疑段按 FAQ 条目展开——这些也是后续「弹幕匹配切片段」的素材。
    """
    sku = product["sku"]
    plan = [
        {"type": "opening", "id": f"{sku}-open",  "hint": "开场，介绍这个商品是什么，制造停留理由"},
        {"type": "pitch",   "id": f"{sku}-pitch", "hint": "讲核心卖点，从 selling_points 里挑 2~3 个展开"},
        {"type": "spec",    "id": f"{sku}-spec",  "hint": "念规格参数和价格，让观众知道怎么买、买到的具体是什么"},
    ]
    for i, faq in enumerate(product.get("faq", []), 1):
        plan.append({"type": "faq", "id": f"{sku}-faq{i}",
                     "hint": f"回答观众提问：{faq['q']}",
                     "q": faq["q"], "a": faq["a"]})
    plan.append({"type": "promo", "id": f"{sku}-promo",
                 "hint": "促单，强调活动价和库存，引导下单"})
    return plan


# ── 后端 A：纯模板 ───────────────────────────────────────────────────

def gen_template(product: dict, plan: list[dict]) -> list[dict]:
    p = product
    name, price, origin = p["name"], p["price"], p.get("origin_price")
    unit = p.get("unit", "")
    sp = p.get("selling_points", [])
    specs = p.get("specs", {})
    out = []

    for item in plan:
        t = item["type"]
        if t == "opening":
            text = (f"欢迎来到直播间。今天给大家带来的这款{p['name']}，"
                    f"{sp[0] if sp else ''}。想了解的朋友先别划走，我一条一条讲。")
        elif t == "pitch":
            points = "。".join(sp[:3])
            text = f"先说他为什么值得买。{points}。"
        elif t == "spec":
            spec_txt = "，".join(f"{k}{v}" for k, v in list(specs.items())[:3])
            price_txt = f"活动价{price}元"
            if origin:
                price_txt += f"，日常价{origin}元"
            text = f"规格说一下：{spec_txt}。{p['name']}{unit}，{price_txt}。"
        elif t == "faq":
            text = f"有朋友问{item['q']}。{item['a']}"
        elif t == "promo":
            price_txt = f"现在活动价{price}元"
            if origin:
                price_txt += f"，比日常的{origin}元划算"
            text = (f"想要的抓紧了，{price_txt}。库存只有{p.get('stock', '少量')}件，"
                    f"拍下今天就能发。点下方小黄车看看。")
        else:
            continue
        seg = {**{k: v for k, v in item.items() if k in ("type", "id")},
               "sku": p["sku"], "text": text}
        if t == "faq":
            seg["q"] = item["q"]          # faq_build.py 用它生成匹配关键词
        out.append(seg)
    return out


# ── 后端 B：LLM ──────────────────────────────────────────────────────

SYSTEM_PROMPT = """你是电商直播间的口播文案策划。你写的文字会被 AI 数字人主播直接朗读。

硬性要求：
1. **只使用我给你的商品资料**，绝不编造参数、成分、功效、价格或资质。
2. 严禁绝对化用语：「最」「第一」「唯一」「顶级」「极致」「国家级」「100%」「百分百」等。
3. 严禁医疗功效宣称：「治疗」「根治」「治愈」「消炎」「祛斑」「美白」「减肥」等。
4. 严禁虚假承诺：「保证」「零风险」「稳赚」「永久」等。
5. 严禁编造限时/限价：「史上最低」「仅此一天」「最后一天」等，除非资料里明确写了。
6. 每段 60~120 字，口语化、短句为主，适合直接朗读，不要书面语和括号注释。
7. 不要出现「大家好我是主播」以外的自我介绍，不要提到自己是 AI。

输出格式：严格的 JSON 数组，每项 {"id": "...", "text": "..."}，不要任何额外文字、不要 markdown 代码块。"""


def _build_user_prompt(product: dict, plan: list[dict], rewrite_of: dict | None = None) -> str:
    parts = ["【商品资料】", json.dumps(product, ensure_ascii=False, indent=2), ""]
    parts.append("【需要生成的段落】")
    for item in plan:
        line = f"- id={item['id']}  类型={item['type']}  要求：{item['hint']}"
        if item.get("a"):
            line += f"（标准答案参考：{item['a']}）"
        parts.append(line)
    if rewrite_of:
        parts.append("")
        parts.append("【上一版存在违规，必须重写】")
        for rid, info in rewrite_of.items():
            parts.append(f"- id={rid}：违规点 {info['phrases']}，原文「{info['text']}」")
        parts.append("请改写这些段落，消除违规，其余段落保持原意。")
    return "\n".join(parts)


def _extract_json(raw: str) -> list[dict]:
    """
    LLM 常常裹一层 ```json，或者前后带解释文字，这里容错提取。

    用 strict=False 解析：模型很爱在字符串里塞**真实换行**（不是转义的 \\n），
    而 json.loads 默认 strict=True 会以 "Invalid control character" 拒绝——
    实测这是最常见的解析失败原因。
    """
    raw = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    if fence:
        raw = fence.group(1).strip()

    for candidate in (raw, None):
        if candidate is None:
            m = re.search(r"\[.*\]", raw, re.S)
            if not m:
                raise ValueError(f"无法从模型输出里解析出 JSON 数组：\n{raw[:500]}")
            candidate = m.group(0)
        try:
            data = json.loads(candidate, strict=False)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, list):
            raise ValueError("模型返回的不是数组")
        return data
    raise ValueError(f"JSON 解析失败：\n{raw[:500]}")


def gen_llm(product: dict, plan: list[dict], cfg: dict) -> list[dict]:
    try:
        from openai import OpenAI
    except ImportError:
        raise SystemExit("未安装 openai：pip install openai")

    if not cfg.get("api_key"):
        raise SystemExit(
            "缺少 LLM API Key。二选一：\n"
            "  ① 设环境变量 LIVE_LLM_API_KEY / LIVE_LLM_BASE_URL / LIVE_LLM_MODEL\n"
            "  ② 复制 llm_config.example.json 为 llm_config.json 并填好\n"
            "  或改用 --backend template（不需要 key）")
    if not cfg.get("base_url"):
        raise SystemExit("缺少 LIVE_LLM_BASE_URL / llm_config.json 里的 base_url")

    client = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"],
                    timeout=cfg.get("timeout", 120))

    def call(user_prompt: str) -> list[dict]:
        r = client.chat.completions.create(
            model=cfg["model"],
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": user_prompt}],
            temperature=cfg.get("temperature", 0.7),
            max_tokens=2000,
        )
        return _extract_json(r.choices[0].message.content or "")

    by_id = {d["id"]: d.get("text", "").strip() for d in call(_build_user_prompt(product, plan))
             if isinstance(d, dict) and d.get("id")}

    # 合规重写：只重写违规段落，最多两轮
    for attempt in range(2):
        bad = {}
        for item in plan:
            txt = by_id.get(item["id"], "")
            vs = [v for v in compliance.check(txt, product.get("category")) if v.severity == BLOCK]
            if vs:
                bad[item["id"]] = {"phrases": [v.phrase for v in vs], "text": txt}
        if not bad:
            break
        print(f"   ↩️  第 {attempt+1} 轮合规重写：{len(bad)} 段")
        try:
            fixed = {d["id"]: d.get("text", "").strip()
                     for d in call(_build_user_prompt(product, plan, bad))
                     if isinstance(d, dict) and d.get("id")}
            by_id.update(fixed)
        except Exception as e:
            print(f"   ⚠️  重写调用失败，保留原稿：{e}")
            break

    out = []
    for item in plan:
        txt = by_id.get(item["id"])
        if not txt:
            print(f"   ⚠️  模型漏了段落 {item['id']}，已跳过")
            continue
        seg = {"type": item["type"], "id": item["id"],
               "sku": product["sku"], "text": txt}
        if item["type"] == "faq":
            seg["q"] = item["q"]          # faq_build.py 用它生成匹配关键词
        out.append(seg)
    return out


# ── 主流程 ───────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="电商直播话术生成")
    ap.add_argument("--products", default=str(DEFAULT_PRODUCTS))
    ap.add_argument("--sku", action="append", help="只生成指定 SKU（可多次）")
    ap.add_argument("--all", action="store_true", help="生成全部 SKU")
    ap.add_argument("--list", action="store_true", help="列出商品库")
    ap.add_argument("--backend", default="template", choices=["llm", "template"])
    ap.add_argument("--profile", default=None, help="数字人档案 id（写进 script.json）")
    ap.add_argument("-o", "--out", default="script.json")
    a = ap.parse_args()

    data = json.loads(Path(a.products).read_text(encoding="utf-8"))
    products = data.get("products", [])

    if a.list:
        print(f"商品库：{a.products}  （{len(products)} 个 SKU）\n")
        for p in products:
            print(f"  {p['sku']:<8}{p['name']:<20}{p.get('category',''):<8}"
                  f"¥{p['price']:<6}{p.get('unit','')}")
        return

    if a.all:
        picked = products
    elif a.sku:
        want = set(a.sku)
        picked = [p for p in products if p["sku"] in want]
        missing = want - {p["sku"] for p in picked}
        if missing:
            raise SystemExit(f"商品库里没有这些 SKU：{sorted(missing)}")
    else:
        raise SystemExit("请指定 --all 或 --sku XXX（或用 --list 看有哪些）")
    if not picked:
        raise SystemExit("没有选中任何商品")

    cfg = load_llm_config()
    if a.backend == "llm":
        print(f"后端：llm  模型={cfg['model']}  base_url={cfg.get('base_url') or '(未配置)'}")
    else:
        print("后端：template（不需要 API key）")

    all_segments, rejected = [], []
    for p in picked:
        plan = plan_segments(p)
        print(f"\n▸ {p['sku']} {p['name']}  （{len(plan)} 段）")
        segs = gen_template(p, plan) if a.backend == "template" else gen_llm(p, plan, cfg)

        for s in segs:
            vs = compliance.check(s["text"], p.get("category"))
            blocks = [v for v in vs if v.severity == BLOCK]
            warns = [v for v in vs if v.severity != BLOCK]
            s["compliance"] = {
                "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "block": [v.to_dict() for v in blocks],
                "warn": [v.to_dict() for v in warns],
            }
            icon = "🛑" if blocks else ("⚠️ " if warns else "✅")
            print(f"   {icon} {s['id']:<16}{len(s['text']):>4}字  {s['text'][:34]}…")
            for v in blocks:
                print(f"        🛑 {v.category}「{v.phrase}」→ {v.advice}")
            if blocks:
                rejected.append(s)
            else:
                s["video"] = None
                all_segments.append(s)

    out = {
        "meta": {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "backend": a.backend,
            "products_file": str(Path(a.products).resolve()),
            "shop_name": data.get("meta", {}).get("shop_name", ""),
        },
        "profile_id": a.profile,
        "segments": all_segments,
    }
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{'─'*66}")
    print(f"✅ 写入 {a.out}：{len(all_segments)} 段可用")
    if rejected:
        print(f"🛑 拒收 {len(rejected)} 段（存在必改级违规，未写入）：")
        for s in rejected:
            print(f"   {s['id']}：{[v['phrase'] for v in s['compliance']['block']]}")
        print("   请修商品资料或改动提示词后重跑；这些段落不会被渲染。")
        sys.exit(2)
    print(f"   估算时长：约 {sum(len(s['text']) for s in all_segments) / 4.5:.0f} 秒朗读")
    print(f"   下一步：python live_build.py -i {a.out} --profile <档案id>")


if __name__ == "__main__":
    main()
