"""知识库服务（开发文档 6.2 的 kb_service.py：文档与图片入库、索引重建）。

冻结契约（协作规范 §3.3，签名不要改）
--------------------------------------
    save_image_upload(file, *, session_id, trace_id)  -> 可被 ImageUploadResponse.model_validate
    create_document(file, *, doc_title, version, category, uploader, trace_id)
        -> DocumentRef(doc_id, job_id, status)，立即返回 202
    rebuild_index(*, scope, force, trace_id) -> str（job_id）
    get_job(job_id) -> JobStatusResponse 可校验的字典 | None
    list_chunks(*, doc_id, q, page, limit, offset, trace_id) -> tuple[list[ChunkItem], int]

长任务（入库 / 重建索引）走进程内任务表 + asyncio 后台任务：
进程重启后任务记录会丢，属于本期约定（运维重跑即可），
要持久化时把 _JOBS 换成一张表即可，路由与契约都不用改。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable

from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from .. import db
from ..rag.ingest import ingest_path, iter_raw_documents
from ..rag.store import get_store
from ..schemas import ChunkItem, ServiceError
from ..setting import get_settings
from ..utils.image import save_image_upload as _save_image_upload
from ..utils.text import mask_secrets

logger = logging.getLogger(__name__)

#: 本期支持解析的文档类型。docx / pptx / xlsx 需接入 MarkItDown（开发文档 5.2.1），
#: 在那之前明确拒绝，而不是入库后产生抓不到页码的切片。
_SUPPORTED_EXT: frozenset[str] = frozenset({".pdf", ".md", ".markdown", ".txt"})
_PENDING_MARKITDOWN: frozenset[str] = frozenset({".docx", ".pptx", ".xlsx"})


@dataclass
class DocumentRef:
    doc_id: int
    job_id: str
    status: str


@dataclass
class Job:
    """进程内后台任务。"""

    job_id: str
    status: str = "pending"  # pending / running / succeeded / failed
    progress: float = 0.0
    message: str = ""
    result: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.time)
    task: asyncio.Task[Any] | None = field(default=None, repr=False)

    def as_dict(self, trace_id: str) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "progress": round(float(self.progress), 3),
            "message": self.message,
            "result": self.result,
            "trace_id": trace_id,
        }


_JOBS: dict[str, Job] = {}


def _require_db() -> None:
    if not db.is_available():
        raise ServiceError(503, "DATABASE_UNAVAILABLE", "数据库不可用，请检查 setting.database_url")


def _new_job() -> Job:
    job = Job(job_id=f"job_{uuid.uuid4().hex[:10]}")
    _JOBS[job.job_id] = job
    return job


def _on_task_done(job: Job, task: asyncio.Task[Any]) -> None:
    """后台任务收尾：只有异常逃逸时才改写状态（正常路径由协程自己写）。"""

    if task.cancelled():
        job.status, job.message = "failed", "任务被取消"
        return
    exc = task.exception()
    if exc is not None:
        job.status = "failed"
        job.message = mask_secrets(str(exc))[:300]
        logger.warning("任务 %s 失败：%s", job.job_id, exc)


def _spawn(job: Job, coro: Awaitable[None]) -> None:
    """把协程挂到当前事件循环（保留引用，避免被垃圾回收）。"""

    task = asyncio.create_task(coro)  # type: ignore[arg-type]
    job.task = task
    task.add_done_callback(lambda finished: _on_task_done(job, finished))


# --------------------------------------------------------------------------- #
# 图片上传
# --------------------------------------------------------------------------- #


async def save_image_upload(file: Any, *, session_id: str | None, trace_id: str) -> dict[str, Any]:
    """校验并落盘图片，返回 ImageUploadResponse 可直接校验的字典。"""

    stored = await _save_image_upload(file, session_id=session_id)
    return {
        "image_id": stored.image_id,
        "filename": stored.filename,
        "mime": stored.mime,
        "size_bytes": stored.size_bytes,
        "width": stored.width,
        "height": stored.height,
        "url": stored.url,
        "trace_id": trace_id,
    }


# --------------------------------------------------------------------------- #
# 文档入库
# --------------------------------------------------------------------------- #


async def _save_upload_to(file: Any, dest: Path) -> int:
    """分片落盘上传文件，返回字节数。"""

    dest.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    with dest.open("wb") as handle:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            handle.write(chunk)
    if size == 0:
        dest.unlink(missing_ok=True)
        raise ServiceError(400, "EMPTY_FILE", "上传内容为空")
    return size


async def _update_document(
    doc_id: int,
    *,
    status: str,
    n_chunks: int = 0,
    error: str = "",
) -> None:
    """回写文档状态；失败只记日志（不掩盖真正的入库结果）。"""

    if not db.is_available():
        return
    try:
        async with db.session_scope() as session:
            row = await session.get(db.KbDocument, doc_id)
            if row is None:
                return
            row.status = status
            row.n_chunks = n_chunks
            row.error = error[:1000]
            session.add(row)
    except Exception as exc:  # noqa: BLE001
        logger.warning("回写文档状态失败 doc_id=%s：%s", doc_id, exc)


async def _run_ingest(
    job: Job,
    *,
    doc_id: int,
    path: Path,
    doc_title: str,
    version: str,
    category: str,
) -> None:
    """后台入库：解析 -> 切片 -> 向量化 -> 回写状态（FR-08）。"""

    job.status, job.progress, job.message = "running", 0.15, "解析与切片…"
    try:
        parsed, chunks = await run_in_threadpool(
            ingest_path,
            path,
            doc_title=doc_title,
            version=version,
            category=category,
        )
        job.progress, job.message = 0.55, f"切片 {len(chunks)} 个，向量化…"

        store = await get_store()
        added = await store.add_chunks(chunks)

        job.status, job.progress = "succeeded", 1.0
        job.message = f"入库完成：{added} 个切片"
        job.result = {
            "doc_id": doc_id,
            "chunks": added,
            "pages": getattr(parsed, "n_pages", 0),
            "images": len(getattr(parsed, "images", []) or []),
            "warnings": list(getattr(parsed, "warnings", []) or [])[:5],
        }
        await _update_document(doc_id, status="ready", n_chunks=added)
        logger.info("文档入库完成 doc_id=%s 切片=%d", doc_id, added)
    except Exception as exc:  # noqa: BLE001 - 任务失败要落到 job 与文档状态上
        job.status, job.message = "failed", mask_secrets(str(exc))[:300]
        await _update_document(doc_id, status="failed", error=str(exc))
        logger.warning("文档入库失败 doc_id=%s：%s", doc_id, exc)


async def create_document(
    file: Any,
    *,
    doc_title: str,
    version: str,
    category: str,
    uploader: str | None,
    trace_id: str,
) -> DocumentRef:
    """上传文档并触发入库（FR-08）。立即返回 job_id，进度用 /api/admin/jobs 查。"""

    _require_db()
    settings = get_settings()

    filename = str(getattr(file, "filename", "") or "")
    suffix = Path(filename).suffix.lower()
    if suffix in _PENDING_MARKITDOWN:
        raise ServiceError(
            415,
            "UNSUPPORTED_DOC_TYPE",
            f"{suffix} 需要接入 MarkItDown 才能解析（开发文档 5.2.1），当前仅支持 PDF / MD / TXT",
            {"extension": suffix, "supported": sorted(_SUPPORTED_EXT)},
        )
    if suffix not in _SUPPORTED_EXT:
        raise ServiceError(
            415,
            "UNSUPPORTED_DOC_TYPE",
            "仅支持 PDF / MD / TXT 文档",
            {"extension": suffix, "supported": sorted(_SUPPORTED_EXT)},
        )

    async with db.session_scope() as session:
        existing = (
            await session.execute(
                select(db.KbDocument).where(
                    db.KbDocument.doc_title == doc_title,
                    db.KbDocument.version == version,
                )
            )
        ).scalars().first()
        if existing is not None:
            raise ServiceError(
                409,
                "DOC_VERSION_EXISTS",
                f"该文档同版本已存在：{doc_title} {version}（doc_id={existing.id}）",
                {"doc_id": int(existing.id)},
            )

    # 先落盘再建记录：source_path 要写进表里
    dest = Path(settings.data_dir) / "documents" / f"{uuid.uuid4().hex[:8]}{suffix}"
    size = await _save_upload_to(file, dest)

    async with db.session_scope() as session:
        document = db.KbDocument(
            doc_title=doc_title,
            source_path=str(dest),
            category=category,
            version=version,
            uploader=uploader or "",
            status="processing",
        )
        session.add(document)
        await session.flush()
        doc_id = int(document.id)

    job = _new_job()
    _spawn(
        job,
        _run_ingest(
            job,
            doc_id=doc_id,
            path=dest,
            doc_title=doc_title,
            version=version,
            category=category,
        ),
    )
    logger.info(
        "文档已接收 doc_id=%s job=%s %s（%.1fKB）trace_id=%s",
        doc_id,
        job.job_id,
        filename,
        size / 1024,
        trace_id,
    )
    return DocumentRef(doc_id=doc_id, job_id=job.job_id, status="processing")


# --------------------------------------------------------------------------- #
# 索引重建
# --------------------------------------------------------------------------- #


async def _run_rebuild(job: Job, *, scope: str, force: bool) -> None:
    """后台重建索引：清空（可选）-> 遍历 data/raw -> 全量入库。"""

    job.status, job.progress, job.message = "running", 0.05, "准备向量库…"
    try:
        store = await get_store()
        if scope == "all" or force:
            await store.reset()

        paths = list(iter_raw_documents())
        if not paths:
            job.status, job.progress = "succeeded", 1.0
            job.message = "data/raw 下没有可入库的文档"
            job.result = {"documents": 0, "chunks": 0, "failed": []}
            return

        total_chunks = 0
        failed: list[dict[str, str]] = []
        for index, path in enumerate(paths, start=1):
            job.message = f"入库 {index}/{len(paths)}：{path.name}"
            job.progress = 0.05 + 0.9 * (index - 1) / len(paths)
            try:
                _parsed, chunks = await run_in_threadpool(ingest_path, path)
                total_chunks += await store.add_chunks(chunks)
            except Exception as exc:  # noqa: BLE001 - 单篇失败不放弃整次重建
                logger.warning("重建索引时 %s 入库失败：%s", path.name, exc)
                failed.append({"file": path.name, "error": mask_secrets(str(exc))[:200]})

        job.status, job.progress = "succeeded", 1.0
        job.result = {
            "documents": len(paths),
            "chunks": total_chunks,
            "failed": failed,
            "scope": scope,
        }
        job.message = f"重建完成：{len(paths)} 篇 / {total_chunks} 个切片" + (
            f"（{len(failed)} 篇失败）" if failed else ""
        )
        logger.info("索引重建完成：%d 篇 / %d 切片", len(paths), total_chunks)
    except Exception as exc:  # noqa: BLE001
        job.status, job.message = "failed", mask_secrets(str(exc))[:300]
        logger.warning("索引重建失败：%s", exc)


async def rebuild_index(*, scope: str, force: bool, trace_id: str) -> str:
    """一键重建索引（FR-08，验收 <= 5 分钟）。返回 job_id。"""

    job = _new_job()
    _spawn(job, _run_rebuild(job, scope=scope, force=force))
    logger.info("已触发索引重建 job=%s scope=%s force=%s trace_id=%s", job.job_id, scope, force, trace_id)
    return job.job_id


async def get_job(job_id: str) -> dict[str, Any] | None:
    """查任务进度；返回 None 表示任务不存在。"""

    job = _JOBS.get(job_id)
    if job is None:
        return None
    return job.as_dict(trace_id="")


# --------------------------------------------------------------------------- #
# 切片查询（调分块参数时用）
# --------------------------------------------------------------------------- #


async def list_chunks(
    *,
    doc_id: int | None,
    q: str | None,
    page: int | None,
    limit: int,
    offset: int,
    trace_id: str,
) -> tuple[list[ChunkItem], int]:
    """切片查询（开发文档 6.2：调分块参数时用）。"""

    store = await get_store()
    chunks = list(await store.all_chunks())

    # doc_id 是业务库里的自增 ID，而切片里存的是标题+版本，先翻译一次
    if doc_id is not None:
        if not db.is_available():
            raise ServiceError(503, "DATABASE_UNAVAILABLE", "数据库不可用，无法按 doc_id 过滤")
        async with db.session_scope() as session:
            row = await session.get(db.KbDocument, doc_id)
        if row is None:
            return [], 0
        chunks = [
            item
            for item in chunks
            if item.doc_title == row.doc_title and item.version == row.version
        ]

    if q:
        needle = q.lower()
        chunks = [item for item in chunks if needle in (item.text or "").lower()]
    if page is not None:
        chunks = [item for item in chunks if int(item.page or 0) == page]

    total = len(chunks)
    window = chunks[offset : offset + limit]
    items = [
        ChunkItem(
            chunk_id=item.chunk_id,
            doc=item.doc_title,
            version=item.version,
            section=item.section or item.heading or None,
            page=int(item.page or 0) or None,
            token_len=int(item.n_tokens or 0),
            text=item.text or "",
            image_url=None,  # 图片侧入库尚未接入（P3 痛点，见开发文档 5.2 抽图）
        )
        for item in window
    ]
    return items, total


__all__ = [
    "DocumentRef",
    "Job",
    "create_document",
    "get_job",
    "list_chunks",
    "rebuild_index",
    "save_image_upload",
]
