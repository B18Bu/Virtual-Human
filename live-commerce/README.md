# 电商直播数字人流水线

把**商品资料**变成一条**可循环播放、并能回应弹幕的直播流**。

```
                    ┌─ 离线 ────────────────────────────────────────┐
products.json  →  script_gen.py  →  script.json  →  live_build.py  →  playlist.mp4
   商品库            话术生成         话术稿         批量渲染+拼接      播放列表
                        ↑                              │
                  compliance.py（广告法拦阻）           ↓
                        ↑                        faq_build.py  →  FAQ 片段 + 索引
                        │                              │
                    ┌─ 在线 ──────────────────────────┴────────────┐
                    │  danmaku.py → matcher.py → player.py → RTMP  │
                    │  （清洗）     （关键词匹配）  （FIFO 喂流）    │
                    └──────────────────────────────────────────────┘
```

仓库外工具，**零侵入 Linly-Talker**。数字人形象与音色来自 `../digital-humans/` 的档案。

> 📖 **弹幕匹配的详细设计与实测数据**见 `FAQ弹幕匹配实现计划.md`

---

## 依赖关系

| 组件 | 位置 | 作用 |
|---|---|---|
| 数字人档案 | `../digital-humans/dh.py` | 提供「形象 + 音色 + 人设」 |
| 6006 服务 | `linly-api/run.sh` | 实际渲染口播视频 |
| 本目录 | `live-commerce/` | 话术 → 播放列表 → 弹幕响应 |

`live_build.py` 和 `faq_build.py` 都会 `import dh`，所以 **`../digital-humans/dh.py` 必须在位**。

---

## 快速开始

```bash
cd /root/autodl-tmp/live-commerce
P=/root/autodl-tmp/conda/envs/linly/bin/python

# ① 看有什么商品
$P script_gen.py --list

# ② 生成话术（不需要 API key）
$P script_gen.py --all --backend template --profile xiaomei -o script.json

# ③ 试跑 2 段，确认链路通
$P live_build.py -i script.json --profile xiaomei --limit 2

# ④ 全量渲染 + 拼接
$P live_build.py -i script.json --profile xiaomei

# ⑤ 主列表转 TS，并按关键帧切块（插播要落在 IDR 上，否则有花屏）
FF=/root/autodl-tmp/conda/envs/linly/bin/ffmpeg
cd build/xiaomei
$FF -y -i playlist.mp4 -c copy -f mpegts playlist.ts
mkdir -p loop && $FF -y -i playlist.ts -c copy -f segment -segment_time 1 \
    -reset_timestamps 0 -segment_format mpegts loop/clip_%04d.ts
cd ../../

# ⑥ 预渲染 FAQ 片段 + 建索引（弹幕匹配用）
$P faq_build.py -i script.json --profile xiaomei

# ⑦ 试匹配
$P matcher.py --selftest

# ⑧ 起播放器（另开一个终端发弹幕）
$P player.py --profile xiaomei --main build/xiaomei/loop \
    --output local:/tmp/live.flv --max-seconds 60
# 另一个终端：echo '{"text":"敏感肌能用吗"}' | ...  或直接在同一条管道里喂
```

**弹幕驱动的完整链路**（一条命令）：

```bash
( sleep 10; echo '{"user":"张三","text":"敏感肌能用吗"}'; sleep 30 ) | \
  $P player.py --profile xiaomei --main build/xiaomei/loop \
    --output local:/tmp/live.flv --max-seconds 40 --reset-cooldown
```

**推流**（换成有稳定带宽的机器，AutoDL 上行不达标）：

```bash
$FF -re -f mpegts -i <FIFO> -c copy -f flv rtmp://<你的推流地址>
```

---

## 接入 LLM

默认走 `template` 后端（纯模板拼装）。要换成 LLM：

```bash
cp llm_config.example.json llm_config.json
# 编辑 llm_config.json，填 api_key / base_url / model

# 【别省】先验 JSON 本身是否合法——粘贴混入换行会让 key 静默失效（见下方 ⚠️）
python -c "import json;json.load(open('llm_config.json'));print('✅ JSON 合法')"

$P script_gen.py --all --backend llm --profile xiaomei -o script.json
```

> ⚠️ **如果配了 key 却报「缺少 LLM API Key」，先查 JSON 合法性，别急着以为 key 没配上。**
> `load_llm_config()`（`script_gen.py:41-59`）在解析失败时**只打一行 `⚠️ 解析失败，忽略` 就继续跑**，
> `api_key` 保持 `None`，真正的报错要到 `gen_llm()`（`script_gen.py:196`）才抛——
> 而那句话把矛头指向「没配 key」，**方向是反的**。
> 唯一可靠的生效证据是**实跑一次**，看 health / 加载日志都不算数。

也可以用环境变量（优先级高于配置文件）：

```bash
export LIVE_LLM_API_KEY=sk-xxx
export LIVE_LLM_BASE_URL=https://api.deepseek.com/v1
export LIVE_LLM_MODEL=deepseek-chat
```

> ⚠️ `llm_config.json` 含密钥，**不要提交到任何仓库**（`.gitignore` 已覆盖 `llm_config.json.*`，
> 连修复时留的 `.broken` 备份一并挡住）。

**两个后端输出同样的 schema**，所以可以先用 template 跑通渲染和推流，配好 key 后无缝切换。

---

## 合规机制（这是本项目的重点，不是附属功能）

`compliance.py` 在**两个环节**各拦一道：

| 环节 | 位置 | 行为 |
|---|---|---|
| 生成时 | `script_gen.py` | block 级违规 → 自动让 LLM **重写**（最多 2 轮）；仍违规 → **拒收，不写进 script.json** |
| 渲染前 | 同上 | 只有通过合规的 segment 才会被 `live_build.py` 渲染 |

**规则分两级**：

- 🛑 `block` —— 明确违法，必须改（「最好」「根治」「零风险」「国家级」…）
- ⚠️ `warn` —— 高风险或依赖上下文，提示人工确认（「第一」「100%」「保证」…）

**为什么「100%」是 warn 而不是 block**：`100% 纯棉` 是**成分含量陈述**（属实即合法），
`100% 有效` 才是**功效绝对化**（违法）。一刀切会误伤正常的商品描述。

**规避写法也能抓**：归一化会抹掉空格和分隔符，`最 好` / `最-好` / `１００％`（全角）都能命中。

单个文件自测：

```bash
$P compliance.py
```

---

## 文件说明

| 文件 | 说明 |
|---|---|
| `compliance.py` | 广告法拦阻。可独立使用：`python -c "import compliance; print(compliance.check('最好的产品'))"` |
| `products.example.json` | 商品库**示例**（3 个 SKU）。字段结构即生产 schema |
| `script_gen.py` | 话术生成。`--backend llm\|template` |
| `live_build.py` | 批量渲染 + 拼接。**可断点续跑** |
| **`faq_build.py`** | **FAQ 片段预渲染 + 建匹配索引。增量渲染、合规复检** |
| **`matcher.py`** | **关键词匹配器（三档加权 + 冷却 + 全局节流）** |
| **`danmaku.py`** | **弹幕输入与清洗（去广告/表情/@，违规词不响应）** |
| **`player.py`** | **播放器：常驻 ffmpeg + FIFO 喂流，弹幕命中即插播** |
| `llm_config.example.json` | LLM 配置模板（不含真实 key） |
| `build/<profile>/` | 产物：`segments/` 单片段、`playlist.mp4` 播放列表、`concat.txt` 拼接清单 |
| `build/faq/` | FAQ 产物：`clips/` 片段（mp4 + ts）、`faq_index.json`、`matched.log`、`unanswered.log` |

---

## 弹幕匹配（FAQ 阶段一）

### 三档关键词

匹配靠关键词，不依赖任何模型。关键词分三档，权重不同：

| 档 | 来源 | 权重 | 判定 |
|---|---|---|---|
| **强** | 人工指定 + 同义词表 | 3×长度 | 单独命中即可 |
| **中** | jieba 实词 | 2×长度 | 需累计 ≥ `--min-weak-score`（默认 6） |
| **弱** | 问题本体 + 前缀 n-gram | 1×长度 | 同上 |

> **为什么要分档**：`会不会太软` 用 jieba 会切出 `不会`（观众打「不会吧」会误匹配），
> 而 `咖啡豆` 单独出现明明是强信号却被低分挡掉。两者都是 jieba 实词，靠长度区分不了。
>
> **通用疑问碎片（`会不会`/`不会`/`怎么样`…）直接拉黑**——实测
> 「大热天躺上去会不会捂汗」靠 `会不会`+`不会` 误匹配到了「会不会太软」。
> 计划文档 §7 明确：**误匹配比漏答更伤**，宁可走未命中。

### 两道节流

| 机制 | 默认 | 作用 |
|---|---|---|
| **单条冷却** | 120 秒 | 同一条 FAQ 不重复播 |
| **全局间隔** | 30 秒 | 防弹幕轰炸时画面狂切 |

冷却状态**持久化在 `faq_index.json`**，重启不丢。调试时用 `--reset-cooldown` 忽略它。

### 未命中闭环

`build/faq/unanswered.log` 记录每条未命中弹幕及其**原因**和 top1 分数：

| 原因 | 含义 | 处理 |
|---|---|---|
| `无关键词命中` | 真的没这条 FAQ | **补进 `products.json` 的 `faq[]`** |
| `命中分数不足(N<6)` | 有相近的，但证据不够 | 阈值定高了，或补关键词 |
| `冷却中` / `全局节流` | 有意抑制 | 正常，不用管 |

这正是「越用越好」的闭环：跑一场直播 → 看 `unanswered.log` → 补 FAQ → `faq_build.py --incremental` 只渲新增。

### 弹幕接入是**可替换插件**

**本系统不内置任何平台的弹幕抓取。** 抖音/淘宝的弹幕只有官方开放平台（需资质）
或第三方逆向库（封号风险、频繁失效）两条路，后者不该由本系统替你决策。

统一输入格式：

```json
{"ts": 1789564800.123, "user": "张三", "text": "敏感肌能用吗", "source": "douyin"}
```

支持 stdin（纯文本或上述 JSON）、文件 tail。外部程序接的话往播放器的 stdin 写即可。

---

## 设计取舍

**为什么批量渲染是串行的？**
6006 服务的 `MAX_WORKERS` 固定为 1（单卡只能串行跑 GPU 推理，见 `config.py:33`）。
并发提交只会排队，还可能撞 503（队列上限 64）。所以 `live_build.py` 一段一段来。

**为什么每渲染完一段就立刻下载？**
6006 的产物有 **6 小时 TTL**（`TASK_TTL_SECONDS=21600`），且过期时**连上传的图一起删**。
攒到最后一起下，跑长任务必丢产物。

**为什么支持断点续跑？**
25 段要跑十几分钟，中途可能因为显存、网络、服务重启中断。
`build/<profile>/segments/<id>.mp4` 存在即跳过，重跑不用从头来。

**为什么拼接优先免重编码？**
同一条管线产出的流参数一致，`concat demuxer + -c copy` 秒级完成且无画质损失。
实测 2 段拼接后时长 22.29s vs 片段合计 22.29s，零偏差。
只有参数不一致时才回退重编码。

---

## 已知限制

- **不支持实时互动**。单条渲染 17~42 秒（视模型冷热），单 worker 串行。
  任何「弹幕 → 出片」的实时路径都会瞬间积压。
  想互动只能**预渲染 FAQ 片段 + 弹幕匹配切换**（`script_gen.py` 已按 FAQ 生成独立片段，就是这个用途）。
- **产物不落 6006 的 TTL 保护**。`build/` 目录里的东西不会被自动清理，也不会被自动保护——自己做好备份。
- **商品库是唯一事实来源**。LLM 只被允许引用 `products.json` 里的内容。如果资料本身有错，话术就会跟着错。
- **合规检查是辅助，不是保证**。规则表覆盖常见违规，但不可能穷尽。**发布前仍需人工审核**。
