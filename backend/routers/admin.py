"""管理路由（开发文档 6.2：文档入库 / 索引重建 / 切片查询）。

本模块只做 HTTP 翻译：取参 -> 调 service -> 返回。
接口契约见 接口文档.md §4.7 - §4.10。

service 契约（backend/services/kb_service.py 需实现，全部 async）：

    create_document(file, *, doc_title, version, category, uploader, trace_id)
        -> DocumentRef(doc_id, job_id, status)          # 立即返回 202，入库异步执行

    rebuild_index(*, scope, force, trace_id) -> str     # job_id

    get_job(job_id) -> JobStatusResponse | None         # None 表示任务不存在

    list_chunks(*, doc_id, q, page, limit, offset, trace_id)
        -> tuple[list[ChunkItem], int]
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status

from ..schemas import (
    ChunkListResponse,
    DocumentUploadResponse,
    IndexRebuildRequest,
    JobStatusResponse,
)
from . import ApiError, get_kb_service, get_trace_id

router = APIRouter(tags=["管理"])


@router.post(
    "/admin/documents",
    response_model=DocumentUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="上传文档并触发入库",
)
async def upload_document(
    file: UploadFile = File(..., description="PDF / MD / TXT / DOCX / PPTX / XLSX"),
    doc_title: str = Form(..., description="文档标题"),
    version: str = Form(..., description="文档版本号，元数据完整性要求必填"),
    category: str = Form(..., description="设备维护 / 工艺 / 标准"),
    uploader: str | None = Form(default=None),
    service: Any = Depends(get_kb_service),
    trace_id: str = Depends(get_trace_id),
) -> DocumentUploadResponse:
    """上传文档并触发入库流水线（FR-08）：解析 -> 抽图 -> 清洗 -> 切片 -> 向量化 -> 入库。

    **长耗时任务，异步执行**：立即返回 job_id，用 GET /api/admin/jobs/{job_id} 查进度。
    version 必填，因为切片元数据完整率 100% 要求 page + version 齐备。
    """

    result = await service.create_document(
        file,
        doc_title=doc_title,
        version=version,
        category=category,
        uploader=uploader,
        trace_id=trace_id,
    )
    return DocumentUploadResponse(
        doc_id=result.doc_id,
        job_id=result.job_id,
        status=result.status,
        trace_id=trace_id,
    )


@router.post(
    "/admin/index/rebuild",
    response_model=dict,
    status_code=status.HTTP_202_ACCEPTED,
    summary="一键重建索引",
)
async def rebuild_index(
    payload: IndexRebuildRequest | None = None,
    service: Any = Depends(get_kb_service),
    trace_id: str = Depends(get_trace_id),
) -> dict[str, Any]:
    """一键重建索引（FR-08，验收标准 <= 5 分钟）。

    注：开发文档 §5.4 的接口表遗漏了本接口，但与 §6.2 规定的 admin.py 职责一致，
    已在 接口文档.md §4.8 补充。请求体可省略，等价于 scope=all, force=false。
    """

    options = payload or IndexRebuildRequest()
    job_id = await service.rebuild_index(scope=options.scope, force=options.force, trace_id=trace_id)
    return {"job_id": job_id, "status": "running", "trace_id": trace_id}


@router.get("/admin/jobs/{job_id}", response_model=JobStatusResponse, summary="任务进度")
async def get_job(
    job_id: str,
    service: Any = Depends(get_kb_service),
    trace_id: str = Depends(get_trace_id),
) -> JobStatusResponse:
    """查询异步任务（文档入库 / 索引重建）进度。"""

    job = await service.get_job(job_id)
    if job is None:
        raise ApiError(404, "JOB_NOT_FOUND", f"任务不存在：{job_id}")
    return JobStatusResponse.model_validate(job)


@router.get("/admin/chunks", response_model=ChunkListResponse, summary="切片查询")
async def list_chunks(
    doc_id: int | None = Query(default=None, description="按文档过滤"),
    q: str | None = Query(default=None, description="按内容模糊搜索"),
    page: int | None = Query(default=None, description="按页码过滤"),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    service: Any = Depends(get_kb_service),
    trace_id: str = Depends(get_trace_id),
) -> ChunkListResponse:
    """切片查询，**调分块参数时用**（开发文档 §6.2）。"""

    items, total = await service.list_chunks(
        doc_id=doc_id, q=q, page=page, limit=limit, offset=offset, trace_id=trace_id
    )
    return ChunkListResponse(items=items, total=total, limit=limit, offset=offset, trace_id=trace_id)
