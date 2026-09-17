# Linly-Talker 使用手册

> 面向**使用者**的操作文档：怎么启动、怎么调用、怎么换音色/换形象、出问题怎么查。
>
> **与既有文档的分工**（避免重复，各文档权威范围如下）：
>
> | 文档 | 权威范围 |
> |---|---|
> | **本文档** | 日常操作：启动 → 调用 → 调参 → 排错 |
> | `linly-api/README.md` | 6006 服务的对外契约（环境变量、端点表） |
> | `linly-webui/README.md` | 6008 Gradio 界面的操作细节 |
> | `README.md`（根目录） | 架构、模型清单、验收结论、实现复盘 |
> | `部署进度与交接.md` | 部署全过程的踩坑记录（**叙事体，非操作手册**） |
> | `音色克隆训练方案.md` | 微调一个专属音色（一次性离线任务） |
>
> ⚠️ 本文档**修正**了既有文档中的两处错误，见 §2.4 与 §8.1。
>
> 核验日期：2026-09-16（结构完整性与权重已实跑复核）

---

## 0. 一句话认知

这是一个**单向的「文本 → 语音 → 口播视频」流水线**，不是对话系统。

给它一段文字 + 一张人脸图，它返回一段这段人脸说这句话的视频。**没有** LLM、**没有** ASR、**没有**多轮对话——尽管目录里有 `LLM/`、`ASR/`、`Qwen/` 这些文件夹，它们**都没有接线**（见 §9）。

---

## 1. 系统现状速查

| 项 | 值 |
|---|---|
| 项目代码 | `/root/autodl-tmp/Linly-Talker`（HEAD `dc831b3`，`git status` 干净） |
| conda 环境 | `/root/autodl-tmp/conda/envs/linly`（Python 3.10.21 + PyTorch 2.4.1+cu121） |
| GPU | RTX 4090 / 24564 MiB |
| 权重 | 123 个文件 / 17.67 GB，已对照官方清单逐个校验通过 |
| 日志目录 | `/root/autodl-tmp/logs/` |
| 磁盘 | 50G 中已用 27G，**可用约 24G** |

**两个对外服务，互斥占用同一张卡：**

| 端口 | 服务 | 公网地址 | 适合场景 |
|---|---|---|---|
| **6006** | FastAPI（`linly-api/`） | `<你的实例地址>` | 程序调用、批量生成 |
| **6008** | Gradio 界面（`linly-webui/`） | `<你的实例地址>` | 人在浏览器里点着用 |

> 🔴 **注意 6008 的地址是双 `u` 开头**（`<你的实例ID>`），6006 是单 `u`（`<你的实例ID>`）。写错就 404。
> 两者也会**抢显存**——Gradio 加载的模型更多，显存紧张时先停它。

---

## 2. 启动 / 停止 / 重启

### 2.1 启动 FastAPI（6006）

```bash
nohup bash /root/autodl-tmp/linly-api/run.sh > /root/autodl-tmp/logs/api.log 2>&1 &
```

> ⚠️ **必须写 `bash`**。`run.sh` 没有可执行位（`-rw-r--r--`），直接 `nohup .../run.sh` 会以 **Exit 126** 静默失败。
> 想省掉 `bash` 就 `chmod +x /root/autodl-tmp/linly-api/run.sh`。

确认起来了（**看 `pipeline_impl` 这一项**，它才是真假服务的判据）：

```bash
sleep 30 && curl -s localhost:6006/api/v1/health
```

期望看到 `"pipeline_impl":"linly-talker"` 且 `"pipeline_ready":true`。
若显示 `"mock"`，说明真管线加载失败、退回了占位实现——**此时产出的「视频」其实是文本文件**，别被「任务成功」骗了。

### 2.2 启动 Gradio（6008）

```bash
nohup bash /root/autodl-tmp/linly-webui/run.sh > /root/autodl-tmp/logs/webui.log 2>&1 &
```

> 同样没有可执行位，也要写 `bash`。

> 6008 的登录凭据在 `/root/autodl-tmp/linly-webui/.webui_credentials`。

### 2.3 首次调用会慢

模型是**惰性加载**的：服务起来时并没有把模型读进显存，**第一个请求**才触发加载，会多花 1~3 分钟。之后的任务恢复正常速度。

想避免首个请求被网关掐断，可以先跑一个预热请求，或设 `LINLY_PRELOAD=1` 让服务启动时就加载。

### 2.4 🚨 停止服务：必须用方括号写法

```bash
# ✅ 正确
pkill -f "[u]vicorn app.main:app"

# ❌ 错误 —— 会把你自己的会话一起杀掉
pkill -f "uvicorn app.main:app"
```

**为什么**：`pkill -f` 匹配的是完整命令行，而**执行这条命令的 shell 自己的命令行里就含有这个字符串**，于是它把自己也匹配上了。`[u]vicorn` 是正则写法，匹配 `uvicorn` 但自身字面量不是 `uvicorn`，从而避开自杀。

> ⚠️ `linly-api/README.md` 第 36、39 行给的是**没有方括号的错误版本**（那份还被复制进了 `Virtual-Human/` 发布仓库）。**以本文档为准。**

---

## 3. 两条路径怎么选

| 你要做的事 | 用哪条 |
|---|---|
| 程序里调、批量生成、集成到别的系统 | **6006 FastAPI** |
| 手动试试效果、想有个界面点点看 | **6008 Gradio** |
| 需要换音色/换形象做实验 | 两者都行，但 **API 更可控**（参数是显式的） |

**先看效果用 6008，真要产出用 6006。**

---

## 4. 路径 A：6006 FastAPI

### 4.1 鉴权

除 4 个端点外都要带 `X-API-Key` 请求头：

```bash
export BASE=http://127.0.0.1:6006          # 服务器本机
# export BASE=<你的实例地址>   # 公网
export KEY=$(cat /root/autodl-tmp/linly-api/.api_key)
```

**免鉴权端点**：`/`、`/ui`、`/api/v1/health`。

**产物下载是个例外，但它也不「免鉴权」**：`/api/v1/files/{文件名}` 不用 `X-API-Key`，
而是要求 URL 上带**短期签名**（`?e=<过期时间>&s=<签名>`）。原因很实际——
`<video src>` 和 `<a href download>` 是浏览器发起的**裸导航，带不上自定义请求头**，
用头部鉴权会把在线播放和下载按钮一起弄坏。

**所以：一律用轮询返回的 `result_url` 原样下载，不要自己拼 URL。**
签名有效期默认 1 小时；过期就重新拉一次任务状态取新 URL。

### 4.2 提交任务（方式一：上传图片文件）

```bash
curl -s -X POST "$BASE/api/v1/tasks/upload" \
  -H "X-API-Key: $KEY" \
  -F "image=@/root/autodl-tmp/Linly-Talker/inputs/example.png" \
  -F "text=大家好，我是数字人" \
  -F "mode=sadtalker" \
  -F "voice=edge:zh-CN-XiaoxiaoNeural"
```

- `image` 后缀白名单：`.png/.jpg/.jpeg/.bmp/.webp`，单张 ≤ 10 MB
- `mode` 省略则默认 `auto`；`voice` 省略则默认 Edge 的晓晓

### 4.3 提交任务（方式二：JSON + base64）

```bash
curl -s -X POST "$BASE/api/v1/tasks" \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d "{\"text\":\"大家好\",\"image_base64\":\"$(base64 -w0 face.png)\",\"mode\":\"sadtalker\"}"
```

> ⚠️ 走 JSON 时，**图片后缀被硬编码成 `.png`**——即使你传的是 JPEG 字节也会存成 `.png`。功能不受影响，但排查时别被文件名误导。
> 文本上限 **1000 字**，超出报 400。

### 4.4 响应与轮询

两个提交端点都返回 **HTTP 202**：

```json
{"task_id":"0c71dbdebc764908","status":"queued","queue_position":1,"poll_url":"/api/v1/tasks/0c71dbdebc764908"}
```

轮询直到 `status` 变成 `succeeded`：

```bash
TASK=0c71dbdebc764908
curl -s "$BASE/api/v1/tasks/$TASK" -H "X-API-Key: $KEY"
```

**任务完整字段**（`TaskInfo`）：

| 字段 | 含义 |
|---|---|
| `task_id` | 任务 ID（16 位 hex） |
| `status` | `queued` / `running` / `succeeded` / `failed` / `canceled` |
| `mode` | 实际使用的口型引擎 |
| `text` | 提交的文本 |
| `progress` | 0.0 ~ 1.0 |
| `stage` | **中文进度文案**，如「语音合成（Edge-TTS）」「口型驱动（SadTalker）」 |
| `created_at` / `started_at` / `finished_at` | 时间戳 |
| `elapsed_seconds` | 耗时 |
| `result_url` | 产物下载地址（成功后才有） |
| `result_bytes` | 产物字节数 |
| `error` | 失败原因（失败时才有） |

### 4.5 下载产物

```bash
# 用任务状态里的 result_url 原样下载——下载签名就在那个 URL 的查询串里
URL=$(curl -s "$BASE/api/v1/tasks/$TASK" -H "X-API-Key: $KEY" \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["result_url"])')
curl -sO "$URL"
```

⚠️ **不要自己拼** `$BASE/api/v1/files/$TASK.mp4`——拼出来的没有签名，一律 404。

### 4.6 任务管理

```bash
# 列出最近任务（limit 默认 50，最大 200）
curl -s "$BASE/api/v1/tasks?limit=20" -H "X-API-Key: $KEY"

# 取消任务（已结束的任务返回 canceled:false）
curl -s -X DELETE "$BASE/api/v1/tasks/$TASK" -H "X-API-Key: $KEY"
```

### 4.7 错误码

| 码 | 含义 |
|---|---|
| 400 | 参数非法（文本空/超 1000 字、`mode` 或 `voice` 取值非法、图片后缀不在白名单） |
| 401 | `X-API-Key` 缺失或错误 |
| 404 | 任务不存在 / 产物不存在 |
| 413 | 请求体超限 |
| 422 | Pydantic 校验失败（字段类型/缺失） |
| 503 | 队列满（上限 64），稍后重试 |

### 4.8 为什么是异步任务制

同步请求会撞公网网关超时——一条视频生成要几十秒到几分钟。所以是**提交拿 task_id → 轮询 → 取产物**。

**并发**：worker 固定为 **1**（单卡串行）。多个任务会排队，不是并行。队列深度可在 health 里看 `queue_depth`。

---

## 5. 路径 B：6008 Gradio

浏览器打开 `<你的实例地址>`，用 `.webui_credentials` 里的凭据登录。

界面分三个标签页，**语音对话 / 多轮对话 / 实时对话**。操作顺序和注意事项见 `linly-webui/README.md`——那份写得很细，这里不重复。

> 🔴 **用 Gradio 时务必手动改 `preprocess` 参数**，默认值会毁掉成片，见下一节。

---

## 6. 参数详解

### 6.1 `mode`：口型驱动引擎

| mode | 出片形态 | 速度 | 说明 |
|---|---|---|---|
| **`sadtalker`** | 整幅人像，跟随头部动作 | 中 | **默认，推荐**。图片原生路径 |
| `wav2lip` | 仅嘴部区域被替换 | 快 | 最省显存；产物会先落到仓库内固定路径，服务会自己搬走 |
| `musetalk` | 半身，口型幅度较小 | 慢 | 需先把图片合成 2 秒静态视频，多一层不确定性 |
| `auto` | = `sadtalker` | — | 可用环境变量 `LINLY_AUTO_MODE` 改默认 |

**选型建议**：默认用 `sadtalker`。要**更大的口型幅度**也走 `sadtalker`（MuseTalk 的张口度约为 SadTalker 的一半，属模型特性）。显存吃紧才退到 `wav2lip`。

> ⚠️ `mode=musetalk` 的可用性在文档间口径不一：`linly-api/README.md` 说「已澄清可用」，而 `/ui` 页面下拉里写的是「实测口型不动，勿用」。**实测复核结论是口型正常**（DWPose 唇部关键点与音频包络 0 滞后、r=0.350），只是幅度小。这条文档债尚未统一，**以实测为准**。

### 6.2 🚨 `preprocess`：最容易踩的坑（仅 Gradio 路径）

**`webui.py:517` 和 `webui.py:622` 的默认值是 `'crop'`**，`app_talk.py:28` 更是硬编码 `crop`。

`crop` **不是画质调优项，它决定成片是「一张浮空的脸」还是「整幅人像」**：

| | `crop`（默认） | `full`（正确） |
|---|---|---|
| 分辨率 | 256×256 | 与源图相同（如 640×1024） |
| 体积 | **约 63 KB** | **约 260~370 KB** |
| 构图 | 只有一张脸，源图其余部分全丢 | 整幅人像 |

**在 Gradio 界面里必须手动把 `preprocess` 从 `crop` 改成 `full`。**

> 根因：`src/facerender/animate.py` 里只有 `'full' in preprocess` 才会把人脸贴回原图。
> 连带陷阱：`full` 用 `mapping_00109` + `facerender_still.yaml`，`crop` 用 `mapping_00229` + `facerender.yaml`，**两套配套，别只换一个**。
>
> ✅ **6006 API 路径没有这个问题** —— 适配层已固定走 `full`，产出 640×1024。

### 6.3 `voice`：音色（四种写法）

```
（留空）/ default / auto                    → Edge-TTS，默认 zh-CN-XiaoxiaoNeural
edge:<音色名>                                → Edge-TTS 指定音色
cosyvoice[:<说话人>]                         → CosyVoice，默认「中文女」
gptsovits:<参考音频>|<参考文本>[|<语言>]      → GPT-SoVITS 克隆（见 §6.5）
zh-CN-XxxNeural                              → 直接丢音色名也行（便利写法）
```

### 6.4 可用音色清单

**Edge-TTS（在线合成，无需本地权重）**——实测可列出 14 个中文音色：

| 音色名 | 说明 |
|---|---|
| `zh-CN-XiaoxiaoNeural` | **默认**，女声 |
| `zh-CN-XiaoyiNeural` | 女声 |
| `zh-CN-YunjianNeural` | 男声 |
| `zh-CN-YunxiNeural` | 男声 |
| `zh-CN-YunxiaNeural` | 男声（童声） |
| `zh-CN-YunyangNeural` | 男声（新闻播报） |
| `zh-CN-liaoning-XiaobeiNeural` | 东北官话 |
| `zh-CN-shaanxi-XiaoniNeural` | 中原官话 |
| `zh-HK-HiuGaaiNeural` / `zh-HK-HiuMaanNeural` / `zh-HK-WanLungNeural` | 粤语 |
| `zh-TW-HsiaoChenNeural` / `zh-TW-HsiaoYuNeural` / `zh-TW-YunJheNeural` | 台湾国语 |

> ⚠️ **不是每个音色都能用**。已知 `zh-CN-YunxiNeural` 会报 `NoAudioReceived`。换音色后**务必先试一句短的**。

**CosyVoice SFT（本地权重，离线可用）**——7 个内置说话人（已从 `spk2info.pt` 实读）：

```
中文女    中文男    日语男    粤语女    英文女    英文男    韩语女
```

### 6.5 GPT-SoVITS 克隆（零样本）

**当前部署的「音色克隆」就是零样本**：不需要训练，给一段参考音频 + 它的文本，模型当场模仿。底模从不更换。

```bash
curl -s -X POST "$BASE/api/v1/tasks/upload" \
  -H "X-API-Key: $KEY" \
  -F "image=@face.png" \
  -F "text=这是要合成的内容" \
  -F "voice=gptsovits:/root/autodl-tmp/ref.wav|这是参考音频的文字|中文"
```

**三条硬约束**：

1. **参考音频必须是服务器上的本地路径**——文件上传接口**只收图片，不收音频**。要换参考音频，得先把音频放到服务器上（scp / JupyterLab 上传）。
2. **时长必须 3~10 秒**。短于 3 秒或长于 10 秒会直接 `raise OSError`。
3. **语言必须写中文名**：`中文` / `英文` / `日文` / `中英混合` / `日英混合` / `多语种混合`。**传 `zh` 会 KeyError**。

### 6.6 换形象

**每次任务都要带图，没有默认形象图。** 把 `image` 换成你的人脸图即可。

建议：正脸、五官清晰、无遮挡、分辨率不低于 512×512、**构图要留够头肩**（因为成片是整幅人像，源图裁得越紧，可动范围越小）。

---

## 7. 典型流程

### 7.1 生成一条口播视频（API 完整流程）

```bash
BASE=http://127.0.0.1:6006
KEY=$(cat /root/autodl-tmp/linly-api/.api_key)

# ① 提交
TASK=$(curl -s -X POST "$BASE/api/v1/tasks/upload" \
  -H "X-API-Key: $KEY" \
  -F "image=@/root/autodl-tmp/Linly-Talker/inputs/example.png" \
  -F "text=大家好，欢迎来到我的频道" \
  -F "mode=sadtalker" \
  -F "voice=cosyvoice:中文女" | python -c "import sys,json;print(json.load(sys.stdin)['task_id'])")
echo "task=$TASK"

# ② 轮询
while true; do
  S=$(curl -s "$BASE/api/v1/tasks/$TASK" -H "X-API-Key: $KEY")
  echo "$S" | python -c "import sys,json;d=json.load(sys.stdin);print(d['status'],d['progress'],d.get('stage',''))"
  echo "$S" | grep -q '"status":"succeeded"' && break
  echo "$S" | grep -qE '"status":"(failed|canceled)"' && { echo "$S"; exit 1; }
  sleep 5
done

# ③ 下载（用带签名的 result_url，不要自己拼路径）
URL=$(curl -s "$BASE/api/v1/tasks/$TASK" -H "X-API-Key: $KEY" \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["result_url"])')
curl -sO "$URL"
ls -la $TASK.mp4
```

**验收一条**：产物体积应在 **260~370 KB** 量级（640×1024）。若只有 ~63 KB，就是踩了 §6.2 的 crop 坑。

### 7.2 换音色的三条路

| 想要的效果 | 怎么做 | 成本 |
|---|---|---|
| 换一个标准音色 | `voice=edge:<音色名>` 或 `cosyvoice:<说话人>` | 零成本，改个参数 |
| 克隆某个人的音色 | 把 3~10 秒干净人声放到服务器 → `voice=gptsovits:<路径>|<文本>|中文` | 零训练，即时可用 |
| 音色**更像、更稳** | **训练专属音色** → 见 §7.3 | 上传音频 + 约 2~95 分钟训练 |

### 7.3 训练并使用专属音色（API 全流程）

> 💡 **不想写命令？** 打开 `http://127.0.0.1:6006/ui`（或公网同路径），页面上有
> **「训练专属音色」** 面板：拖入音频 → 填音色名 → 开始 → 在文本框里校对 → 提交训练，
> 全程可视化。训好的音色会出现在 **「我的音色」** 列表里，点「用这个音色」自动回填调用模板。
>
> 下面是用 curl 的等價流程，适合脚本化或排查。

上传一段音频，服务自动切片、识别、训练，产出一个可用音色。**全程不需要下载任何东西**（ASR 模型已在本地）。

```bash
BASE=http://127.0.0.1:6006
KEY=$(cat /root/autodl-tmp/linly-api/.api_key)
```

**① 上传音频，建音色**

```bash
curl -s -X POST "$BASE/api/v1/voices" -H "X-API-Key: $KEY" \
  -F "audio=@/path/to/你的录音.wav" \
  -F "voice_id=myvoice" \
  -F "auto_train=false"
```

音频要求：**1~30 分钟干净人声**（wav/mp3/m4a 等），单人、无背景音乐、无回声。

**② 等识别完成**（轮询到 `status=reviewing`）

```bash
curl -s "$BASE/api/v1/voices/myvoice" -H "X-API-Key: $KEY"
```

**③ 取识别文本，人工校对**

```bash
curl -s "$BASE/api/v1/voices/myvoice/transcript" -H "X-API-Key: $KEY"
```

返回每行 `文件名<TAB>文本`。**改右侧文本即可**，不要动左边文件名、不要增删行。

> 🔴 **这一步不要跳过。** 实测中 ASR 把「**待办**事项」识别成了「**代办**事项」——
> 这类同音字错误会**直接教坏模型**，训练完才发现就白跑一趟。
> 想跳过就用 `auto_train=true`（见下方）。

**④ 提交校对稿并开始训练**

```bash
curl -s -X POST "$BASE/api/v1/voices/myvoice/train" -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' \
  -d "{\"transcript\":\"$(cat 校对好的文件)\"}"
```

**⑤ 轮询训练进度**（几十分钟量级，`stage` 会显示当前阶段）

```bash
curl -s "$BASE/api/v1/voices/myvoice" -H "X-API-Key: $KEY"
# status: asr → training → done
# stage: 音频切片 / 语音识别 / HuBERT 特征提取 / 训练 SoVITS / 训练 GPT / 完成
```

**⑥ 用这个音色出片**

```bash
curl -s -X POST "$BASE/api/v1/tasks/upload" -H "X-API-Key: $KEY" \
  -F "image=@face.png" \
  -F "text=这是用我自己的音色合成的" \
  -F "voice=finetuned:myvoice|/服务器上的参考音频.wav|参考音频的文字|中文"
```

> `finetuned:` 是**新开的语法**，与既有的 `gptsovits:` 并存、互不影响。
> 参考音频仍需 3~10 秒且是服务器本地路径（同 §6.5 的三条硬约束）。

**其他端点**

| 操作 | 命令 |
|---|---|
| 列出所有音色 | `GET /api/v1/voices` |
| 查单个音色 | `GET /api/v1/voices/{id}` |
| 删除音色（连模型一起删，不可恢复） | `DELETE /api/v1/voices/{id}` |
| 跳过校对直接训练 | 建音色时加 `-F "auto_train=true"` |
| 调训练轮数 | 建音色时加 `-F "epochs_s2=8" -F "epochs_s1=8"` |

**训练要多久**：取决于数据量与轮数。实测 16 段 / 约 80 秒语料、4~8 轮，**全程约 95 秒**（含预处理）。
数据越多、轮数越多越慢，但一般不超过几十分钟。

**产在哪**：训练好的音色在 `/root/autodl-tmp/voices/<音色名>/`，含 `gpt.ckpt` + `sovits.pth` + `meta.json`。
中间产物在 `/root/autodl-tmp/linly-api/voice_work/<音色名>/`（含完整训练日志 `api_train.log`）。

**显存**：每个音色常驻约 240 MB。共享的 BERT/HuBERT 只有一份，不随音色数量增长。

> 原理说明与手工排错参见 `音色克隆训练方案.md`；命令行方式 `python /root/autodl-tmp/voice-train/pipeline.py --help`。

---

## 8. 排错手册

### 8.1 成片只有 63 KB / 是一张浮空的脸

**原因**：用了 `preprocess=crop`。见 §6.2。
**解决**：Gradio 里手动改成 `full`；或改用 6006 API（已固定 full）。

### 8.2 视频里人物不张嘴 / 或音轨是静音

**先分辨是「没口型」还是「没声音」**——这两件事的根因完全不同。

**若是静音**：检查 Edge-TTS 的 VOLUME 参数。
> 项目里 `TTS/EdgeTTS.py:146` 做的是 `volume = 100 - volume`，**这个参数是反的**。传 `0` 会得到一条**格式合法、长度正常、内容全零**的静音音轨——**`ffprobe` 查不出来**，必须量 PCM 才能发现。适配层已固定传 `100`（满音量）。

**若是没口型**：注意 `mode=musetalk` 的张口幅度约为 SadTalker 的一半，看起来可能「像没动」。用 DWPose 唇部关键点复核，别靠肉眼。

### 8.3 `ffmpeg: command not found`

**ffmpeg 只在 conda 环境里**，系统 PATH 没有：

```bash
/root/autodl-tmp/conda/envs/linly/bin/ffmpeg -version   # 4.2.2
```

项目里有些地方用 `subprocess.run(shell=True)` 调裸 `ffmpeg` 命令——适配层已把解释器同级 `bin` 前置到 PATH 兜底。**你自己写脚本时要显式带路径或先 `conda activate linly`。**

### 8.4 `libgomp: Invalid value for environment variable OMP_NUM_THREADS`

平台注入了 `OMP_NUM_THREADS=0` / `MKL_NUM_THREADS=0`，**0 对 libgomp 是非法值**。而且 `nproc` 报 224 核，cgroup 实际只给 25 核。

`run.sh` 已钉死为 8。**自己起 Python 前也要设**：

```bash
export OMP_NUM_THREADS=8 MKL_NUM_THREADS=8
```

### 8.5 `Can't get attribute 'HParams'`（加载 GPT-SoVITS 时）

**顶层 `utils` 模块被劫持**：`import TFG` 会把 `Musetalk/musetalk/utils/utils.py` 注册成顶层 `utils`，导致 `torch.load(s2G488k.pth)` 找不到正确的 `HParams`。

适配层的 `_restore_gptsovits_utils_module()` 会按文件路径把 `GPT_SoVITS/utils.py` 抢回来。**自己写脚本时若同时用了 TFG 和 GPT-SoVITS，要复刻这个处理。**

### 8.6 服务起来了但产出是文本文件

`/api/v1/health` 里 `pipeline_impl` 是 `mock`。说明真管线加载失败**静默退回了占位实现**。去看 `/root/autodl-tmp/logs/api.log` 里的报错。

### 8.7 任务一直 `queued` 不动

单卡串行，前面有任务在跑。看 `queue_depth`。或前一个任务卡死了——用 §2.4 的方括号命令重启服务。

### 8.8 显存不足（OOM）

Gradio（6008）加载的模型比 API 多。**先停 Gradio**，再跑 API 任务。

---

## 9. 能力边界（重要，避免误期待）

**✅ 已接线、可用：**

| 域 | 能力 |
|---|---|
| TTS | Edge-TTS（默认）、CosyVoice SFT（7 说话人）、GPT-SoVITS 零样本克隆 |
| 口型驱动 | SadTalker（默认）、Wav2Lip、MuseTalk |

**❌ 目录里有代码，但没接线（调不到）：**

| 域 | 未接线的部分 |
|---|---|
| LLM | ChatGLM / Qwen / Qwen2 / Linly / Llama2Chinese / ChatGPT / Gemini / GPT4Free / QAnyThing —— **全部** |
| ASR | FunASR / Whisper / OmniSenseVoice —— **全部** |
| TTS | ChatTTS、PaddleTTS、XTTS |
| 数字人 | NeRFTalk、MuseV、Wav2Lipv2 |

> **不要期待多轮对话。** 尽管 FastAPI 的自我描述写的是「数字人对话系统的外部 HTTP 接口」，代码里**没有任何 LLM/ASR 调用路径**，也没有会话概念——每个任务都是独立的一次文本播报。
>
> 想加对话能力，需要**新写代码**，不是配置问题。

**其他限制：**

- ❌ **音频无法通过 API 上传**——参考音频必须是服务器本地路径
- ❌ **没有「注册音色」「选择说话人」端点**
- ❌ **没有默认形象图**——每个任务必须带图
- ⚠️ `outputs/verify/` 下的手工验收产物**不可下载**（文件名含 `/`，被安全校验拦掉）
- ✅ `/docs`（Swagger）**默认已关闭**（`/redoc`、`/openapi.json` 一并关）。本地调试用
  `LINLY_ENABLE_DOCS=1` 启动才打开

---

## 10. 磁盘与维护

当前 50G 用了 27G。若需腾空间（按收益排序）：

| 目标 | 可释放 | 说明 |
|---|---|---|
| `Linly-Talker/Qwen/` | **3.5 G** | LLM 权重，**未接线**，不用就删 |
| `conda/pkgs/` | 645 M | conda 包缓存，`conda clean -a` |
| `/root/autodl-tmp/.tmp/` | 238 M | 临时残留 |
| `Linly-Talker/temp/` | 41 M | 中间产物，服务会自行清理 |
| `Linly-Talker/results/` | 27 M | 历史产物 |

**产物清理**：任务产物有 **6 小时 TTL**，过期后连上传的图片一起自动删除。要长期保留就及时下载。

---

## 附：一页速查

```bash
# 启动（必须带 bash，run.sh 无可执行位）
nohup bash /root/autodl-tmp/linly-api/run.sh > /root/autodl-tmp/logs/api.log 2>&1 &

# 停止（方括号！）
pkill -f "[u]vicorn app.main:app"

# 健康检查（看 pipeline_impl 是否为 linly-talker）
curl -s localhost:6006/api/v1/health

# 密钥
cat /root/autodl-tmp/linly-api/.api_key

# 公网
#   6006: <你的实例地址>   (单 u)
#   6008: <你的实例地址>  (双 u)

# 浏览器效果页
#   http://127.0.0.1:6006/ui
```
