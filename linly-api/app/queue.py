"""单卡串行任务队列。

GPU 推理是阻塞式 Python 调用，而且一张卡往往装不下两个常驻模型，
因此固定单 worker 顺序执行，多余请求排队——这是硬件约束，不是实现偷懒。
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import config, storage
from .adapter import BasePipeline
from .schemas import TaskInfo, TaskStatus
from .security import sign_result

logger = logging.getLogger(__name__)


@dataclass
class TaskRecord:
    task_id: str
    text: str
    mode: str
    voice: str | None
    image_path: Path
    status: TaskStatus = TaskStatus.QUEUED
    progress: float = 0.0
    stage: str = "排队中"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    result_path: Path | None = None
    result_bytes: int | None = None
    error: str | None = None
    cancel_requested: bool = False

    # 这两个属性由 worker 线程写入、事件循环线程读取。
    # CPython 下对 float/str 的单个属性赋值是原子的，足够这里的用途。
    def set_progress(self, fraction: float, stage: str) -> None:
        self.progress = max(0.0, min(1.0, float(fraction)))
        self.stage = stage

    def elapsed(self) -> float | None:
        if self.started_at is None:
            return None
        return round((self.finished_at or time.time()) - self.started_at, 1)

    def signed_result_url(self, public_url: str) -> str:
        """产物下载地址，带短期签名。

        有效期取 `URL_TTL` 与**产物剩余寿命**的较小值。产物到期会被 `purge_expired`
        删掉，若签名声明的有效期超过它，就会出现「链接名义上还有效、点下去却是 404」
        的假承诺——而且调用方无法区分「签名过期」与「文件已删」。
        """
        name = self.result_path.name
        exp = int(time.time()) + config.URL_TTL_SECONDS
        if self.finished_at is not None:
            exp = min(exp, int(self.finished_at) + config.TASK_TTL_SECONDS)
        return f"{public_url}/api/v1/files/{name}?e={exp}&s={sign_result(name, exp)}"

    def to_info(self, public_url: str) -> TaskInfo:
        result_url = None
        if self.result_path is not None and self.status is TaskStatus.SUCCEEDED:
            result_url = self.signed_result_url(public_url)
        return TaskInfo(
            task_id=self.task_id,
            status=self.status,
            mode=self.mode,
            text=self.text,
            progress=self.progress,
            stage=self.stage,
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            elapsed_seconds=self.elapsed(),
            result_url=result_url,
            result_bytes=self.result_bytes,
            error=self.error,
        )


class TaskQueue:
    def __init__(self, pipeline: BasePipeline, public_url: str = "") -> None:
        self._pipeline = pipeline
        self._public_url = public_url.rstrip("/")
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=config.MAX_QUEUE_SIZE)
        self._records: dict[str, TaskRecord] = {}
        self._workers: list[asyncio.Task] = []
        self._running = False

    # ------------------------------------------------------------ 生命周期
    async def start(self) -> None:
        self._running = True
        for idx in range(config.MAX_WORKERS):
            self._workers.append(asyncio.create_task(self._worker(idx), name=f"linly-worker-{idx}"))
        logger.info("任务队列已启动，worker 数=%d，队列上限=%d", config.MAX_WORKERS, config.MAX_QUEUE_SIZE)

    async def stop(self) -> None:
        self._running = False
        for task in self._workers:
            task.cancel()
        for task in self._workers:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._workers.clear()
        logger.info("任务队列已停止")

    # ------------------------------------------------------------ 对外接口
    @property
    def depth(self) -> int:
        return self._queue.qsize()

    @property
    def public_url(self) -> str:
        return self._public_url

    def create(
        self,
        *,
        text: str,
        mode: str,
        voice: str | None,
        image_path: Path,
        task_id: str | None = None,
    ) -> TaskRecord:
        """建立任务记录并入队。队列满时抛 asyncio.QueueFull。

        task_id 允许外部传入：提交接口需要先拿到 id 才能把图片落盘，
        这样图片校验失败时不会在队列里留下垃圾任务。
        """
        task_id = task_id or uuid.uuid4().hex[:16]
        record = TaskRecord(
            task_id=task_id, text=text, mode=mode, voice=voice, image_path=image_path
        )
        self._records[task_id] = record
        self._queue.put_nowait(task_id)
        logger.info("任务入队 %s (mode=%s, 队列深度=%d)", task_id, mode, self._queue.qsize())
        return record

    def get(self, task_id: str) -> TaskRecord | None:
        return self._records.get(task_id)

    def list(self, limit: int = 50) -> list[TaskRecord]:
        records = sorted(self._records.values(), key=lambda r: r.created_at, reverse=True)
        return records[:limit]

    def find_by_result_name(self, filename: str) -> TaskRecord | None:
        """按产物文件名反查已成功的任务，用于下载接口做归属校验。"""
        for record in self._records.values():
            if (
                record.status is TaskStatus.SUCCEEDED
                and record.result_path is not None
                and record.result_path.name == filename
            ):
                return record
        return None

    def cancel(self, task_id: str) -> bool:
        record = self._records.get(task_id)
        if record is None or record.status in (
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELED,
        ):
            return False
        record.cancel_requested = True
        if record.status is TaskStatus.QUEUED:
            # 还没轮到它，直接标记完成，worker 取出时会跳过
            record.status = TaskStatus.CANCELED
            record.stage = "已取消"
            record.finished_at = time.time()
        return True

    def purge_expired(self, ttl_seconds: int | None = None) -> int:
        """清理超过 TTL 的终态任务及其产物。"""
        ttl = ttl_seconds or config.TASK_TTL_SECONDS
        now = time.time()
        dead = [
            tid
            for tid, r in self._records.items()
            if r.finished_at is not None
            and now - r.finished_at > ttl
            and r.status in (TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELED)
        ]
        for tid in dead:
            record = self._records.pop(tid)
            storage.delete_quietly(record.result_path)
            storage.delete_quietly(record.image_path)
        if dead:
            logger.info("清理过期任务 %d 个", len(dead))
        return len(dead)

    # ------------------------------------------------------------ worker
    async def _worker(self, idx: int) -> None:
        logger.info("worker-%d 就绪", idx)
        while self._running:
            try:
                task_id = await self._queue.get()
            except asyncio.CancelledError:
                break

            record = self._records.get(task_id)
            if record is None:
                self._queue.task_done()
                continue
            if record.cancel_requested or record.status is TaskStatus.CANCELED:
                self._queue.task_done()
                continue

            record.status = TaskStatus.RUNNING
            record.started_at = time.time()
            record.stage = "启动中"
            logger.info("任务开始 %s", task_id)

            try:
                # 模型推理是阻塞调用，放进线程池，避免卡死事件循环
                result = await asyncio.to_thread(self._run_sync, record)
                if record.cancel_requested:
                    record.status = TaskStatus.CANCELED
                    record.stage = "已取消"
                    storage.delete_quietly(result)
                else:
                    record.status = TaskStatus.SUCCEEDED
                    record.result_path = result
                    record.result_bytes = result.stat().st_size if result.is_file() else None
                    record.progress = 1.0
                    record.stage = "完成"
                    logger.info(
                        "任务成功 %s，产物 %s（%s 字节，耗时 %ss）",
                        task_id, result.name, record.result_bytes, record.elapsed(),
                    )
            except Exception as exc:  # noqa: BLE001
                record.status = TaskStatus.FAILED
                record.error = f"{type(exc).__name__}: {exc}"
                record.stage = "失败"
                logger.exception("任务失败 %s", task_id)
            finally:
                record.finished_at = time.time()
                self._queue.task_done()

    def _run_sync(self, record: TaskRecord) -> Path:
        """在 worker 线程里执行：按 task_id 规范化产物名，保证可预测。"""
        out = self._pipeline.generate(
            image_path=record.image_path,
            text=record.text,
            mode=record.mode,
            voice=record.voice,
            progress_cb=record.set_progress,
            should_cancel=lambda: record.cancel_requested,
        )
        final = storage.result_path(record.task_id, out.suffix or ".mp4")
        if out.resolve() != final.resolve():
            out.replace(final)
        return final
