"""Linly-Talker FastAPI 服务入口。

对外监听 0.0.0.0:6006 —— AutoDL 只把 6006/6008 映射到公网，
所以 6006 归本服务；项目的 Gradio WebUI 不启动（两者互斥）。
本服务不修改 Linly-Talker 任何源码，仅通过 adapter.py 调用它。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from . import config, storage
from .adapter import build_pipeline
from .queue import TaskQueue
from .schemas import (
    HealthResponse,
    TaskCreateJSON,
    TaskCreateResponse,
    TaskInfo,
    TranscriptBody,
    TranscriptResponse,
    VoiceCreateResponse,
    VoiceInfo,
    VoiceStatus,
)
from .security import (
    is_valid_api_key,
    load_or_create_api_key,
    preload_secrets,
    require_api_key,
    verify_result,
)
from .voices import VoiceManager, VoiceRecord

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("linly-api")

PURGE_INTERVAL_SECONDS = 600
ALLOWED_MODES = {"auto", "musetalk", "sadtalker", "wav2lip"}


# --------------------------------------------------------------------- 硬件探测
def deploy_scope_for(vram_gb: float) -> str:
    """按显存判定部署范围，门槛与部署方案一致。"""
    if vram_gb >= config.VRAM_FULL:
        return "full：可启用 MuseTalk 实时对话"
    if vram_gb >= config.VRAM_MID:
        return "mid：跳过 MuseTalk，实时对话不可用"
    return "minimal：仅 Wav2Lip + EdgeTTS"


def probe_gpu() -> dict:
    """探测 GPU。无卡模式下 torch.cuda.is_available() 为 False，这里不抛异常。"""
    info = {"gpu_available": False, "gpu_name": None, "vram_gb": None,
            "deploy_scope": "未探测", "detail": {}}
    try:
        import torch  # 延迟导入：无卡模式/未装 torch 时不应导致服务起不来

        info["detail"]["torch"] = torch.__version__
        info["detail"]["torch_cuda"] = torch.version.cuda
        if torch.cuda.is_available():
            name = torch.cuda.get_device_name(0)
            vram = round(torch.cuda.get_device_properties(0).total_memory / 1024**3, 1)
            info.update(gpu_available=True, gpu_name=name, vram_gb=vram,
                        deploy_scope=deploy_scope_for(vram))
        else:
            info["deploy_scope"] = "无可用 GPU（无卡模式？）—— 无法执行推理"
    except Exception as exc:  # noqa: BLE001
        info["deploy_scope"] = f"torch 不可用：{type(exc).__name__}: {exc}"
    return info


# --------------------------------------------------------------------- 生命周期
@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()

    # 一次性预加载 API Key 与签名密钥。**不要**留到请求里惰性加载：那是事件循环中的
    # 阻塞文件 IO，且若密钥文件中途被删会静默重新生成，让所有在途下载 URL 集体失效。
    preload_secrets()

    api_key = load_or_create_api_key()
    logger.info("=" * 68)
    logger.info("API Key: %s", api_key)
    logger.info("请求需携带请求头：X-API-Key: %s", api_key)
    logger.info("=" * 68)

    pipeline = build_pipeline()
    if config.PRELOAD_ON_STARTUP:
        logger.info("预加载模型（LINLY_PRELOAD=1）...")
        await asyncio.to_thread(pipeline.load)

    queue = TaskQueue(pipeline, public_url=config.PUBLIC_URL)
    await queue.start()
    app.state.pipeline = pipeline
    app.state.queue = queue

    # 音色训练：独立于推理队列（训练几十分钟起步，塞进单 worker 队列会把口播请求全堵死）
    voices = VoiceManager()
    await voices.startup()
    app.state.voices = voices

    async def _purge_loop() -> None:
        while True:
            await asyncio.sleep(PURGE_INTERVAL_SECONDS)
            try:
                queue.purge_expired()
            except Exception:  # noqa: BLE001
                logger.exception("清理过期任务时出错")
            try:
                # 训练中间产物可能有 GB 级，rmtree 会阻塞，丢到线程里做。
                # 这里删的是**中间产物**，已注册的音色模型保留（见 voices.purge_expired）
                n = await asyncio.to_thread(voices.purge_expired)
                if n:
                    logger.info("回收了 %d 份超期训练产物", n)
            except Exception:  # noqa: BLE001
                logger.exception("清理过期音色时出错")

    purge_task = asyncio.create_task(_purge_loop(), name="linly-purge")

    # 这一行沿用 Gradio 的日志格式，便于沿用部署方案的启动成功判定（grep 同一行）
    logger.info("Running on local URL:  http://%s:%d", config.HOST, config.PORT)
    if config.PUBLIC_URL:
        logger.info("公网入口: %s", config.PUBLIC_URL)
    if config.ENABLE_DOCS:
        logger.info("接口文档: %s/docs （LINLY_ENABLE_DOCS=1 已开启，公网部署建议关闭）",
                    config.PUBLIC_URL or "")
    else:
        logger.info("接口文档已关闭（临时需要时用 LINLY_ENABLE_DOCS=1 启动）")

    try:
        yield
    finally:
        purge_task.cancel()
        await voices.shutdown()
        await queue.stop()


# 接口文档三件套**一起**开关。只关 docs_url/redoc_url 是不够的——/openapi.json
# 仍会把完整接口结构吐给公网，那正是要防的东西。默认全关（config.ENABLE_DOCS 默认 False）。
app = FastAPI(
    title="Linly-Talker API",
    description="数字人对话系统的外部 HTTP 接口。GPU 任务串行执行，提交后轮询取结果。",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if config.ENABLE_DOCS else None,
    redoc_url="/redoc" if config.ENABLE_DOCS else None,
    openapi_url="/openapi.json" if config.ENABLE_DOCS else None,
)


# --------------------------------------------------------------------- 基础
@app.middleware("http")
async def limit_request_size(request: Request, call_next):
    """按 Content-Length 兜一层请求体上限。

    分块读取只能保护自己读的那部分；JSON 提交是框架先把整个 body 解析完才轮到我们，
    所以必须在中间件这一层先挡掉超大请求。
    """
    raw = request.headers.get("content-length")
    if raw and raw.isdigit() and int(raw) > config.MAX_REQUEST_BYTES:
        return JSONResponse(
            status_code=413,
            content={"detail": f"请求体超过 {config.MAX_REQUEST_BYTES // 1024 // 1024}MB 上限"},
        )
    return await call_next(request)


# --------------------------------------------------------------------- CORS
# ⚠️ **注册位置有讲究**：Starlette 的 `add_middleware` 是 insert(0)，而中间件栈按
# 逆序构建，所以**后 add 的在最外层**。这段必须放在 limit_request_size 之后，
# 否则 CORS 沦为内层——被 413 挡下的请求将不带 CORS 头，浏览器只能报一句不透明的
# 跨域错误，而不是干净的 413。
#
# ⚠️ `allow_methods` 的**默认值是 ("GET",)**！不显式给全的话，跨域 POST 的预检
# 直接失败——而带 X-API-Key 自定义头的请求**必然**触发预检，等于跨域调用 100% 不可用。
if config.CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.CORS_ORIGINS,
        # 鉴权走自定义头 X-API-Key 而非 Cookie，不需要放行凭据。
        # 这也让 allow_methods/allow_headers 的取值不受通配限制。
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["X-API-Key", "Content-Type"],
        max_age=600,
    )


STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/", include_in_schema=False)
async def root() -> JSONResponse:
    """服务自述。

    接口文档关闭时**不输出 docs 键**——而不是输出 `"docs": null`，
    别让 JSON 形状暗示一个实际不存在的端点。
    """
    payload = {
        "service": "Linly-Talker API",
        "version": "1.0.0",
        "ui": "/ui （浏览器效果页：上传图片 + 输入文本 → 生成 → 在线播放）",
        "health": "/api/v1/health",
        "submit": "POST /api/v1/tasks （JSON，需 X-API-Key）",
        "submit_upload": "POST /api/v1/tasks/upload （multipart，需 X-API-Key）",
        "poll": "GET /api/v1/tasks/{task_id}",
        "voice_train": "POST /api/v1/voices （上传音频训练专属音色，需 X-API-Key）",
        "voice_list": "GET /api/v1/voices",
    }
    if config.ENABLE_DOCS:
        payload["docs"] = "/docs"
    return JSONResponse(payload)


@app.get("/ui", include_in_schema=False)
async def ui() -> FileResponse:
    """浏览器效果页：上传图片 + 输入文本 → 生成口播视频 → 在线播放。

    纯静态页面，**不含任何密钥**；调用 API 所需的 X-API-Key 由使用者自己填，
    存在浏览器 localStorage 里。所以它不扩大攻击面——能打开页面的人依旧拿不到
    GPU，除非他本来就持有密钥（与直接用 curl 调 API 的权限完全一致）。
    """
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


@app.get("/api/v1/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    """健康检查，无需鉴权。用于探活与确认模型/显存状态。"""
    gpu = probe_gpu()
    pipeline = request.app.state.pipeline
    queue: TaskQueue = request.app.state.queue
    return HealthResponse(
        status="ok",
        gpu_available=gpu["gpu_available"],
        gpu_name=gpu["gpu_name"],
        vram_gb=gpu["vram_gb"],
        deploy_scope=gpu["deploy_scope"],
        pipeline_ready=pipeline.is_ready(),
        pipeline_impl=pipeline.name,
        queue_depth=queue.depth,
        max_workers=config.MAX_WORKERS,
        detail=gpu["detail"],
    )


# --------------------------------------------------------------------- 任务
def _enqueue(
    queue: TaskQueue,
    *,
    task_id: str,
    text: str,
    mode: str,
    voice: str | None,
    image_path,
) -> TaskCreateResponse:
    try:
        record = queue.create(
            text=text, mode=mode, voice=voice, image_path=image_path, task_id=task_id
        )
    except asyncio.QueueFull:
        storage.delete_quietly(image_path)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"队列已满（上限 {config.MAX_QUEUE_SIZE}），请稍后重试",
        ) from None
    return TaskCreateResponse(
        task_id=record.task_id,
        status=record.status,
        queue_position=queue.depth,
        poll_url=f"{queue.public_url}/api/v1/tasks/{record.task_id}",
    )


def _validate_text(text: str) -> str:
    text = (text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text 不能为空")
    if len(text) > config.MAX_TEXT_CHARS:
        raise HTTPException(status_code=400, detail=f"text 超过 {config.MAX_TEXT_CHARS} 字上限")
    return text


def _validate_mode(mode: str) -> str:
    if mode not in ALLOWED_MODES:
        raise HTTPException(status_code=400, detail=f"mode 必须是 {sorted(ALLOWED_MODES)} 之一")
    return mode


def _suffix_of(upload: UploadFile) -> str:
    name = (upload.filename or "").lower()
    if "." not in name:
        return ".png"
    return "." + name.rsplit(".", 1)[-1]


async def _read_capped(upload: UploadFile, limit: int, label: str = "图片") -> bytes:
    """分块读取并设上限。直接 await upload.read() 会把整个请求体灌进内存，公网接口上不能这么干。"""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                status_code=413,
                detail=f"{label}超过 {limit // 1024 // 1024}MB 上限",
            )
        chunks.append(chunk)
    return b"".join(chunks)


@app.post(
    "/api/v1/tasks",
    response_model=TaskCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_api_key)],
)
async def create_task_json(request: Request, payload: TaskCreateJSON) -> TaskCreateResponse:
    """JSON 提交：图片走 base64。适合服务端调用。"""
    queue: TaskQueue = request.app.state.queue
    text = _validate_text(payload.text)
    mode = _validate_mode(payload.mode)

    # 先生成 id 把图片落盘，图片不合法就不会在队列里留下垃圾任务
    task_id = uuid.uuid4().hex[:16]
    try:
        image_path = storage.save_base64_image(payload.image_base64, task_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return _enqueue(
        queue, task_id=task_id, text=text, mode=mode, voice=payload.voice, image_path=image_path
    )


@app.post(
    "/api/v1/tasks/upload",
    response_model=TaskCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_api_key)],
)
async def create_task_upload(
    request: Request,
    image: UploadFile = File(..., description="人像图片文件"),
    text: str = Form(..., description="要播报的中文文本"),
    mode: str = Form("auto", description="auto / musetalk / sadtalker / wav2lip"),
    voice: str | None = Form(None),
) -> TaskCreateResponse:
    """multipart 提交：直接上传图片文件。适合网页/Postman 调试。"""
    queue: TaskQueue = request.app.state.queue
    text = _validate_text(text)
    mode = _validate_mode(mode)

    suffix = _suffix_of(image)
    if suffix not in config.ALLOWED_IMAGE_SUFFIX:
        raise HTTPException(status_code=400, detail=f"不支持的图片格式 {suffix}")

    data = await _read_capped(image, config.MAX_IMAGE_BYTES)

    task_id = uuid.uuid4().hex[:16]
    try:
        image_path = storage.save_bytes(data, suffix, task_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return _enqueue(
        queue, task_id=task_id, text=text, mode=mode, voice=voice, image_path=image_path
    )


@app.get(
    "/api/v1/tasks",
    response_model=list[TaskInfo],
    dependencies=[Depends(require_api_key)],
)
async def list_tasks(request: Request, limit: int = 50) -> list[TaskInfo]:
    queue: TaskQueue = request.app.state.queue
    return [r.to_info(queue.public_url) for r in queue.list(limit=max(1, min(limit, 200)))]


@app.get(
    "/api/v1/tasks/{task_id}",
    response_model=TaskInfo,
    dependencies=[Depends(require_api_key)],
)
async def get_task(request: Request, task_id: str) -> TaskInfo:
    queue: TaskQueue = request.app.state.queue
    record = queue.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")
    return record.to_info(queue.public_url)


@app.delete(
    "/api/v1/tasks/{task_id}",
    dependencies=[Depends(require_api_key)],
)
async def cancel_task(request: Request, task_id: str) -> dict:
    queue: TaskQueue = request.app.state.queue
    record = queue.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")
    ok = queue.cancel(task_id)
    return {"task_id": task_id, "canceled": ok, "status": record.status.value}


# --------------------------------------------------------------------- 产物下载
@app.get("/api/v1/files/{filename}")
async def download_result(
    request: Request,
    filename: str,
    e: int = Query(default=0, description="签名过期时间（Unix 秒）"),
    s: str = Query(default="", description="HMAC-SHA256 签名"),
) -> FileResponse:
    """下载产物。**必须携带有效签名**：`?e=<过期时间>&s=<签名>`。

    为什么用查询串签名而不是 X-API-Key：`<video src>` 与 `<a href download>` 都是
    浏览器发起的**裸导航**，带不上自定义请求头，用头部鉴权会把在线播放和下载按钮
    一起弄坏。签名放在查询串里，浏览器可以直接播，且链接到期即作废。

    **所有失败一律返回同一个 404**（签名无效 / 已过期 / 文件不存在 / 归属校验失败），
    理由：① 不给未授权者任何「这个文件到底存不存在」的判断依据；② `<video>` 本来就
    无法从响应里区分 403 与 404（都是黑屏），区分它们没有收益。
    """
    queue: TaskQueue = request.app.state.queue
    # 先验签，再碰文件系统——resolve_result 内部的 is_file() 本身就是个存在性 oracle。
    if not verify_result(filename, e, s):
        raise HTTPException(status_code=404, detail="文件不存在或链接已过期")
    path = storage.resolve_result(filename)
    if path is None or queue.find_by_result_name(filename) is None:
        raise HTTPException(status_code=404, detail="文件不存在或链接已过期")
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=filename,
        headers={"Cache-Control": "no-store"},
    )


# --------------------------------------------------------------------- 音色训练
# 完整流程：
#   POST   /api/v1/voices                  上传音频 → 自动切片+识别 → 停在等校对
#   GET    /api/v1/voices/{id}/transcript  取识别文本
#   PUT    /api/v1/voices/{id}/transcript  提交人工校对后的文本
#   POST   /api/v1/voices/{id}/train       开始训练
#   GET    /api/v1/voices/{id}             查进度
#   GET    /api/v1/voices                  列出所有音色
#   DELETE /api/v1/voices/{id}             删除音色及其全部中间产物
#
# 上传时可传 auto_train=true 跳过校对直接训练（省事，但 ASR 错字无人纠正）。


def _voice_or_404(manager: VoiceManager, voice_id: str) -> VoiceRecord:
    rec = manager.get(voice_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"音色 {voice_id} 不存在")
    return rec


@app.post(
    "/api/v1/voices",
    response_model=VoiceCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_api_key)],
)
async def create_voice(
    request: Request,
    audio: UploadFile = File(..., description="训练音频：1~30 分钟干净人声，wav/mp3/m4a 等"),
    voice_id: str = Form(..., description="音色名，只允许字母/数字/下划线，最长 32"),
    auto_train: bool = Form(False, description="true 则跳过人工校对直接训练"),
    epochs_s1: int = Form(config.TRAIN_EPOCHS_S1, ge=1, le=50),
    epochs_s2: int = Form(config.TRAIN_EPOCHS_S2, ge=1, le=100),
) -> VoiceCreateResponse:
    """上传一段音频，开始训练一个专属音色。

    默认**不**直接训练：先自动切片 + 语音识别，产出一份标注草稿停下，
    等校对确认（见 `GET/PUT .../transcript`）后再调 `POST .../train`。
    这样能避免 ASR 错字被训练进模型 —— 实测中「待办」被识别成「代办」就是典型例子。
    """
    manager: VoiceManager = request.app.state.voices

    suffix = _suffix_of_audio(audio)
    data = await _read_capped(audio, config.MAX_AUDIO_BYTES, label="音频")
    if not data:
        raise HTTPException(status_code=400, detail="音频内容为空")

    # 先把音频落盘再建任务，避免建立了一个后续必然失败的空任务
    try:
        audio_path = storage.save_bytes(
            data, suffix, voice_id, kind="voice",
            allowed=config.ALLOWED_AUDIO_SUFFIX,
            max_bytes=config.MAX_AUDIO_BYTES, label="音频")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    try:
        rec = await manager.create(
            voice_id, audio_path, source_name=audio.filename or audio_path.name,
            auto_train=auto_train, epochs_s1=epochs_s1, epochs_s2=epochs_s2)
    except ValueError as exc:
        storage.delete_quietly(audio_path)
        raise HTTPException(status_code=409, detail=str(exc)) from None

    base = config.PUBLIC_URL
    return VoiceCreateResponse(
        voice_id=rec.voice_id,
        status=rec.status,
        poll_url=f"{base}/api/v1/voices/{rec.voice_id}",
        transcript_url=f"{base}/api/v1/voices/{rec.voice_id}/transcript",
        message=("已开始全自动训练（未校对标注）。"
                 if auto_train else
                 "已完成切片与识别，请取 transcript 校对后调 /train 开始训练。"),
    )


def _suffix_of_audio(upload: UploadFile) -> str:
    name = (upload.filename or "").lower()
    if "." not in name:
        raise HTTPException(
            status_code=400,
            detail=f"音频文件名缺少扩展名，无法判断格式；允许 {sorted(config.ALLOWED_AUDIO_SUFFIX)}",
        )
    return "." + name.rsplit(".", 1)[-1]


@app.get("/api/v1/voices", response_model=list[VoiceInfo],
         dependencies=[Depends(require_api_key)])
async def list_voices(request: Request) -> list[VoiceInfo]:
    """列出所有音色（含已完成的历史音色）。"""
    manager: VoiceManager = request.app.state.voices
    return [VoiceInfo(**r.to_info()) for r in manager.list()]


@app.get("/api/v1/voices/{voice_id}", response_model=VoiceInfo,
         dependencies=[Depends(require_api_key)])
async def get_voice(request: Request, voice_id: str) -> VoiceInfo:
    """查询训练进度。`status=done` 且 `model_ready=true` 时即可使用。"""
    manager: VoiceManager = request.app.state.voices
    return VoiceInfo(**_voice_or_404(manager, voice_id).to_info())


@app.get("/api/v1/voices/{voice_id}/transcript", response_model=TranscriptResponse,
         dependencies=[Depends(require_api_key)])
async def get_transcript(request: Request, voice_id: str) -> TranscriptResponse:
    """取 ASR 标注全文，供人工校对。

    每行 `文件名<TAB>文本`。改右侧文本即可，**不要**动左侧文件名、
    不要增删行、不要把 TAB 换成空格。
    """
    manager: VoiceManager = request.app.state.voices
    rec = _voice_or_404(manager, voice_id)
    try:
        text = manager.read_transcript(rec)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return TranscriptResponse(voice_id=voice_id, segments=rec.segments, transcript=text)


@app.put("/api/v1/voices/{voice_id}/transcript",
         dependencies=[Depends(require_api_key)])
async def put_transcript(request: Request, voice_id: str,
                         body: TranscriptBody) -> dict:
    """提交校对后的标注。之后调 `POST .../train` 开始训练。"""
    manager: VoiceManager = request.app.state.voices
    rec = _voice_or_404(manager, voice_id)
    if rec.status in (VoiceStatus.TRAINING, VoiceStatus.ASR):
        raise HTTPException(status_code=409,
                            detail=f"当前状态 {rec.status.value} 不接受校对提交")
    try:
        n = manager.write_transcript(rec, body.transcript)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"voice_id": voice_id, "saved_lines": n,
            "next": f"POST {config.PUBLIC_URL}/api/v1/voices/{voice_id}/train"}


@app.post("/api/v1/voices/{voice_id}/train", status_code=status.HTTP_202_ACCEPTED,
          dependencies=[Depends(require_api_key)])
async def train_voice(request: Request, voice_id: str,
                      body: TranscriptBody | None = None) -> dict:
    """开始训练。

    可以直接提交校对稿（body 里带 transcript），也可以先 PUT 校对稿再空跑本接口。
    训练是**长任务**（几十分钟量级），立即返回 202，之后轮询 `GET /api/v1/voices/{id}`。
    """
    manager: VoiceManager = request.app.state.voices
    rec = _voice_or_404(manager, voice_id)
    try:
        await manager.start_training(rec, body.transcript if body else None)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return {"voice_id": voice_id, "status": rec.status.value,
            "poll_url": f"{config.PUBLIC_URL}/api/v1/voices/{voice_id}",
            "note": "训练为长任务，请轮询；完成后 voice 参数用 "
                    f"finetuned:{voice_id}|<参考音频>|<参考文本>|中文"}


@app.delete("/api/v1/voices/{voice_id}", dependencies=[Depends(require_api_key)])
async def delete_voice(request: Request, voice_id: str) -> dict:
    """删除音色：连同中间产物与训练好的模型一并删除，不可恢复。"""
    manager: VoiceManager = request.app.state.voices
    rec = _voice_or_404(manager, voice_id)
    manager.delete(rec)
    return {"voice_id": voice_id, "deleted": True}
