# Linly-Talker 数字人系统 · 部署与交付 README

> 基础部署：2026-09-14　|　能力扩展：2026-09-16 ~ 09-17　|　实例：AutoDL west-B 区
> 状态：**基础验收通过（11/11 项能力可用）；音色训练 / 数字人档案 / 电商直播流水线均已实测跑通**
>
> 本文档面向**复现者与接手人**。原部署过程另有三份内部文档（`交付报告.md` 正式报告、
> `部署进度与交接.md` 全过程记录与踩坑、`AutoDL部署提示词.md` 原始任务书），
> 因含实例信息未纳入本仓库；扩展阶段的文档已收录，见第十三节。

---

## 一、项目概述

把开源数字人项目 **Linly-Talker** 部署到 AutoDL 算力实例，并在其上包装一层可对外调用的
HTTP 服务与可视化界面，实现「**一张人像图片 + 一段文本 → 一段会说话的口播视频**」，
以及「语音对话 → 识别 → 应答 → 合成 → 数字人视频」的完整链路。

**核心约束**：AutoDL 实例只把 **6006 / 6008** 两个端口映射到公网，而 Linly-Talker 自带的
Gradio WebUI 默认也占 6006。二者互斥。最终采用**双端口分工**方案：

```
6006 ── FastAPI 服务（linly-api）  ── 程序调用：异步任务制，鉴权，适合批量/集成
6008 ── Gradio WebUI（linly-webui）── 人机交互：语音对话、多轮对话、实时对话
```

**全程未修改 Linly-Talker 任何源码**，仓库 `git status` 始终保持干净。

---

## 二、从零复现（本仓库不含模型权重）

> **本仓库只含代码与脚本，不含模型权重。** GitHub 对单文件有 100MB 硬限制，
> 而本项目有 **30 个文件超过该限制**（最大 3.24 GB，权重合计约 17.7 GB），
> 无论如何都推不上去。上游 Linly-Talker 官方也是这么做的。

权重从 **ModelScope** 获取（国内速度与稳定性优于 HuggingFace）。完整步骤如下：

```bash
# ① 前置：AutoDL 实例（显存 ≥ 11G 可全量部署；< 6G 只能跑 Wav2Lip + EdgeTTS）
export PIP_CACHE_DIR=/root/autodl-tmp/.cache/pip
mkdir -p /root/autodl-tmp/logs /root/autodl-tmp/.cache/pip
cd /root/autodl-tmp

# ② 把本仓库放到 /root/autodl-tmp/Virtual-Human（即当前目录结构）
#    然后装环境（Phase 1：只依赖网络，不依赖项目源码）
bash deploy/setup_env.sh

# ③ 克隆上游项目 + 子模块（Phase 2）
bash deploy/setup_project.sh

# ④ 拉模型权重（约 17.7 GB）
source /etc/network_turbo          # AutoDL 加速；用完 unset http_proxy https_proxy
pip install modelscope
python -c "from modelscope import snapshot_download; \
  snapshot_download('Kedreamix/Linly-Talker', resume_download=True, cache_dir='./', revision='master')"

# ⑤ 归位：把 ModelScope 快照缓存搬到仓库期望的路径
#    自带两阶段冲突预检，任何一个目标已存在就整体中止，绝不覆盖
bash deploy/place_models.sh --dry-run    # 先干跑看一遍
bash deploy/place_models.sh

# ⑥ 验收：对照固化清单逐文件 SHA256 校验
/root/autodl-tmp/conda/envs/linly/bin/python deploy/verify_installed.py
# 期望：参与验收 123 个文件，全部通过，0 缺失 / 0 大小不符 / 0 哈希不符
```

**为什么要有 `deploy/` 这一层**：`ms_manifest.json` 是 ModelScope 的文件清单快照
（165 个条目含 SHA256），`place_models.sh` 负责归位并预检冲突，`verify_installed.py`
负责逐文件校验。**光看官方的 `download_models.sh` 跑完不算数**——它不校验，
曾出现过 `May.json` 下载不完整却无任何提示的情况。

> **两个易踩的坑**：
> ① 两个 `mapping_*.pth.tar`（各约 149 MB）**都要**，SadTalker 按 `preprocess` 二选一使用，
> 只下一个会导致另一种模式启动失败；
> ② 归位这一步**不能省**——ModelScope 的缓存布局与项目期望的目录结构不一致。

---

## 三、目录结构与交付物

```
Virtual-Human/                 # ← 本仓库（不含权重，约 570 KB）
├── README.md                  #   本文件
├── linly-api/                 # ★ FastAPI 服务（6006）
│   ├── app/
│   │   ├── main.py            #   路由 / 中间件 / 生命周期 / 硬件探测
│   │   ├── adapter.py         #   ★ 唯一与项目耦合的适配层（RealPipeline）
│   │   ├── queue.py           #   单 worker 异步任务队列 + TTL 清理
│   │   ├── schemas.py         #   请求/响应模型
│   │   ├── security.py        #   X-API-Key 鉴权
│   │   ├── storage.py         #   上传落盘 / 产物路径 / 目录穿越防护
│   │   ├── voices.py          #   ★ 音色训练任务编排（切片 → ASR → 训练）
│   │   ├── config.py          #   全部配置（环境变量）
│   │   └── static/index.html  #   浏览器效果页（/ui）
│   ├── run.sh                 #   启动脚本
│   ├── smoke_test.py          #   HTTP 链路冒烟
│   └── README.md              #   接口文档
├── linly-webui/               # ★ Gradio WebUI 启动器（6008）
│   ├── launch.py              #   改端口 + 注入鉴权 + 钉 root_path
│   ├── run.sh
│   └── README.md
├── deploy/                    # ★ 部署与验收脚本
│   ├── verify_installed.py    #   权重 SHA256 逐文件验收（对照 ms_manifest.json）
│   ├── place_models.sh        #   权重归位（两阶段冲突预检，支持 --dry-run）
│   ├── _probe.py              #   产物探测公共模块（ffprobe/音轨/运动）
│   ├── verify_b3.py           #   生成链路验收
│   ├── verify_c.py            #   HTTP 服务链路验收
│   ├── verify_webui.py        #   对话链路验收
│   ├── fix_webui_state.py     #   WebUI 全局状态复位（见 14.1 故障速查）
│   ├── audit_capabilities.py  #   ★ 功能大盘点（11 项）
│   └── diag_musetalk*.py      #   MuseTalk 诊断脚本
├── voice-train/               # ★ 音色训练流水线（上传音频 → 专属音色）
│   ├── pipeline.py            #   编排：切片 → ASR → 校对 → 训练 → 产物
│   ├── slicer2.py             #   音频切片
│   ├── tools/i18n/            #   上游缺失的 i18n 包（恒等空壳，见第十三节）
│   └── testdata/              #   自测语料生成脚本（音频产物不入库）
├── digital-humans/            # ★ 数字人档案（形象 + 音色 + 人设 固化为档案）
│   ├── dh.py                  #   create / list / show / verify / render / export
│   └── xiaomei/profile.json   #   档案 schema 示例（二进制资产不入库）
├── live-commerce/             # ★ 电商直播流水线
│   ├── script_gen.py          #   商品 → 话术（llm / template 双后端）
│   ├── compliance.py          #   广告法拦阻（block / warn 两级）
│   ├── live_build.py          #   批量渲染 + 拼接（可断点续跑）
│   ├── faq_build.py           #   FAQ 预渲染 + 索引
│   ├── danmaku.py             #   弹幕清洗
│   ├── matcher.py             #   关键词匹配（三档加权 + 冷却 + 节流）
│   ├── player.py              #   FIFO 喂流 + 插播切换
│   ├── llm_config.example.json / products.example.json
│   ├── README.md
│   └── FAQ弹幕匹配实现计划.md
├── 项目部署进度总览.md          # ★ 全项目进度看板（接手先读这份）
├── Linly-Talker使用手册.md      #   日常操作：启动 → 调用 → 调参 → 排错
├── 音色克隆训练方案.md          #   音色微调原理与排错
├── 数字人资产固化与电商直播方案.md #   三大角度分析 + 分阶段方案
└── (部署时另外生成，不纳入本仓库)
    ├── Linly-Talker/          #   上游项目 + 全部模型权重（~19 GB）
    ├── conda/envs/linly/      #   Python 环境（~8.7 GB）
    ├── voices/<音色名>/        #   训练出的音色权重（gpt.ckpt / sovits.pth）
    └── logs/                  #   运行日志
```

---

## 四、系统架构

```
                     ┌──────────────────────── 公网（AutoDL 自定义服务）────────────────────────┐
                     │  <你的实例地址>（6006 端口）        <你的实例地址>（6008 端口）  │
                     └───────────────┬──────────────────────────────────┬─────────────────────┘
                                     │                                  │
                     ┌───────────────▼──────────────┐   ┌───────────────▼──────────────────┐
                     │  FastAPI（linly-api）        │   │  Gradio WebUI（项目自带）         │
                     │  · X-API-Key 鉴权            │   │  · 表单登录 + cookie              │
                     │  · /ui 静态效果页            │   │  · 三标签页                       │
                     │  · 异步任务队列（单 worker） │   │   · 个性化角色互动                │
                     └───────────────┬──────────────┘   │   · 数字人多轮智能对话            │
                                     │                  │   · MuseTalk 实时对话             │
                                     │                  └───────────────┬──────────────────┘
                                     │                                  │
                     ┌───────────────▼──────────────────────────────────▼──────────────────┐
                     │                        adapter / webui 调用层                        │
                     │  （严格惰性导入；os.chdir(仓库根)；PATH 注入 ffmpeg；线程数钉死）    │
                     └───────────────┬──────────────────────────────────┬──────────────────┘
                                     │                                  │
        ┌────────────────────────────▼───────────┐   ┌──────────────────▼────────────────┐
        │  TTS                                   │   │  口型驱动                          │
        │  · Edge-TTS（云端，无需权重）★默认     │   │  · SadTalker ★默认（图片原生）    │
        │  · CosyVoice-300M-SFT（本地）          │   │  · Wav2Lip（最省显存）            │
        │  · GPT-SoVITS（声音克隆）              │   │  · MuseTalk（只重绘嘴部）         │
        └────────────────────────────────────────┘   └───────────────────────────────────┘
                                     │                                  │
                                     └──────────────┬───────────────────┘
                                                    ▼
                                        ffmpeg 合成 → mp4（h264 + aac）
```

**关键设计**：`adapter.py` 是**唯一**与 Linly-Talker 内部实现耦合的文件。项目源码、函数签名
一旦变化，改动面收敛在这一个文件里；HTTP 层 / 队列 / 鉴权 / 文件下发完全不依赖项目结构。

---

## 五、技术栈

### 5.1 运行时基础

| 组件 | 版本 | 说明 |
| --- | --- | --- |
| GPU | NVIDIA RTX 4090 / 24564 MiB | 驱动 595.71.05 |
| Python | 3.10.21 | conda 环境 `linly` |
| PyTorch | **2.4.1+cu121** | torchvision 0.19.1 / torchaudio 2.4.1 |
| CUDA | 12.1 | |
| ffmpeg | 4.2.2 | 项目多处用**裸命令名**调用，依赖 PATH |

### 5.2 Web 服务层

| 包 | 版本 | 用途 |
| --- | --- | --- |
| fastapi | 0.141.1 | API 框架 |
| uvicorn | 0.52.4 | ASGI 服务器（workers 固定 1） |
| pydantic | 2.13.5 | 请求/响应校验 |
| starlette | 1.6.0 | FastAPI 底层（**与 Gradio 有版本冲突**，见复盘 11.3） |
| gradio | 4.44.1 | 项目自带 WebUI |
| gradio_client | 1.3.0 | 验收脚本调用 UI 端点 |

### 5.3 深度学习与视觉

| 包 | 版本 | 用途 |
| --- | --- | --- |
| diffusers | 0.27.2 | MuseTalk 的 UNet / VAE |
| transformers | 4.39.2 | BERT / HuBERT / Qwen |
| accelerate | 0.28.0 | `device_map="auto"` |
| mmcv | 2.1.0 | **含 CUDA ops**（需源码编译，见复盘 11.8） |
| mmdet / mmpose / mmengine | 3.3.0 / 1.3.2 / 0.10.7 | DWPose 关键点检测 |
| basicsr / gfpgan | 1.4.2 / 1.3.8 | 人脸增强（可选） |
| opencv / face_detection | 4.9.0 | 人脸检测 |

### 5.4 音频

| 包 | 版本 | 用途 |
| --- | --- | --- |
| edge-tts | 7.2.8 | 微软在线 TTS（默认引擎） |
| funasr | 1.4.15 | Paraformer 中文 ASR |
| librosa / soundfile | 0.10.2 / 0.12.1 | 音频读写与重采样 |
| imageio / imageio-ffmpeg | 2.19.3 / 0.4.7 | 项目内视频写出 |

### 5.5 配置解析

`omegaconf 2.3.1`、`HyperPyYAML 1.2.2`、`ruamel.yaml 0.19.1`（**后两者版本不兼容**，见复盘 11.4）

---

## 六、模型清单

全部权重来自 ModelScope **`Kedreamix/Linly-Talker@master`**。经 `deploy/verify_installed.py`
逐文件 SHA256 校验：

```
参与验收的文件：123 个，17.67 GB
  通过         : 123      缺失 : 0      大小不符 : 0      SHA256 不符 : 0
  无需归位（跳过）: 4  [.gitattributes / .gitmodules / configuration.json / README.md]
```

### 5.1 口型驱动

| 模型 | 文件 | 大小 | 状态 |
| --- | --- | --- | --- |
| SadTalker V0.0.2 (256) | `checkpoints/SadTalker_V0.0.2_256.safetensors` | 692 MB | ✅ **默认引擎** |
| SadTalker mapping（full 用） | `checkpoints/mapping_00109-model.pth.tar` | 148.6 MB | ✅ |
| SadTalker mapping（crop 用） | `checkpoints/mapping_00229-model.pth.tar` | 148.3 MB | ✅ |
| 3DMM BFM 拟合 | `src/config/`（`May.pth` 等） | ~25 MB | ✅ |
| Wav2Lip | `checkpoints/wav2lip.pth` | 416 MB | ✅ |
| Wav2Lip (GAN) | `checkpoints/wav2lip_gan.pth` | 416 MB | ✅ |
| Wav2Lipv2 | `checkpoints/wav2lipv2.pth` | 205 MB | ⚠️ 未接线 |
| MuseTalk UNet | `Musetalk/models/musetalk/pytorch_model.bin` | 3.2 GB | ✅（曾被误判，见 10.5） |
| MuseTalk VAE | `Musetalk/models/sd-vae-ft-mse/` | 320 MB | ✅ |
| DWPose | `Musetalk/models/dwpose/` | 389 MB | ✅ |
| face-parse-bisent | `Musetalk/models/face-parse-bisent/` | 96 MB | ✅ |
| Whisper tiny（MuseTalk 用） | `Musetalk/models/whisper/tiny.pt` | 73 MB | ✅ |

> **`mapping_00109` 与 `mapping_00229` 都会被用到**：SadTalker 按 `preprocess` 二选一
> （`full` → 00109 + `facerender_still.yaml`；`crop` → 00229 + `facerender.yaml`）。
> 只下一个会导致另一种模式启动失败。

### 5.2 语音合成（TTS）

| 模型 | 位置 | 大小 | 状态 |
| --- | --- | --- | --- |
| Edge-TTS | 云端服务，无本地权重 | — | ✅ **默认** |
| CosyVoice-300M-SFT | `checkpoints/CosyVoice_ckpt/CosyVoice-300M-SFT/` | 2.2 GB | ✅ |
| CosyVoice-300M | 同上目录 | 2.2 GB | 备用 |
| GPT-SoVITS s1 (GPT) | `GPT_SoVITS/pretrained_models/s1bert25hz-…ckpt` | 148 MB | ✅ |
| GPT-SoVITS s2 (SoVITS) | `GPT_SoVITS/pretrained_models/s2G488k.pth` | 102 MB | ✅ |
| chinese-roberta-wwm-ext-large | 同上 | 622 MB | ✅ |
| chinese-hubert-base | 同上 | 181 MB | ✅ |

### 5.3 语音识别（ASR）

| 模型 | 位置 | 大小 | 状态 |
| --- | --- | --- | --- |
| Whisper base | `Whisper/base.pt` | 145 MB | ✅ 默认 |
| Whisper tiny | `Whisper/tiny.pt` | 76 MB | ✅ |
| FunASR Paraformer-large | `FunASR/speech_seaco_paraformer_large…/` | 953 MB | ✅ **中文更优，自带标点** |
| FunASR VAD / 标点 | `FunASR/` | 287 MB | ✅ |

### 5.4 语言模型（LLM）

| 模型 | 位置 | 大小 | 状态 |
| --- | --- | --- | --- |
| Qwen-1.8B-Chat | `Qwen/Qwen-1_8B-Chat/` | 3.5 GB | ✅ 本地唯一 LLM |

### 5.5 人脸增强

| 模型 | 大小 | 状态 |
| --- | --- | --- |
| GFPGAN v1.4 | ~350 MB | ✅ 可用，默认**关闭**（`LINLY_ENHANCER=1` 开启，每帧多一道推理） |

---

## 七、上游项目与依赖来源

| 项目 | 来源 | 版本 | 说明 |
| --- | --- | --- | --- |
| **Linly-Talker** | `github.com/Kedreamix/Linly-Talker` | HEAD `dc831b3`（2026-02-10） | 主项目 |
| ChatTTS | `github.com/2noise/ChatTTS` | `f6b530ba`（v0.1.1~156） | 子模块 |
| CosyVoice | `github.com/FunAudioLLM/CosyVoice` | `6be8d0fc`（flow_cache~103） | 子模块 |
| MuseV | `github.com/TMElyralab/MuseV` | — | `.gitmodules` 里的**陈旧残留**，上游 HEAD 已无该 gitlink，非遗漏 |
| mmpose / mmdet / mmcv | OpenMMLab | 见 5.3 | MuseTalk 的人脸关键点链路依赖 |

克隆时经 `githubproxy.cc` 代理；权重走 ModelScope（国内速度与稳定性更好）。

**未安装且本次不需要**：`pytorch3d`、`TFG/requirements_nerf.txt`（ER-NeRF 专用，编译风险高）、
CosyVoice 的 `ttsfrd`（仅提供 Python 3.8 的 wheel）。

---

## 八、业务流程

### 7.1 图片 + 文本 → 口播视频（主链路）

```
① 提交          POST /api/v1/tasks        base64 图片 + 文本 + mode + voice
                 │  鉴权 → 图片落盘 uploads/ → 入队 → 返回 202 + task_id
                 ▼
② 调度          TaskQueue（单 worker 串行）→ asyncio.to_thread 跑同步推理
                 │  单卡只能串行；多 worker 会各自持有一份模型 → 爆显存
                 ▼
③ 适配层        严格惰性导入 → os.chdir(仓库根) → PATH 注入 ffmpeg
                 │
                 ├─ 0.02  准备
                 ├─ 0.06  语音合成
                 │    └─ TTS：Edge-TTS / CosyVoice / GPT-SoVITS
                 ├─ 0.42  语音就绪（已归一化为 16kHz 单声道 wav）
                 │    └─ 必要性：Edge-TTS 写出的其实是 MP3 字节，扩展名不可信；
                 │       而 SadTalker 内部用 librosa 读音频，格式不统一会直接抛错
                 ├─ 0.45  口型驱动
                 │    ├─ SadTalker：face detect → 3DMM → audio2coeff → face render
                 │    │   → 贴回原图（preprocess=full）→ ffmpeg 合成
                 │    ├─ Wav2Lip：只重绘嘴部区域，其余原样保留
                 │    └─ MuseTalk：静态视频 → 关键点/mask/latent → UNet → 贴回
                 ├─ 0.95  口型驱动完成
                 └─ 1.00  完成
                 ▼
④ 产物          产物改名 outputs/<task_id>.mp4 → 进度置 succeeded
                 ▼
⑤ 取回          GET /api/v1/tasks/{id} 轮询 → GET /api/v1/files/{name} 下载
```

### 7.2 语音对话 → 数字人应答（WebUI 链路）

```
麦克风/上传语音 → Asr()（Whisper / FunASR）
                → LLM_response()（Qwen）→ 应答文本
                → TTS_response()（Edge-TTS，volume 必须传 100）→ 应答语音
                → human_response()（SadTalker / Wav2Lip）→ 数字人视频
```

**为什么是异步任务制**：一条视频要串起 TTS + 口型驱动，SadTalker 单条数十秒；同步 HTTP
请求会撞上公网网关的响应超时。故一律「提交拿 id → 轮询 → 取产物」。

### 7.3 耗时与资源实测

| 场景 | 耗时 | 显存 |
| --- | --- | --- |
| SadTalker 冷启动（含模型加载） | 36 ~ 39 s | ~3.7 GB |
| SadTalker 热启动 | 16 ~ 18 s | ~1.9 GB |
| Wav2Lip | **18 s** | 更低 |
| MuseTalk | 34 ~ 36 s | +7.1 GB（`init_model` 一次性） |
| Edge-TTS（云端） | 0.4 ~ 1.4 s | — |
| Qwen-1.8B 加载 | ~4 s | ~4 GB |
| 全量常驻（FastAPI + WebUI + Qwen） | — | **~18 GB / 23.5 GB** |

---

## 九、使用方式

### 8.1 启动

```bash
# ① FastAPI 服务（6006）
nohup bash /root/autodl-tmp/linly-api/run.sh > /root/autodl-tmp/logs/api.log 2>&1 &

# ② Gradio 对话界面（6008）—— 约 80 秒加载完模型
nohup bash /root/autodl-tmp/linly-webui/run.sh > /root/autodl-tmp/logs/webui.log 2>&1 &

# 确认监听
ss -lntp | grep -E '6006|6008'
```

> **停止服务不要用 `pkill -f "uvicorn app.main:app"`**：它会匹配到执行这条命令的**自己的 shell**
> （命令行里含同样字符串），把自己一起杀掉。用方括号写法或按 PID：
> `pkill -f "[u]vicorn app.main:app"` / `kill $(pgrep -f '[l]aunch.py')`

### 8.2 三个入口

| 入口 | 地址 | 鉴权 | 用途 |
| --- | --- | --- | --- |
| **浏览器效果页** | `https://<你的实例地址>/ui` | 页面内填 Key | 上传图片 + 文本 → 在线播放 |
| **API** | `https://<你的实例地址>/api/v1/…` | `X-API-Key` | 程序集成 |
| **对话界面** | `https://<你的实例地址>（双 u 前缀）` | 表单登录 | 语音对话 / 多轮对话 |

```bash
cat /root/autodl-tmp/linly-api/.api_key                # API Key
cat /root/autodl-tmp/linly-webui/.webui_credentials    # linly:<密码>
```

> AutoDL 的 6008 入口与 6006 是**两个不同的域名**：6006 形如 `u<实例ID>-<UUID>.<区>.seetacloud.com:8443`，
> 6008 是同一个地址**前面多一个 `u`**。具体地址在控制台「自定义服务」里看，服务本身会自动读
> `AutoDLService6006URL` / `AutoDLService6008URL` 这两个环境变量。

### 8.3 API 调用

```bash
BASE=https://<你的实例地址>
KEY=$(cat /root/autodl-tmp/linly-api/.api_key)

# 提交（multipart 最省事）
curl -s -X POST "$BASE/api/v1/tasks/upload" -H "X-API-Key: $KEY" \
  -F "image=@/path/to/face.png" \
  -F "text=大家好，我是数字人小美。" \
  -F "mode=sadtalker"
# -> {"task_id":"...","status":"queued","queue_position":1,"poll_url":"..."}

# 轮询
curl -s "$BASE/api/v1/tasks/<task_id>" -H "X-API-Key: $KEY"

# 下载
curl -sO "$BASE/api/v1/files/<task_id>.mp4"
```

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/ui` | 浏览器效果页（无需鉴权，页面本身不含密钥） |
| GET | `/api/v1/health` | 健康检查（无需鉴权） |
| POST | `/api/v1/tasks` | JSON 提交（base64 图片） |
| POST | `/api/v1/tasks/upload` | multipart 提交 |
| GET | `/api/v1/tasks/{id}` | 轮询状态与进度 |
| DELETE | `/api/v1/tasks/{id}` | 取消任务 |
| GET | `/api/v1/files/{name}` | 下载产物 |

### 8.4 `mode` 与 `voice` 参数

```
mode = auto | sadtalker | wav2lip | musetalk

voice =
  （留空）                                   → Edge-TTS + zh-CN-XiaoxiaoNeural
  edge:zh-CN-YunyangNeural                   → Edge-TTS + 指定音色
  cosyvoice:中文女                            → CosyVoice SFT
  gptsovits:<参考音频>|<参考文本>[|<语言>]     → GPT-SoVITS 声音克隆
```

---

## 十、验收

### 9.1 六条成功标准（对照任务书）

| # | 标准 | 结果 |
| --- | --- | --- |
| 1 | 代码与权重在数据盘，系统盘无新增模型/环境 | ✅ 数据盘 32G/50G；系统盘 2.1G/30G |
| 2 | conda 环境在数据盘 | ✅ `/root/autodl-tmp/conda/envs/linly` |
| 3 | `torch.cuda.is_available()` 为 True | ✅ RTX 4090 / 23.5 GB |
| 4 | 6006 端口 LISTEN | ✅ `LISTEN 0 2048 0.0.0.0:6006` |
| 5 | 端到端生成 mp4 > 100KB | ✅ **372 KB**（640×1024，h264+aac） |
| 6 | 输出交付报告 | ✅ `交付报告.md` |

### 9.2 功能大盘点（`deploy/audit_capabilities.py`，11/11）

| 功能 | 结果 | 证据 |
| --- | --- | --- |
| Edge-TTS 默认音色 | ✅ | 音轨 RMS 3020 |
| Edge-TTS 指定音色 | ✅ | `zh-CN-YunyangNeural` |
| CosyVoice SFT | ✅ | `中文女`，RMS 2940 |
| GPT-SoVITS 声音克隆 | ✅ | RMS 3191 |
| 口型驱动 SadTalker | ✅ | 640×1024 / 有声 / 画面在动 |
| **口型驱动 Wav2Lip** | ✅ | 640×1024 / 有声 / 画面在动 / 18s |
| **口型驱动 MuseTalk** | ✅ | 640×1024 / 有声 / 画面在动（曾误判，见 10.5） |
| Whisper 语音识别 | ✅ | 中文转写正确 |
| FunASR 语音识别 | ✅ | `大家好，我是数字人，小美，很高兴认识你。`（自带标点） |
| 多轮对话记忆 | ✅ | 记住 3/3 次 + 无历史对照组 |
| 对话轮次累计 | ✅ | history 正常累积 |

### 9.3 复跑命令

```bash
export PATH=/root/autodl-tmp/conda/envs/linly/bin:$PATH
cd /root/autodl-tmp

python deploy/verify_installed.py                    # 权重 SHA256（约 97s）
python deploy/verify_b3.py                           # 生成链路
python deploy/verify_c.py                            # HTTP 服务链路
python deploy/verify_webui.py [Qwen]                 # 对话链路
python deploy/audit_capabilities.py --with-lipsync   # 11 项功能全量盘点
```
结论行以 `>>>` 开头，全通过时退出码 0。

---

## 十一、实现复盘

> 这一节是本次交付最有价值的部分。**大部分时间不是在「让它跑起来」，而是在「确认它跑对了」。**
> 下面每一条都是实际踩过、且**报错信息完全指不到真因**的问题。

### 10.1 验收方法论的演进：从「看字节数」到「六项检查」

最初的任务书给的验收标准是「mp4 文件大于 100KB」。这个指标**能被三种错误方式满足**，
我们三种全踩了：

| 被漏掉的情况 | 为什么 100KB 拦不住 | 补的检查 |
| --- | --- | --- |
| 音轨全静音 | 长度正常、编码合法、`ffprobe` 照报 `aac/16kHz/4.288s` | **解 PCM 算 RMS** |
| 画面是静帧 | 静帧拼出来的 mp4 一样大 | **中心区域帧间运动** |
| 只有一张浮空的人脸 | 256×256 的裁切视频也能到 100KB | **分辨率 + 构图**（看源图尺寸） |

**最终六项检查**（`deploy/_probe.py`）：

```
>100KB ｜ 有视频流 ｜ 有音频流 ｜ 音轨非静音（RMS>50）｜ 分辨率非空壳 ｜ 画面在动
```

**阈值全部来自实测标定，不是拍脑袋**：

- 运动阈值 **1.0**（判据是「全片变化最大的 1/8×1/8 分块」）—— 静帧基线 **0.056**、
  静音期产物 **0.513**、真实产出 **2.71 ~ 10.72**。取 1.0 高于静帧 18 倍、
  高于「静音期静嘴」2 倍、低于最低真产出 2.7 倍。**为什么不用整块中心区**见 10.5 末段。
- 音轨阈值 **50** —— 静音音轨 RMS 恰好是 **0.0**，正常中文语音 **1000~5000**。

**另外两条方法论**：

- **弱断言不如不做**。多轮对话最初用「颜色」测，两轮答案措辞雷同，答对了也说明不了什么。
  改成「记一个 4 位数 → 追问」，并加**无历史对照组**（清会话后问同样的问题，
  模型会胡诌一个别的数字）——这才真正证明了上下文在起作用。
- **单次抽样会骗人**。多轮记忆单跑一次可能全绿也可能全红，改成 3 次取成功率（判据 ≥2/3）：
  既能拦住「history 根本没接上」这种真故障（那是 0/3），又不会因小模型偶发胡诌而误报。

### 10.2 六个「能跑通但结果不对」的静默故障

这一类最危险：**任务返回 `succeeded`、产物格式完全合法，但内容是错的。**

| # | 现象 | 根因 | 定位难度 |
| --- | --- | --- | --- |
| 1 | 视频只有 63KB，够不到 100KB | SadTalker 的 `preprocess="crop"` **根本不执行人脸回贴**，直接返回 256×256 裁脸 | 中（要看源码分叉） |
| 2 | 音轨全静音 | Edge-TTS 的 `VOLUME` 参数是**反的**（`100-volume`），传 0 得到 `-100%` | **高**（ffprobe 完全无感） |
| 3 | 语音识别返回空字符串 | 喂给 Whisper 的素材本身是静音（问题 2 的连带） | **高**（看着像 ASR 坏了） |
| 4 | 浏览器永远登不进 Gradio | 网关不转发 `x-forwarded-host`，前端把 `root` 推断成 `http://localhost:6008`，登录请求发到**用户自己电脑** | **极高**（服务端日志一条登录记录都没有） |
| 5 | 证书明明对却提示密码错误 | 我用 `secrets.token_urlsafe()` 生成的密码里**同时含字母 `o` 和数字 `0`** | 中（是可用性问题） |
| 6 | CosyVoice / GPT-SoVITS 完全不可用 | 依赖版本漂移 + 模块名抢占（见 11.3 / 11.4） | 中 |

**共性教训**：

> **「服务端能认证」不等于「浏览器能登录」；「有音轨」不等于「有声音」；
> 「任务 succeeded」不等于「结果正确」。**
> 每一层都要用**对端视角**去验，而不是用自己方便的方式去验。

问题 4 尤其典型：我上一轮用 `curl` 直接打 `/login` 拿到 `{"success":true}`，就判定鉴权没问题——
但 curl 走的是路径请求，**不经过前端拼 URL 那一步**，而 bug 恰好就在那一层。

### 10.3 依赖版本漂移：Gradio ↔ Starlette

`requirements_webui.txt` 只写了 `gradio==4.*`（装到 4.44.1），而服务侧的 `fastapi>=0.115`
装到 0.141.1 并拉来 **starlette 1.6.0** —— Gradio 4.44.1 从未声称兼容 starlette 1.x。
**这个冲突一直存在，只是此前从未启动过 Gradio，所以没人发现。**

打了三个 site-packages 补丁（均未动项目源码）：

| 文件 | 症状 | 修法 |
| --- | --- | --- |
| `gradio_client/utils.py` | JSON Schema 的 `additionalProperties` 允许是布尔值，上游当 dict 递归 → `TypeError: argument of type 'bool' is not iterable` | 加类型判断 |
| `gradio/routes.py` | 按 Starlette **旧签名**调 `TemplateResponse` → 首页 500 | 改用新签名 |
| `gradio/components/video.py` | 项目在无字幕时把**视频路径**塞进字幕字段 → `ValueError`（视频已生成成功却返回不了） | 非 .srt/.vtt 视为无字幕 |

> **前两个的症状极具误导性**：它们让 Gradio 首页 500，随后 Gradio 的 `url_ok()` 探活失败，
> `launch()` 直接拒绝启动并报
> 「**When localhost is not accessible, a shareable link must be created**」。
> 这个报错会把人往代理/网络配置上带，**完全指不到真因**。下次见到它，
> 先去看服务端日志里首页路由的异常。

### 10.4 模块名抢占：`utils` 被偷走

```
AttributeError: Can't get attribute 'HParams' on <module 'utils' from
    '.../Musetalk/musetalk/utils/utils.py'>
```

`s2G488k.pth` 是 pickle 存的，里面按**模块名 `utils`** 引用 `HParams`，而 `HParams` 定义在
`GPT_SoVITS/utils.py`。但 **`import TFG` 会往 sys.path 里塞 `./Musetalk`**，
`Musetalk/musetalk/utils/utils.py` 先一步以顶层 `utils` 的身份进了 `sys.modules`。
`sys.path.append('GPT_SoVITS/')` 是**追加到末尾**的，靠路径顺序赢不回来
→ 只能在加载权重前用 `importlib` 按**文件路径**显式纠正。

同类还有 `ruamel.yaml 0.19.1` 把 `max_depth` 属性从 loader 上拿掉，而 HyperPyYAML
（CosyVoice 用它读配置）自己造 loader、跳过了设置那一步 → CosyVoice 完全不可用。

**教训**：多个项目共用一套 conda 环境时，**顶层模块名冲突**和**配置库的小版本差异**
是两个高发雷区，且症状离根因很远。

### 10.5 MuseTalk：一次**已被推翻**的误判（2026-09-14 复核）

> **结论先说：MuseTalk 的口型一直是正常的。** 下面记录的「无口型动作」是被
> **静音素材**污染出来的假故障。保留全过程，因为它示范了「验收样本本身不可信」
> 有多难被发现，以及一个钝指标能把人带多远。

**当时的现象**：`mode=musetalk` 任务成功、mp4 合法（142,590 字节 / 640×1024 / 106 帧 /
有声），但抽帧比对第 10 帧与第 60 帧「完全一致」，嘴始终闭着不动。

**当时的排查**（`deploy/diag_musetalk*.py`）：

| 环节 | 观测 | 当时的判断 |
| --- | --- | --- |
| 准备阶段 | 100 帧、人脸框合理、mask 非零 33%、latents 正常 | ✅ |
| Whisper 音频特征 | 无 nan/inf，相邻 chunk 逐元素平均差 0.237 | ✅ 在变 |
| UNet 输入条件 | 逐批 `std` 极差 1.53 | ✅ 进了模型 |
| **UNet 输出** | 逐批 `std` 极差 **0.008** | ❌ 判为「对条件无响应」 |

已排除：权重完整（3.4 GB，SHA256 验证过）、`load_state_dict` strict 全量加载、
抽查 `attn2.to_out`/`conv_in` 等权重**量级正常无零初始化**、`diffusers 0.27.2` 对
`attention_head_dim=8` 仍按旧语义解析为 **8 头**（与权重匹配）、
`inference()` 与 `inference_noprepare()` 的 UNet 调用逐行一致。

**复核推翻了两点：**

1. **样本是静音的。** 上述证据全部来自当天 12:19 的产物，而 Edge-TTS 的 VOLUME 反向
   bug（10.2 的问题 2）下午才修好——**喂静音，MuseTalk 自然闭嘴**，「嘴不动」是**症状**
   不是病因。修复后同一流程重跑，抽帧可见明显开合露齿，嘴部隔 20 帧差从 **1.093 → 3.818**。
2. **「输出 std 极差」是个钝指标。** 不同内容可以有完全相同的 std，拿它判断
   「模型是否响应条件」必然漏判。改成**固定同一 latent、只换条件**的对照实验后：
   条件 std 从 0.97 变到 2.81 时，输出 latent 相对差 **6.6%（真音频 vs 全零）~ 7.8%**，
   解码后下半脸像素差 **3~4/255**（静帧基线 0.01）——**条件确实在驱动输出**。

**复核用的硬证据**（DWPose 真实唇部关键点 vs 音频包络，三段产物同一素材）：

| 引擎 | 张口度极差 | 与音频包络相关 r | 最佳滞后 |
| --- | --- | --- | --- |
| MuseTalk | 8.3 px | **0.350** | **0 帧** |
| SadTalker | 16.7 px | 0.576 | 0 帧 |
| Wav2Lip | 18.3 px | 0.291 | 0 帧 |

三者**都在 0 滞后**，MuseTalk 的相关系数介于另两者之间 → 口型与音频同步。
张口幅度约为另两个的一半，属模型特性（inpaint 型只重绘嘴部，不动头、不眨眼），
**不是故障**。想要更大的嘴部动作就用 SadTalker。

**顺带修正了判据本身**：原来的「画面在动」取**画面正中 50%×50%** 的相邻帧最大平均差，
实质在测「全画面抖动」，对只重绘嘴部的引擎会被面积稀释——同一个真在动的产物，
中心区判据只有 0.269（阈值 0.5 → 误判 ❌），而它嘴部的真实运动是 2.71。
已改为**最大分块帧间运动**（8×8 网格，阈值 1.0），把面积这个无关变量归一化掉：

| 视频 | 旧判据（中心区） | 新判据（最大分块） |
| --- | --- | --- |
| 静帧基线 | 0.006 | **0.056** |
| MuseTalk（静音期） | 0.072 | **0.513** |
| MuseTalk（修复后） | 0.269 ❌ | **2.713** ✅ |
| Wav2Lip | 1.496 ✅ | 5.765 ✅ |
| SadTalker | 2.436 ✅ | 10.719 ✅ |

> 另有一个真 bug 但不在我们路径上：本分支的 `get_landmark_and_bbox` 返回 **3** 个值，
> `TFG/MuseTalk.py:281` 只接 2 个（那个函数是坏的，服务走的是 `inference_noprepare`）。

**教训（三条，都很通用）**：

1. **验收样本本身要先验。** 那条静音音轨在 ffprobe 眼里全绿——长度、编码、采样率
   都正常，只有解 PCM 量 RMS 才看得出来。样本不可信时，后面所有结论都不成立。
2. **别拿一个会互相抵消的标量当判据。** 「std 极差」既受条件影响也受内容影响；
   固定其余变量做对照实验，才是可靠的因果证据。
3. **判据要匹配被测对象的形态。** 「全画面在动」对会摇头的 SadTalker 合理，
   对只重绘嘴的 MuseTalk / Wav2Lip 就是错的口径——**同一个判据在不同引擎上不等价**。

### 10.6 一个自我纠错：Wav2Lip 其实一直可用

我最初在适配层里写了：

```python
# 「本项目里它需要另一套 checkpoint 与依赖，尚未接线」
raise NotImplementedError("wav2lip 尚未接入")
```

**这个判断是错的。** Wav2Lip 的权重（`wav2lip.pth` 等 3 个）和封装
（`TFG/Wav2Lip.py`）**本项目都自带**，`face_detection` 依赖也装好了。
本次已接入并实测通过（18 秒出片，比 SadTalker 快一倍）。

**教训**：把「我没试过」写成「它缺依赖」是一种危险的表述——它会变成后续所有人的前提。

> 附带发现：WebUI 里的 Wav2Lip 选项**是坏的**（项目 bug）：`human_response` 中
> `crop_pic_path` 只在 SadTalker 分支被赋值，Wav2Lip 分支引用它就是 `UnboundLocalError`。
> 适配层直接调引擎类，绕开了这个 bug。

### 10.7 架构决策的复盘

**决策 1：FastAPI 独占 6006，不启动 Gradio。**
当时是为了「项目源码零改动」——`configs.py` 的 `port = 6006` 是硬性约束。
回头看这个决策**是对的但不够**：它保住了源码干净，却没保住「开箱即用」，
后来还是得补一个仓库外的启动器（`linly-webui/`）来把 Gradio 拉起来。
好在启动器同样做到了零改动（在 `from configs import *` 之前改 `configs.port`/`configs.ip`）。

**决策 2：服务代码放仓库外（`linly-api/`）。**
**非常正确。** 全程 Linly-Talker 的 `git status` 都是干净的，重新克隆也不丢服务代码；
所有与项目耦合的部分收敛在 `adapter.py` 一个文件里。

**决策 3：严格惰性导入。**
`import VITS` 会在模块顶层加载 BERT+HuBERT（1.2 GB 显存），`import TFG` 会加载 DWPose
（0.3 GB）——**导入即加载**，因为两个包都用 `try/except` 包住了导入，**失败只打印一句中文提示
就继续**（拿到的是残缺的包，后续调用才报错，排错成本极高）。
惰性导入让服务空载只占 1 MiB，代价是首个任务多等约 20 秒。**权衡合理。**

### 10.8 环境类踩坑（摘要）

| 坑 | 说明 |
| --- | --- |
| 平台注入 `OMP_NUM_THREADS=0` | 对 libgomp 是**非法值**；且 `nproc` 报 224 而 cgroup 只给 25 核。已在启动脚本里钉死为 8 |
| `mmcv` 编译必须装 `ninja` | 否则 `MAX_JOBS` **静默失效**，2 分 41 秒变 2 小时 |
| `mmcv` 默认构建隔离会装成 `mmcv-lite` | **无 CUDA ops**，且不报错 |
| pip 下载慢的真凶 | CDN 限速 HTTP/1.1，`curl --http2` 取 wheel 再本地装 |
| `basicsr` 的构建环境白下 1.6GB torch | `setup_requires` 不钉版本，卡住整个 `pip install` |
| GitHub raw 正文通道被掐 | HEAD 正常但 GET 0 字节，`g2p_en` 导入会**挂死**；改用 jsDelivr / 预置 nltk 数据 |
| 项目用**裸命令名**调 ffmpeg | 只认 PATH，不经 `conda activate` 启动时子进程 rc=127 |
| `save_video_with_watermark` 留游离 mp4 | 临时文件名是裸 `uuid4().mp4`，写在 CWD（=仓库根），进程崩了会弄脏 `git status` |
| `nltk` 数据须预先落盘 | 否则 `g2p_en` 导入时联网下载会被掐死 |

---

## 十二、已知限制与后续建议

### 11.1 已知限制

| # | 限制 | 影响 | 建议 |
| --- | --- | --- | --- |
| 1 | **MuseTalk 嘴部动作幅度约为另两个引擎的一半** | 观感偏「含蓄」（不是故障，见 10.5） | 要更大的嘴部动作走 SadTalker / Wav2Lip |
| 2 | **每请求重载 SadTalker 权重** | 热启动仍有约 20 秒加载开销 | `TFG/SadTalker.py:196-199` 的 `test2()` 每次重建三个模型，属项目源码，硬约束禁止改动 |
| 3 | **LLM 是进程级单例** | 多人同时用会**串上下文** | 项目设计；点「清空会话」重置。生产需按用户隔离 |
| 4 | **LLM 只有 1.8B** | 多轮记忆不稳（数字类 3/3、姓名类 1/3） | 换更大模型；本地只有这一个 |
| 5 | **`zh-CN-YunxiNeural` 音色不可用** | 指定它会报 `NoAudioReceived` | 微软服务侧问题，换音色即可 |
| 6 | **`/root/.cache/pip` 980MB 在系统盘** | 与硬性约束措辞有出入（非模型/环境） | 可删，见 12.3 |
| 7 | **公网可达性未自证** | 容器内解析不到公网域名，验收只走回环 | 需从外网访问一次确认 |
| 8 | **`/docs` 默认开启** | 暴露接口结构 | 生产建议 `docs_url=None` |

### 11.2 口型质量的复查方法（MuseTalk 复核时用的）

「口型对不对」不要靠肉眼扫一遍就下结论——**眼睛分不清「音轨静音」和「嘴不动」，
而这正是最初踩的坑**。两步走：

```bash
export PATH=/root/autodl-tmp/conda/envs/linly/bin:$PATH
/root/autodl-tmp/conda/envs/linly/bin/python -u /root/autodl-tmp/deploy/audit_capabilities.py --with-lipsync
```

**① 嘴在不在动** —— 用 `deploy/_probe.py` 的「最大分块帧间运动」（8×8 网格，阈值 1.0）。
静帧 0.056、静音期产物 0.513、真实产出 2.71~10.72。

**② 口型跟不跟音频** —— 需要 DWPose 唇部关键点：

- 取 68 点人脸的外唇上下中点（`keypoints[23:91]` 的 `[74]`/`[80]`），
  除以嘴角宽度做尺度归一，得到「张口度」时间序列；
- 音频侧用 `ffmpeg` 解 16 kHz PCM、按视频 fps 做 hop 取 RMS 得包络；
- 两者做归一化互相关，扫 0~10 帧滞后。实测基线：

| 引擎 | 张口度极差 | r | 最佳滞后 |
| --- | --- | --- | --- |
| MuseTalk | 8.3 px | 0.350 | 0 帧 |
| SadTalker | 16.7 px | 0.576 | 0 帧 |
| Wav2Lip | 18.3 px | 0.291 | 0 帧 |

**读法**：r 落在 0.3~0.6 属正常（口型与包络本就是弱相关，不必追求更高）；
**但滞后必须是 0 帧**——跑出 3 帧以上（>100 ms）才说明音画不同步，那才是真问题。

> ⚠️ 复核时先用「嘴部暗像素占比」当代理指标，测出 MuseTalk 滞后 4 帧（160 ms），
> 换成 DWPose 真关键点后是 **0 帧**——**代理指标的「滞后」其实是它自己的噪声**。
> 结论性指标要用能直接解释的那个，别用相关量凑出来的代理。

### 11.3 可回收磁盘

```bash
rm -rf /root/.cache/pip              # 系统盘 980M
rm -rf /root/autodl-tmp/wheels       # 数据盘 2.7G（torch 早已装好）
rm -rf /root/autodl-tmp/.cache/pip   # 数据盘 2.3G
```

### 11.4 保存环境

**不要自行关机/重启/更换镜像。** 建议在 AutoDL 控制台「更多操作」中**保存镜像**，
以便后续换机复用环境（当前数据盘 32G/50G，系统盘 2.1G/30G）。

---

## 十三、能力扩展（2026-09-16 ~ 09-17）

基础部署验收通过后，在其上叠了三层能力。**三者同样不改 Linly-Talker 任何源码**。

```
商品库 products.json ─► script_gen.py ─► script.json ─► live_build.py ─► playlist.mp4
                          ▲                                   │
                    compliance.py（广告法拦阻）                  ▼
                                                       faq_build.py（预渲染 + 索引）
                                                              │
弹幕 ─► danmaku.py ─► matcher.py ─► player.py ─► 插播切换 ─► RTMP / 本地 FLV
```

| 层 | 目录 | 一句话 |
| --- | --- | --- |
| 音色训练 | `voice-train/` + `linly-api/app/voices.py` | 上传音频 → 训练专属音色 → `voice=finetuned:<名>` |
| 数字人档案 | `digital-humans/dh.py` | 把「形象 + 音色 + 人设」固化成可校验、可迁移的档案 |
| 电商直播 | `live-commerce/` | 商品库 → 话术 → 批量渲染 → 播放列表 → 弹幕匹配插播 |

### 13.1 音色训练

**上传音频 → 自动切片/ASR → 人工校对 → 训练 → 使用**，全流程实测通过，7 个端点
（`POST /api/v1/voices` 等）。产物落在 `/root/autodl-tmp/voices/<名>/{gpt.ckpt, sovits.pth, meta.json}`。

| 项 | 值 |
| --- | --- |
| 实现位置 | `voice-train/pipeline.py` + `linly-api/app/voices.py` |
| 实测耗时 | **88.1 秒**（16 段 / 80 秒语料 / 4+4 轮） |
| 仓库改动 | **零** |

> ⚠️ **关键认知**：Linly-Talker 自带的 GPT-SoVITS **训练链路被上游摘掉了**（缺 `tools/` 包、
> config 缺训练键、数据拼装缺失）。但**不需要去上游仓库找**——本仓库的 `voice-train/tools/i18n/`
> 是一个十几行的恒等空壳，补上它 + 用项目自带的 train 脚本即可跑通。

> ⚠️ **两个坑**：① 音色权重路径**不能含 `pretrained` 子串**（`VITS/GPT_SoVITS.py:351` 会走错分支）；
> ② 每个音色常驻约 **240 MB 显存**，且缓存**无上限无淘汰**——10 个音色约 2.4 GB。

### 13.2 数字人档案

**动机**：在此之前系统里根本没有「保存数字人」这个概念——上传的图 **6 小时后会被
`queue.py:179-180` 连记录一起删掉**。档案把易失的运行时状态变成可校验、可迁移的资产。

| 项 | 值 |
| --- | --- |
| 工具 | `digital-humans/dh.py` |
| 档案结构 | `<档案>/{profile.json, avatar.*, ref.wav, ref.txt, preview.mp4}` |
| 子命令 | `create / list / show / verify / render / export` |

**三个核心设计**：① 形象 **sha256 校验**（防换图）；② `preprocess` **锁定 `full`**
（避开 SadTalker 的 crop 坑，见第十四节）；③ **参考音频复制进档案**——补掉「音色与形象不自包含」的缺口。

### 13.3 电商直播流水线

| 组件 | 说明 |
| --- | --- |
| `script_gen.py` | `--backend llm\|template` **双后端**，输出同一 schema；LLM 出稿失败可退回模板 |
| `compliance.py` | 广告法拦阻，block / warn 两级。能抓 `最 好`（中间插空格）、全角 `１００％` 等规避写法 |
| `live_build.py` | 批量渲染 + 拼接，**可断点续跑** |
| `matcher.py` | 三档加权关键词 + 冷却 + 全局节流，**自测 19/19** |
| `danmaku.py` | 弹幕清洗（去广告 / 表情 / @），13 类用例全通过 |
| `player.py` | 常驻 ffmpeg + FIFO，**插播响应 ≈ 55 ms** |

**实测**：25 段 → **4.3 分钟成片**（255.06 s，与片段之和**零偏差**），17.5 MB，总耗时 11.2 分钟。

> 🔴 **一条方向性结论：不做「实时互动直播」。** 单条渲染 17~42 秒、单 worker 串行，
> 任何「弹幕 → 实时出片」的链路都会**瞬间积压**。可行路径只有
> **「预渲染 FAQ + 匹配切换」**（即上表已实现的方案）。
>
> 🔴 **第二条：渲染与推流必须分离。** AutoDL 上行带宽实测 **1.1~25.5 Mbps，波动 23.4 倍**，
> 5 次里 2 次低于 2 Mbps，而 720p 推流需要**持续稳定**的 2~4 Mbps。⇒ 本机可渲染，
> **不可推流**，需另找推流机。

### 13.4 关键实测数据（产能规划以此为准）

| 指标 | 值 |
| --- | --- |
| 单条口播渲染 | **42 秒（冷态）/ 17~18 秒（预热）** |
| 音色训练 | 88.1 秒（16 段 / 80 秒语料 / 4+4 轮） |
| 25 段成片 | 11.2 分钟 → 4.3 分钟播放列表 |
| 13 条 FAQ 预渲染 | 约 6 分钟 |
| 弹幕插播响应 | ≈ 55 ms |
| ffmpeg 启动开销 | **1.8 秒**（所以不能靠 kill/restart 切片段） |
| AutoDL 上行带宽 | 1.1~25.5 Mbps（波动 23.4×）🔴 不能推流 |
| 成片规格 | 640×1024 / 248~442 KB |

### 13.5 尚未完成

| # | 事项 | 卡在哪 |
| --- | --- | --- |
| B2 | **真实推流** | AutoDL 上行不达标，需确定推流机并测其上行 |
| B3 | 平台推流地址 | 需抖音 / 淘宝 / 视频号 各自开放平台授权 |
| B4 | 弹幕接入方式 | 官方开放平台（需资质）/ 第三方库（有风险）/ 人工输入 |
| T1 | 插播点 PTS 不连续 | 每次插播约 46 条 DTS 警告（**产物仍可正常解码**）。`-avoid_negative_ts` 等参数实测均无效 |
| T5 | `linly-api` 7 条已知缺陷 | 其中 **`GET /api/v1/files/{filename}` 无鉴权、`/docs` 未关闭** 属公网暴露，建议优先修 |

---

## 十四、附录：故障速查

| 现象 | 先查这里 |
| --- | --- |
| 对话界面点生成，页面上只显示「错误」两个字 | **先去 `logs/webui.log` 看真实报错**——「错误」只是 Gradio 的中文文案。<br>最常见的是界面下拉框与后端全局变量脱节（如 `AttributeError: 'Wav2Lipv2' object has no attribute 'test2'`）：`webui.py` 用**模块级全局变量**存当前模型，用 API 直接调 `*_model_change` 会让后端切走而界面不动。<br>跑 `deploy/fix_webui_state.py` 恢复，并把界面下拉框选成一致 |
| 视频很小 / 只有一张脸 | `mode` 是否用了 `crop`？SadTalker 需 `full` 才回贴原图 |
| 视频没声音 / ASR 返回空 | 量音轨 RMS（`deploy/_probe.py:audio_rms`）；静音一般是 TTS 的 volume 传错 |
| 浏览器登不进 Gradio | `curl -s localhost:6008/ \| grep -o '"root":"[^"]*"'` 是否输出公网地址 |
| Gradio 报「localhost is not accessible」 | **不是网络问题**，去看服务端日志里首页路由的异常 |
| `NoAudioReceived` | 换一个 Edge 音色，微软服务侧个别音色不可用 |
| `Can't get attribute 'HParams'` | 顶层 `utils` 被 MuseTalk 抢占，见 11.4 |
| `'Loader' object has no attribute 'max_depth'` | `ruamel.yaml` 与 HyperPyYAML 版本不匹配 |
| **MuseTalk 标签页报错，页面只显示「错误」两个字** | 见下表：该标签页有**严格的操作顺序**，真实报错只在 `logs/webui.log` |
| 子进程 ffmpeg rc=127 | PATH 里没有 conda 环境的 bin |
| 重启命令把自己杀了 | `pkill -f` 模式匹配到了自己的 shell，改用方括号写法 |

### 14.1 WebUI 的 MuseTalk 标签页：必须按顺序操作

界面上的「错误」两个字零信息量，真实原因一定在 `logs/webui.log`。该标签页有
**两个必须先做的前置动作**，顺序错了就报下面这两条：

| 报错（`logs/webui.log`） | 触发的操作 | 正确做法 |
| --- | --- | --- |
| `AttributeError: 'MuseTalk_RealTime' object has no attribute 'audio_processor'` | 没点「加载MuseTalk模型(传入视频前先加载)」就点了生成 | **先点「加载MuseTalk模型」**（`init_model()` 之后才会有这些属性，约 8.6 GB 显存） |
| `TypeError: object of type 'NoneType' has no len()`<br>（`musetalk/utils/utils.py:49 datagen`） | 换了视频后**没等素材准备完**就点生成 | 上传/更换视频会触发 `prepare_material`（关键点 + mask + latent），**等它跑完**再生成 |

固定顺序：**加载模型 → 传视频（等素材准备）→ 生成**。
换视频后要重跑素材准备，换模型后要重新加载。

> 这两条是上游 `webui.py` 的固有行为（全局状态 + 无前置校验），属项目源码、
> 硬约束禁止改动，所以只能靠**用法说明**规避。用 `linly-api`（6006）调 `mode=musetalk`
> 不受影响——适配层会把模型加载与素材准备自动按正确顺序做完。

> 另一种「报错」不是 MuseTalk 的问题：`AttributeError: 'Wav2Lipv2' object has no attribute 'test2'`
> 是全局状态与下拉框脱节，跑 `deploy/fix_webui_state.py` 复位即可（见 10.6）。
