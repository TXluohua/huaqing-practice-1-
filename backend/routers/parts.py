"""备件商城与采购路由（开发文档 6.2：商城目录 + 采购申请单 + 结算台账）。

本模块只做 HTTP 翻译：取参 -> 调 service -> 返回，**不写业务逻辑**。
挂载方式（由 main.py / routers 聚合决定，本模块**不加 prefix**）：

    from backend.routers import api_router
    # api_router 统一加 settings.api_prefix（/api），故完整路径为 /api/parts/...

接口清单
--------
    GET  /parts/catalog                     备件商城目录（过滤 + 分页）
    GET  /parts/settlements                 结算台账
    GET  /parts/orders                      采购申请单列表
    POST /parts/orders                      新建采购申请单（draft）
    GET  /parts/orders/{order_id}           单个采购申请单
    POST /parts/orders/{order_id}/submit    提交申请       draft -> submitted
    POST /parts/orders/{order_id}/approve   审批通过（**人工确认点**）submitted -> approved
    POST /parts/orders/{order_id}/reject    驳回 / 取消（reason 前缀 cancel:/取消： 表示取消）
    POST /parts/orders/{order_id}/receive   收货登记       approved -> received
    POST /parts/orders/{order_id}/settle    结算记账（approved / received，幂等）
    GET  /parts/{code}                      单条备件（**必须最后注册**，否则会吃掉上面几条固定路径）

service 契约（backend/services/parts_service.py，全部 async）：
    list_catalog(*, device_model, keyword, limit, offset, trace_id) -> dict
    get_part(code, *, trace_id) -> dict | None                    # None -> 404 PART_NOT_FOUND
    create_order(payload, *, trace_id) -> dict
    list_orders(*, status, applicant, limit, offset, trace_id) -> dict
    get_order(order_id, *, trace_id) -> dict | None               # None -> 404 ORDER_NOT_FOUND
    submit_order / approve_order / reject_order / receive_order(order_id, payload, *, trace_id)
        -> dict | None                                            # None -> 404 ORDER_NOT_FOUND
    settle_order(order_id, payload, *, trace_id) -> dict          # 订单不存在时 service 抛 404
    list_settlements(*, order_no, limit, offset, trace_id) -> dict

边界（开发文档 §1.3 / FR-10）：approve 即人工确认；系统**不对供应商发起任何
真实下单**，settle 只记账不做财务过账；无依据的替代件由 service 层拒单。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Path, Query

from ..schemas import (
    OrderActionRequest,
    OrderCreateRequest,
    OrderListResponse,
    PartCatalogResponse,
    PartItem,
    PartOrderResponse,
    SettlementListResponse,
    SettlementRequest,
    SettlementResponse,
)
from . import ApiError, get_parts_service, get_trace_id

router = APIRouter(tags=["备件商城与采购"])


# --------------------------------------------------------------------------- #
# 商城目录
# --------------------------------------------------------------------------- #


@router.get("/parts/catalog", response_model=PartCatalogResponse, summary="备件商城目录")
async def list_catalog(
    device_model: str | None = Query(
        default=None, description="按设备型号过滤（台账中的「通用」件始终包含）"
    ),
    keyword: str | None = Query(
        default=None, description="关键词：部件名 / 备件编码 / 规格 模糊匹配"
    ),
    limit: int = Query(default=20, ge=1, le=200, description="每页条数"),
    offset: int = Query(default=0, ge=0, description="偏移量"),
    service: Any = Depends(get_parts_service),
    trace_id: str = Depends(get_trace_id),
) -> PartCatalogResponse:
    """备件商城目录（FR-10）：库存 / 库位 / 到货时间 / 替代件（含兼容性依据）。

    数据来自备件台账（`parts_query`），`total` 是**过滤后**的总数。
    台账里没有价格字段（price_cny 为 0），价格接真实 ERP 后即可返回。
    """

    result = await service.list_catalog(
        device_model=device_model,
        keyword=keyword,
        limit=limit,
        offset=offset,
        trace_id=trace_id,
    )
    return PartCatalogResponse.model_validate(result)


@router.get("/parts/settlements", response_model=SettlementListResponse, summary="采购结算台账")
async def list_settlements(
    order_no: str | None = Query(default=None, description="按订单号模糊过滤"),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    service: Any = Depends(get_parts_service),
    trace_id: str = Depends(get_trace_id),
) -> SettlementListResponse:
    """结算台账（只记账，不做真实财务过账）。

    `total_amount` 是**过滤后全部记录**的金额合计，便于对账。
    """

    result = await service.list_settlements(
        order_no=order_no, limit=limit, offset=offset, trace_id=trace_id
    )
    return SettlementListResponse.model_validate(result)


# --------------------------------------------------------------------------- #
# 采购申请单
# --------------------------------------------------------------------------- #


@router.get("/parts/orders", response_model=OrderListResponse, summary="采购申请单列表")
async def list_orders(
    status: str | None = Query(
        default=None,
        description="按状态过滤：draft / submitted / approved / rejected / received / cancelled",
    ),
    applicant: str | None = Query(default=None, description="按申请人过滤"),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    service: Any = Depends(get_parts_service),
    trace_id: str = Depends(get_trace_id),
) -> OrderListResponse:
    """采购申请单列表（按创建时间倒序）。"""

    result = await service.list_orders(
        status=status,
        applicant=applicant,
        limit=limit,
        offset=offset,
        trace_id=trace_id,
    )
    return OrderListResponse.model_validate(result)


@router.post("/parts/orders", response_model=PartOrderResponse, summary="新建采购申请单")
async def create_order(
    payload: OrderCreateRequest = Body(..., description="采购明细（替代件必须带台账依据）"),
    service: Any = Depends(get_parts_service),
    trace_id: str = Depends(get_trace_id),
) -> PartOrderResponse:
    """新建采购申请单，状态 `draft`。

    逐条校验备件编码；`is_substitute=true` 的明细必须能在台账里找到对应替代条目
    且带兼容性依据，否则 400 `SUBSTITUTE_BASIS_REQUIRED`（零幻觉：无依据不下单）。
    **系统不对供应商发起任何真实下单**，本接口只生成内部申请单。
    """

    result = await service.create_order(payload, trace_id=trace_id)
    return PartOrderResponse.model_validate(result)


@router.get(
    "/parts/orders/{order_id}",
    response_model=PartOrderResponse,
    summary="单个采购申请单",
)
async def get_order(
    order_id: int = Path(..., ge=1, description="订单 id"),
    service: Any = Depends(get_parts_service),
    trace_id: str = Depends(get_trace_id),
) -> PartOrderResponse:
    """单个采购申请单详情（含 `allowed_actions`，前端据此渲染按钮）。"""

    result = await service.get_order(order_id, trace_id=trace_id)
    if result is None:
        raise ApiError(404, "ORDER_NOT_FOUND", f"采购申请单不存在：{order_id}")
    return PartOrderResponse.model_validate(result)


@router.post(
    "/parts/orders/{order_id}/submit",
    response_model=PartOrderResponse,
    summary="提交采购申请",
)
async def submit_order(
    order_id: int = Path(..., ge=1),
    payload: OrderActionRequest = Body(default_factory=OrderActionRequest),
    service: Any = Depends(get_parts_service),
    trace_id: str = Depends(get_trace_id),
) -> PartOrderResponse:
    """提交申请：`draft -> submitted`（非法迁移 409 INVALID_STATE）。"""

    result = await service.submit_order(order_id, payload, trace_id=trace_id)
    if result is None:
        raise ApiError(404, "ORDER_NOT_FOUND", f"采购申请单不存在：{order_id}")
    return PartOrderResponse.model_validate(result)


@router.post(
    "/parts/orders/{order_id}/approve",
    response_model=PartOrderResponse,
    summary="审批通过（人工确认）",
)
async def approve_order(
    order_id: int = Path(..., ge=1),
    payload: OrderActionRequest = Body(default_factory=OrderActionRequest),
    service: Any = Depends(get_parts_service),
    trace_id: str = Depends(get_trace_id),
) -> PartOrderResponse:
    """审批通过：`submitted -> approved`。

    **这就是人工确认点**（开发文档 §1.3）：系统绝不对供应商发起真实下单，
    审批只代表内部申请被批准，采购动作仍由人工走原有流程执行。
    """

    result = await service.approve_order(order_id, payload, trace_id=trace_id)
    if result is None:
        raise ApiError(404, "ORDER_NOT_FOUND", f"采购申请单不存在：{order_id}")
    return PartOrderResponse.model_validate(result)


@router.post(
    "/parts/orders/{order_id}/reject",
    response_model=PartOrderResponse,
    summary="驳回 / 取消采购申请",
)
async def reject_order(
    order_id: int = Path(..., ge=1),
    payload: OrderActionRequest = Body(..., description="reason 必填；取消请用 cancel:/取消： 前缀"),
    service: Any = Depends(get_parts_service),
    trace_id: str = Depends(get_trace_id),
) -> PartOrderResponse:
    """驳回 / 取消（**同一个端点**，用 `reason` 区分意图，不新增路由）。

    * `reason` 形如 `cancel: 原因` / `取消：原因` -> 取消：`draft`/`submitted -> cancelled`
    * 其他 `reason` -> 驳回：`submitted`/`approved -> rejected`

    两种都必须带 `reason`，否则 400 `REASON_REQUIRED`。
    """

    result = await service.reject_order(order_id, payload, trace_id=trace_id)
    if result is None:
        raise ApiError(404, "ORDER_NOT_FOUND", f"采购申请单不存在：{order_id}")
    return PartOrderResponse.model_validate(result)


@router.post(
    "/parts/orders/{order_id}/receive",
    response_model=PartOrderResponse,
    summary="收货登记",
)
async def receive_order(
    order_id: int = Path(..., ge=1),
    payload: OrderActionRequest = Body(default_factory=OrderActionRequest),
    service: Any = Depends(get_parts_service),
    trace_id: str = Depends(get_trace_id),
) -> PartOrderResponse:
    """收货登记：`approved -> received`（只登记到货事实，不做库存扣减）。"""

    result = await service.receive_order(order_id, payload, trace_id=trace_id)
    if result is None:
        raise ApiError(404, "ORDER_NOT_FOUND", f"采购申请单不存在：{order_id}")
    return PartOrderResponse.model_validate(result)


@router.post(
    "/parts/orders/{order_id}/settle",
    response_model=SettlementResponse,
    summary="订单结算",
)
async def settle_order(
    order_id: int = Path(..., ge=1),
    payload: SettlementRequest = Body(default_factory=SettlementRequest),
    service: Any = Depends(get_parts_service),
    trace_id: str = Depends(get_trace_id),
) -> SettlementResponse:
    """结算记账（`approved` / `received` 才能结算）。

    `amount` 缺省用订单金额；重复结算 409 `ALREADY_SETTLED`（幂等保护）。
    只写 `part_settlement` 台账，不做真实财务过账。
    """

    result = await service.settle_order(order_id, payload, trace_id=trace_id)
    return SettlementResponse.model_validate(result)


# --------------------------------------------------------------------------- #
# 单条备件 —— **必须放在所有 /parts/xxx 固定路径之后**
# 否则 /parts/catalog、/parts/orders、/parts/settlements 会被 {code} 吃掉
# --------------------------------------------------------------------------- #


@router.get("/parts/{code}", response_model=PartItem, summary="备件详情")
async def get_part(
    code: str = Path(..., min_length=1, description="备件编码（也支持台账里登记的替代件编码）"),
    service: Any = Depends(get_parts_service),
    trace_id: str = Depends(get_trace_id),
) -> PartItem:
    """单条备件（FR-10）：库存 / 库位 / 到货时间 / 替代件及其兼容性依据。"""

    result = await service.get_part(code, trace_id=trace_id)
    if result is None:
        raise ApiError(404, "PART_NOT_FOUND", f"备件不存在：{code}")
    return PartItem.model_validate(result)


__all__ = ["router"]
