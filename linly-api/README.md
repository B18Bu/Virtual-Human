# Linly-Talker API 服务

把 Linly-Talker 包装成可供外部调用的 HTTP 服务。**不修改 Linly-Talker 任何源码**，
服务代码独立放在 `/root/autodl-tmp/linly-api/`，与项目仓库解耦。

## 为什么是 6006

AutoDL 只把实例的 **6006 / 6008** 映射到公网，其他端口从外部不可达。
Linly-Talker 的 Gradio WebUI 默认也监听 6006（`configs.py` 中 `port = 6006`，
属于部署硬性约束禁止改动的部分）。

两者互斥，本项目选择：**FastAPI 独占 6006 对外，不启动 Gradio WebUI**。
需要网页界面时，另起 Gradio 到其他端口，通过 SSH 隧道本地访问。

公网入口（AutoDL 控制台「自定义服务」也可查看）：

```
https://<你的实例地址>
```

该地址就是 `AutoDLService6006URL` 环境变量的值，服务会自动读取它拼装 `result_url`。

## 启动 / 停止 / 重启

```bash
# 启动
nohup /root/autodl-tmp/linly-api/run.sh > /root/autodl-tmp/logs/api.log 2>&1 &

# 查看日志
tail -f /root/autodl-tmp/logs/api.log

# 确认监听
ss -lntp | grep 6006

# 停止
pkill -f "uvicorn app.main:app"

# 重启
pkill -f "uvicorn app.main:app"; sleep 2
nohup /root/autodl-tmp/linly-api/run.sh > /root/autodl-tmp/logs/api.log 2>&1 &
```

## 浏览器效果页

打开即可用，不需要装任何东西：

```
https://<你的实例地址>/ui
```

页面流程：填密钥 → 选图片 → 输入文本 → 开始生成 → 看进度条 → 视频在线播放 / 下载。
还会在顶部显示服务与 GPU 状态；产物低于 100KB 验收线时会明确提示。

**密钥不进页面。** `/ui` 是纯静态 HTML，本身不含任何密钥；页面上填的 `X-API-Key` 存在
**你自己浏览器的 localStorage**，随请求头发给后端。所以能打开页面的人依旧拿不到 GPU，
除非他本来就持有密钥——与直接用 curl 调 API 的权限完全一致，没有扩大攻击面。
（服务器上取密钥：`cat /root/autodl-tmp/linly-api/.api_key`）

页面用的正是 `POST /api/v1/tasks/upload`（multipart）这条接口。

## 鉴权

除 `/`、`/ui`、`/api/v1/health`、`/api/v1/files/{name}` 外，所有接口都需要请求头：

```
X-API-Key: <你的密钥>
```

密钥来源优先级：环境变量 `LINLY_API_KEY` > 文件 `/root/autodl-tmp/linly-api/.api_key`（首次启动自动生成，权限 0600）。
启动日志里会打印一次，也可以用 `cat /root/autodl-tmp/linly-api/.api_key` 读取。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/ui` | **浏览器效果页**：上传图片 + 输入文本 → 生成 → 在线播放。**无需鉴权**（页面本身不含密钥，见下） |
| GET | `/api/v1/health` | 健康检查：GPU、显存、部署范围、模型实现、队列深度。**无需鉴权** |
| POST | `/api/v1/tasks` | JSON 提交，图片走 base64。返回 `task_id`，HTTP 202 |
| POST | `/api/v1/tasks/upload` | multipart 提交，直接传图片文件。返回 `task_id`，HTTP 202 |
| GET | `/api/v1/tasks/{task_id}` | 轮询任务状态与进度 |
| GET | `/api/v1/tasks` | 列出最近任务 |
| DELETE | `/api/v1/tasks/{task_id}` | 取消任务 |
| GET | `/api/v1/files/{filename}` | 下载产物 |

### 为什么是异步任务制

一条口播视频要串起 TTS + 口型驱动，SadTalker 单条可能数十秒到数分钟。
同步 HTTP 请求会撞上公网网关的响应超时，因此一律「提交拿 id → 轮询 → 取产物」。

`status` 取值：`queued` → `running` → `succeeded` / `failed` / `canceled`。

### 并发

GPU 只有一张，模型常驻显存，因此 **worker 固定为 1，所有任务串行**。
超出并发上限的请求在队列里排队（上限 `LINLY_MAX_QUEUE_SIZE`，默认 64），
队列满时返回 503。这是硬件约束，不是实现限制。

## 调用示例

```bash
BASE=https://<你的实例地址>
KEY=$(cat /root/autodl-tmp/linly-api/.api_key)

# 1) 提交（multipart，最省事）
curl -s -X POST "$BASE/api/v1/tasks/upload" \
  -H "X-API-Key: $KEY" \
  -F "image=@/path/to/face.png" \
  -F "text=大家好，我是数字人小琳。" \
  -F "mode=auto"
# -> {"task_id":"a1b2c3...","status":"queued","queue_position":0,"poll_url":"..."}

# 2) 轮询
curl -s "$BASE/api/v1/tasks/a1b2c3..." -H "X-API-Key: $KEY"
# -> {"status":"running","progress":0.5,"stage":"语音合成(TTS)",...}
#    完成后 result_url 给出下载地址

# 3) 下载产物
curl -sO "$BASE/api/v1/files/a1b2c3....mp4"
```

## 配置项（环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `LINLY_PORT` | `6006` | 监听端口，**不要改**，改了公网就访问不到 |
| `LINLY_API_KEY` | 空 | 指定密钥；留空则自动生成并落盘 |
| `LINLY_MAX_WORKERS` | `1` | 固定 1，多 worker 会重复加载模型导致 OOM |
| `LINLY_MAX_QUEUE_SIZE` | `64` | 排队上限 |
| `LINLY_MAX_IMAGE_BYTES` | `10485760` | 单张图片上限 10MB |
| `LINLY_MAX_TEXT_CHARS` | `1000` | 单次文本长度上限 |
| `LINLY_TASK_TTL` | `21600` | 任务记录与产物保留 6 小时 |
| `LINLY_PRELOAD` | `0` | 设 1 则启动时预加载模型（启动慢，但首个任务快） |
| `LINLY_DEFAULT_MODE` | `auto` | 默认形象驱动方式 |
| `LINLY_SADTALKER_PREPROCESS` | `full` | SadTalker 构图模式，见下 |
| `LINLY_ENHANCER` | `0` | 设 1 启用 GFPGAN 人脸增强（更清晰，但每帧多一道推理，明显变慢） |
| `LINLY_SADTALKER_STILL` | `0` | 设 1 启用 still 模式（头部动作更少） |
| `LINLY_AUTO_MODE` | `sadtalker` | `mode=auto` 时实际走哪个引擎 |

`LINLY_SADTALKER_PREPROCESS` 的取值：

- `full`（默认）：把生成的动画人脸贴回**原图**，640x1024 的源图就得到 640x1024 的成片；
- `crop`：只返回 256x256 的**纯人脸**裁切，原图其余部分全部丢弃。体积约为 `full` 的 1/5，
  且构图不成片——部署提示词第 5 条要求产物 > 100KB，`crop` 实测只有 63KB。

注意 `full` 与 `crop` 会启用**不同的权重**（`mapping_00109` + `facerender_still.yaml`
对 `mapping_00229` + `facerender.yaml`），两套都已随模型一起下载到位。

## 安全须知

这是**裸暴露在公网**的服务，请务必注意：

1. **密钥不能泄露**。泄露等于把 GPU 免费送人。
2. `/docs`（Swagger UI）默认开启，会暴露完整接口结构。生产使用建议关闭
   （`FastAPI(docs_url=None, redoc_url=None)`）。
3. 建议加前置限流或 IP 白名单；AutoDL 侧不提供这两项。
4. 产物下载做了归属校验（文件名必须对应一个已成功登记的任务），
   但产物名本身是随机 `task_id`，不要把它当强凭证。

## 当前实现状态

| 组件 | 状态 |
| --- | --- |
| HTTP 层 / 队列 / 鉴权 / 文件下发 | ✅ 已实现 |
| `adapter.py` 中的 `RealPipeline` | ✅ 已实现并验收通过（`pipeline_impl: linly-talker`） |
| TTS | ✅ Edge-TTS（默认，无需本地权重）/ CosyVoice SFT / GPT-SoVITS 克隆 |
| 口型驱动 | ✅ SadTalker（默认，图片原生）/ Wav2Lip（最省显存，18s 出片）/ MuseTalk（⚠️ 产出无口型动作） |

`/api/v1/health` 返回的 `pipeline_impl` 是判断真假的依据：`linly-talker` 表示模型链路
已接通；`mock` 表示跑的是占位实现，**产物是文本文件不是视频**。

生成的视频默认是**整幅画面**（源图多大就多大，例如 640x1024），不是裁出来的人脸方块——
原因见 `adapter.py` 的 `_sadtalker_preprocess()`。

`voice` 参数支持的写法：

```
（留空）                                    → Edge-TTS + 默认音色 zh-CN-XiaoxiaoNeural
edge:zh-CN-YunyangNeural                    → Edge-TTS + 指定音色
cosyvoice:中文女                             → CosyVoice SFT + 指定说话人
gptsovits:<参考音频>|<参考文本>[|<语言>]      → GPT-SoVITS 声音克隆
```

几个实用的坑：

- **不是所有 Edge 音色都能用**。实测 `zh-CN-YunxiNeural` 在 edge-tts 7.2.8 上**直连也报
  `NoAudioReceived`**（微软服务侧该音色不可用），而 `Xiaoxiao` / `Yunyang` 正常。
  遇到这个报错先换个音色试，别怀疑自己的参数。
- **GPT-SoVITS 的 `<语言>` 必须写中文名**（`中文`/`英文`/`日文`/`中英混合`/`日英混合`/`多语种混合`，
  默认 `中文`），不能写 `zh`——底层 `dict_language` 是拿中文名当键的。
- **GPT-SoVITS 的参考音频必须是服务器本地路径**，且时长 **3~10 秒**，超范围会直接抛
  `OSError`。

## 部署验收

三条验收脚本都在 `/root/autodl-tmp/deploy/`，结论行以 `>>>` 开头，全通过时退出码 0：

```bash
export PATH=/root/autodl-tmp/conda/envs/linly/bin:$PATH   # 脚本要调 ffprobe

# 生成链路（直调适配层，不经过 HTTP）
/root/autodl-tmp/conda/envs/linly/bin/python -u /root/autodl-tmp/deploy/verify_b3.py

# 服务链路（提交 → 轮询 → 取产物，服务须已在 6006 监听）
/root/autodl-tmp/conda/envs/linly/bin/python -u /root/autodl-tmp/deploy/verify_c.py
```

两者都不只看字节数：还校验编码 / 分辨率 / 音轨 / 帧间确有运动，避免「静帧拼出来的大文件」蒙混过关。
