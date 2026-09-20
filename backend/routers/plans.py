"""维护计划路由（开发文档 6.2：维护计划生成）。

本模块只做 HTTP 翻译：取参 -> 调 service -> 返回，**不写业务逻辑**。
路径前缀 `/plans` 由注册方（main.py / routers 聚合）带，本模块**不加 prefix**。

接口清单
--------
    POST /plans/generate           生成维护计划（可预览不落库）
    GET  /plans                    维护计划列表（过滤 + 分页，最紧急在前）
    POST /plans/{plan_id}/complete 完成计划项（滚动到下一周期）
    POST /plans/{plan_id}/skip     跳过计划项（记原因）

service 契约（backend/services/plan_service.py，全部 async）：
    generate_plans(payload: PlanGenerateRequest, *, trace_id) -> dict
        -> PlanGenerateResponse 可直接校验的字典；`items` 每一项都带 evidence 与
           chunk_id（零幻觉），知识库给不出依据的项进 `uncovered`。
    list_plans(*, device_model, status, limit, offset, trace_id) -> dict
        -> PlanListResponse；按 remaining 升序（最紧急在前）。
    complete_plan(plan_id, payload: PlanUpdateRequest, *, trace_id) -> dict | None
        -> PlanItem；None 表示 id 不存在（404 PLAN_NOT_FOUND）。
    skip_plan(plan_id, payload: PlanUpdateRequest, *, trace_id) -> dict | None
        -> PlanItem；None 同上。

业务口径见 plan_service 模块 docstring：周期数值只来自知识库原文，
「每班次 / 每次开腔」这类只有频次没有数值的条款不折算进计划。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Path, Query

from ..schemas import (
    PlanGenerateRequest,
    PlanGenerateResponse,
    PlanItem,
    PlanListResponse,
    PlanUpdateRequest,
)
from . import ApiError, get_plan_service, get_trace_id

router = APIRouter(tags=["维护计划"])


@router.post("/plans/generate", response_model=PlanGenerateResponse, summary="生成维护计划")
async def generate_plans(
    payload: PlanGenerateRequest,
    service: Any = Depends(get_plan_service),
    trace_id: str = Depends(get_trace_id),
) -> PlanGenerateResponse:
    """按设备型号生成维护计划（FR 维护计划生成）。

    周期数值**只从知识库原文抽取**（`每 4000 运行小时`、`每 25 片`、`每 3 个月`…），
    每条计划项都带 `evidence`（文档 / 版本 / 页码 / 章节）与 `chunk_id`；
    知识库里查不到周期的维护项不会出现在 `items` 里，而是列进 `uncovered`（不编造周期）。

    `persist=false` 只预览不写库；`persist=true` 追加写入 `maintenance_plan`
    （**不覆盖旧计划**，历史留档）。
    """

    result = await service.generate_plans(payload, trace_id=trace_id)
    return PlanGenerateResponse.model_validate(result)


@router.get("/plans", response_model=PlanListResponse, summary="维护计划列表")
async def list_plans(
    device_model: str | None = Query(default=None, description="按设备型号过滤"),
    status: str | None = Query(
        default=None, description="按状态过滤：planned / due / done / skipped"
    ),
    limit: int = Query(default=20, ge=1, le=200, description="每页条数"),
    offset: int = Query(default=0, ge=0, description="偏移量"),
    service: Any = Depends(get_plan_service),
    trace_id: str = Depends(get_trace_id),
) -> PlanListResponse:
    """维护计划列表：按 `remaining` 升序（最紧急 / 已超期的排在最前）。

    `total` 是**过滤后**的总数，配合 `limit` / `offset` 分页。
    """

    result = await service.list_plans(
        device_model=device_model,
        status=status,
        limit=limit,
        offset=offset,
        trace_id=trace_id,
    )
    return PlanListResponse.model_validate(result)


@router.post(
    "/plans/{plan_id}/complete",
    response_model=PlanItem,
    summary="完成维护计划项",
)
async def complete_plan(
    plan_id: int = Path(..., ge=1, description="计划项 id"),
    payload: PlanUpdateRequest = Body(default_factory=PlanUpdateRequest),
    service: Any = Depends(get_plan_service),
    trace_id: str = Depends(get_trace_id),
) -> PlanItem:
    """标记计划项已完成，并**滚动到下一个周期**（`remaining` 回到 `cycle_value`）。

    `completed_value` 给了就作为新计量基线，缺省按「旧基线 + 一个周期」推进；
    `due_at` 按口径重算（运行小时按 24 h 连续运行折算，生产片数没有速率则留空）。
    """

    result = await service.complete_plan(plan_id, payload, trace_id=trace_id)
    if result is None:
        raise ApiError(404, "PLAN_NOT_FOUND", f"维护计划项不存在：{plan_id}")
    return PlanItem.model_validate(result)


@router.post("/plans/{plan_id}/skip", response_model=PlanItem, summary="跳过维护计划项")
async def skip_plan(
    plan_id: int = Path(..., ge=1, description="计划项 id"),
    payload: PlanUpdateRequest = Body(default_factory=PlanUpdateRequest),
    service: Any = Depends(get_plan_service),
    trace_id: str = Depends(get_trace_id),
) -> PlanItem:
    """跳过计划项：`status="skipped"`，`note` 追加原因（周期与到期时间不变）。"""

    result = await service.skip_plan(plan_id, payload, trace_id=trace_id)
    if result is None:
        raise ApiError(404, "PLAN_NOT_FOUND", f"维护计划项不存在：{plan_id}")
    return PlanItem.model_validate(result)


__all__ = ["router"]
