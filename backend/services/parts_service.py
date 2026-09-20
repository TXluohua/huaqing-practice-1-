"""备件商城与采购服务（开发文档 6.2 的 parts_service.py）。

覆盖两块新业务
--------------
    ① 备件商城目录 + 采购申请单 `part_order`（draft → submitted → approved → received）
    ② 采购结算台账 `part_settlement`（只记账，不做真实财务过账）

冻结契约（`backend/routers/__init__.py` 的 `_PARTS_SERVICE_API`，签名不要改）
----------------------------------------------------------------------------
    list_catalog(*, device_model, keyword, limit, offset, trace_id) -> dict
    get_part(code, *, trace_id) -> dict | None
    create_order(payload, *, trace_id) -> dict
    list_orders(*, status, applicant, limit, offset, trace_id) -> dict
    get_order(order_id, *, trace_id) -> dict | None
    submit_order / approve_order / reject_order / receive_order(order_id, payload, *, trace_id) -> dict | None
    settle_order(order_id, payload, *, trace_id) -> dict
    list_settlements(*, order_no, limit, offset, trace_id) -> dict

返回的 dict 直接喂给 schemas.py 里对应的 pydantic 模型（PartItem /
PartCatalogResponse / PartOrderResponse / SettlementResponse / ...）。

边界（开发文档 §1.3、FR-10）——**零幻觉与「不自动下单」在本模块是硬约束**
------------------------------------------------------------------------
* **系统绝不对供应商发起任何真实下单**：`approve_order` 就是人工确认点，
  `PartOrder` 只承载**内部采购申请**；订单即使走到 approved/received，
  也不产生任何对外询价、下单、付款动作 —— 询价与下单仍由人工走原有流程。
* **替代件必须带兼容性依据**：`parts_query` 已把缺 `basis` 的替代条目降级为
  「需原厂确认」，本服务再挡一道 —— 无依据的替代件一律
  400 `SUBSTITUTE_BASIS_REQUIRED`，宁可不接单也不猜（备件用错会损坏设备）。
* 备件数据的**唯一读取入口**是 `backend/tools/kb_tools.parts_query`：本模块
  不解析 YAML、也不缓存一份自己的台账副本，避免与工具口径漂移。

价格与库存
----------
`backend/config/parts_inventory.yaml` 里有 `price_cny` / `supplier`，但
`parts_query` 的返回契约里**没有**这两个字段。按「不编造价格」+「不自行解析
YAML」两条约束，数据源没给的字段一律取 `0.0` / `""`（金额因此为 0，属预期）；
一旦平台侧把价格补进工具返回，本模块不改一行代码即可取到 —— `_price_of` /
`_supplier_of` 读的就是工具返回里的 `price_cny` / `supplier`。

库存**不扣减**：本模块只生成采购申请，不做出入库（`stock` 只用于展示与提示）。

时间统一 UTC（`db.UTCDateTime` 落库为 naive UTC，输出 `isoformat()` 带 `+00:00`）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import db
from ..db import PartOrder, PartSettlement
from ..schemas import (
    OrderActionRequest,
    OrderCreateRequest,
    OrderItemRequest,
    OrderItemUpdateRequest,
    ServiceError,
    SettlementRequest,
)
from ..setting import get_settings
from ..tools import kb_tools

logger = logging.getLogger(__name__)

#: 商城目录遍历用的备件编码 —— 备件台账 backend/config/parts_inventory.yaml 的 5 条示例。
#: 为什么写死：`parts_query` 是「按编码/名称查一条」的工具，没有 list 接口，而约束是
#: 不许自己解析 YAML。台账增删条目时同步维护这里（或后续给工具补一个 list 能力）。
CATALOG_CODES: tuple[str, ...] = (
    "SP-ETA-0101",
    "SP-ETA-0102",
    "SP-ETA-0210",
    "SP-ETA-0220",
    "SP-ETA-0310",
)

#: 「通用」件：不挑设备型号，按任意型号过滤时都应包含
GENERIC_MODEL = "通用"

#: 采购申请单币种（本期只做 CNY）
CURRENCY = "CNY"

#: 单条明细的最大数量（与 OrderItemRequest.qty 的上限一致）
MAX_ITEM_QTY = 999

#: 订单状态全集
ORDER_STATUSES: tuple[str, ...] = (
    "draft",
    "submitted",
    "approved",
    "rejected",
    "received",
    "cancelled",
)

#: 各状态允许的下一步动作（前端据此渲染按钮；与状态机保持同源）
_ALLOWED_ACTIONS: dict[str, tuple[str, ...]] = {
    "draft": ("submit", "cancel"),
    "submitted": ("approve", "reject", "cancel"),
    "approved": ("receive",),
    "received": ("settle",),
    "rejected": (),
    "cancelled": (),
}

#: 取消意图标记。`OrderActionRequest` 里没有 cancel 字段，取消复用 `reject_order`
#: 且**不新增端点**：reason 必须形如 `cancel: 原因` / `取消：原因`（或恰好是
#: `cancel` / `取消`），否则按普通驳回处理。
_CANCEL_MARKERS: tuple[str, ...] = ("cancel", "取消")

#: 分页上限（防止一次拉全表）
_MAX_LIMIT = 200


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _require_db() -> None:
    """数据库不可用时给明确的 503，而不是让异常漏成 500。"""

    if not db.is_available():
        raise ServiceError(503, "DB_UNAVAILABLE", "数据库不可用，请检查 setting.database_url")


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:  # 兜底：直接构造的 naive datetime 按 UTC 解释
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _page(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(_as_int(limit, 20), _MAX_LIMIT)), max(0, _as_int(offset, 0))


def allowed_actions(status: str) -> list[str]:
    """当前状态允许的下一步动作（未知状态返回空列表）。"""

    return list(_ALLOWED_ACTIONS.get(str(status or ""), ()))


def _append_note(existing: str, line: str) -> str:
    """把一行审计信息追加到 note（表里没有独立的 operator/reason 列）。"""

    line = (line or "").strip()
    if not line:
        return existing or ""
    return f"{existing}\n{line}" if existing else line


def _is_cancel_reason(reason: str) -> bool:
    """判断 reject_order 的 reason 表达的意图是「取消」还是「驳回」。

    取消标记要求带分隔符（`cancel:` / `取消：`），避免把「取消替代件方案」这类
    正常驳回理由误判成取消。
    """

    head = (reason or "").strip().lower()
    for marker in _CANCEL_MARKERS:
        if head == marker:
            return True
        if head.startswith(marker) and head[len(marker) : len(marker) + 1] in (":", "："):
            return True
    return False


def _reason_body(reason: str) -> str:
    """去掉取消标记前缀，保留人类可读的原因正文。"""

    head = (reason or "").strip()
    lowered = head.lower()
    for marker in _CANCEL_MARKERS:
        if lowered == marker:
            return ""
        if lowered.startswith(marker) and head[len(marker) : len(marker) + 1] in (":", "："):
            return head[len(marker) + 1 :].strip()
    return head


# --------------------------------------------------------------------------- #
# 备件台账读取（唯一入口：kb_tools.parts_query）
# --------------------------------------------------------------------------- #


async def _query_part(code: str) -> dict[str, Any]:
    """调备件查询工具取一条台账。工具坏了按「查不到」处理，由调用方明确报错。"""

    try:
        payload = await kb_tools.parts_query(code)
    except Exception as exc:  # noqa: BLE001 - 数据源故障不能变成 500
        logger.warning("parts_query(%r) 调用失败：%s", code, exc)
        return _empty_part(code, note=f"备件数据源不可用：{exc}")
    if not isinstance(payload, dict):
        return _empty_part(code, note="备件数据源返回结构异常")
    return payload


def _empty_part(code: str, *, note: str = "") -> dict[str, Any]:
    return {
        "found": False,
        "part": code,
        "code": code,
        "substitutes": [],
        "forbidden": [],
        "notes": [note] if note else [],
    }


async def _catalog_payloads() -> list[dict[str, Any]]:
    """遍历台账编码取全部备件（每次调用都走工具，工具自己带 lru 缓存）。"""

    payloads: list[dict[str, Any]] = []
    for code in CATALOG_CODES:
        payload = await _query_part(code)
        if payload.get("found"):
            payloads.append(payload)
        else:  # 台账被删条目时跳过，而不是造一条假数据
            logger.warning("备件台账中未找到目录编码：%s", code)
    return payloads


async def _find_substitute(code: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """找「列出该替代件编码」的主件。

    替代件编码（如 SP-ETA-0101F）在 `parts_query` 里查不到（工具只匹配主件），
    所以要在台账的 `substitutes` 里反查它属于哪一条主件 —— 这也是替代件
    **依据（basis）的唯一来源**。
    """

    needle = (code or "").strip().lower()
    if not needle:
        return None
    for parent_code in CATALOG_CODES:
        parent = await _query_part(parent_code)
        if not parent.get("found"):
            continue
        for sub in parent.get("substitutes") or []:
            if not isinstance(sub, dict):
                continue
            if str(sub.get("code") or "").strip().lower() == needle:
                return parent, sub
    return None


def _price_of(entry: dict[str, Any]) -> float:
    """台账价格：数据源没给就是 0.0（**不编造价格**）。"""

    for key in ("price_cny", "unit_price", "price"):
        if entry.get(key) is None:
            continue
        return round(_as_float(entry.get(key)), 2)
    return 0.0


def _supplier_of(entry: dict[str, Any]) -> str:
    return str(entry.get("supplier") or "")


def _stock_status(stock: int) -> str:
    """库存紧张判定：与 parts_query 同口径（阈值取自 setting.parts_low_stock）。"""

    return "low" if _as_int(stock) <= int(get_settings().parts_low_stock) else "ok"


def _forbidden_lines(payload: dict[str, Any]) -> list[str]:
    """把台账的 forbidden（[{part, reason}]）转成 PartItem.forbidden 要求的字符串列表。

    契约注意：`PartItem.forbidden: list[str]`，而工具返回的是对象列表，
    直接塞进去会被 pydantic 拒绝。这里按工具 notes 的口径格式化成一行文字，
    信息不丢、也不新增判断。
    """

    lines: list[str] = []
    for bad in payload.get("forbidden") or []:
        if isinstance(bad, dict):
            lines.append(f"禁止替代：{bad.get('part')} —— {bad.get('reason')}")
        elif bad:
            lines.append(str(bad))
    return lines


def _part_view(payload: dict[str, Any], trace_id: str = "") -> dict[str, Any]:
    """台账条目 -> PartItem 可校验的 dict。"""

    stock = _as_int(payload.get("stock"))
    return {
        "code": str(payload.get("code") or ""),
        "part": str(payload.get("part") or ""),
        "device_model": str(payload.get("device_model") or ""),
        "spec": str(payload.get("spec") or ""),
        "stock": stock,
        "unit": str(payload.get("unit") or "个"),
        "location": str(payload.get("location") or ""),
        "lead_time_days": _as_int(payload.get("lead_time_days")),
        "price_cny": _price_of(payload),
        "supplier": _supplier_of(payload),
        "stock_status": str(payload.get("stock_status") or _stock_status(stock)),
        "substitutes": [dict(s) for s in (payload.get("substitutes") or []) if isinstance(s, dict)],
        "forbidden": _forbidden_lines(payload),
        "trace_id": trace_id,
    }


def _substitute_view(
    parent: dict[str, Any], sub: dict[str, Any], trace_id: str = ""
) -> dict[str, Any]:
    """替代件条目 -> PartItem 可校验的 dict（规格/库位台账未登记，留空不编造）。"""

    stock = _as_int(sub.get("stock"))
    return {
        "code": str(sub.get("code") or ""),
        "part": str(sub.get("part") or ""),
        "device_model": str(parent.get("device_model") or ""),
        "spec": "",
        "stock": stock,
        "unit": str(sub.get("unit") or parent.get("unit") or "个"),
        "location": "",
        "lead_time_days": _as_int(sub.get("lead_time_days")),
        "price_cny": _price_of(sub),
        "supplier": _supplier_of(sub),
        "stock_status": _stock_status(stock),
        "substitutes": [],
        "forbidden": _forbidden_lines(parent),
        "trace_id": trace_id,
    }


# --------------------------------------------------------------------------- #
# ① 商城目录
# --------------------------------------------------------------------------- #


def _model_matches(part_model: str, requested: str) -> bool:
    """型号过滤：精确匹配，或该件是「通用」件（通用件对所有型号都适用）。"""

    if not requested:
        return True
    if requested == part_model:
        return True
    return part_model == GENERIC_MODEL


def _keyword_matches(payload: dict[str, Any], needle: str) -> bool:
    """关键词模糊匹配：部件名 / 编码 / 规格。"""

    haystack = " ".join(
        str(payload.get(key) or "") for key in ("part", "code", "spec", "location")
    ).lower()
    return needle in haystack


async def list_catalog(
    *,
    device_model: str | None,
    keyword: str | None,
    limit: int,
    offset: int,
    trace_id: str,
) -> dict:
    """备件商城目录（GET /parts/catalog）。

    数据来自 `parts_query` 逐条读取；按 `device_model`（含「通用」件）与
    `keyword`（部件名/编码/规格）过滤后分页，`total` 是**过滤后**的总数。
    """

    model = (device_model or "").strip()
    needle = (keyword or "").strip().lower()
    limit, offset = _page(limit, offset)

    filtered = [
        payload
        for payload in await _catalog_payloads()
        if _model_matches(str(payload.get("device_model") or ""), model)
        and (not needle or _keyword_matches(payload, needle))
    ]
    page = filtered[offset : offset + limit]
    return {
        "items": [_part_view(payload, trace_id) for payload in page],
        "total": len(filtered),
        "trace_id": trace_id,
    }


async def get_part(code: str, *, trace_id: str) -> dict | None:
    """单条备件（GET /parts/{code}）：台账里没有 -> None（路由转 404）。

    替代件编码（只登记在主件的 `substitutes` 里）也支持查询，返回该替代件
    自身的库存/交期与主件的禁止替代清单。
    """

    payload = await _query_part(code)
    if payload.get("found"):
        return _part_view(payload, trace_id)
    found = await _find_substitute(code)
    if found is None:
        return None
    parent, sub = found
    return _substitute_view(parent, sub, trace_id)


# --------------------------------------------------------------------------- #
# ② 采购申请单
# --------------------------------------------------------------------------- #


def _order_view(order: PartOrder, trace_id: str = "") -> dict[str, Any]:
    """PartOrder -> PartOrderResponse 可校验的 dict。"""

    return {
        "id": int(order.id or 0),
        "order_no": str(order.order_no or ""),
        "status": str(order.status or "draft"),
        "device_model": str(order.device_model or ""),
        "purpose": str(order.purpose or ""),
        "applicant": str(order.applicant or ""),
        "items": [_order_item_view(item) for item in (order.items or [])],
        "total_amount": round(_as_float(order.total_amount), 2),
        "currency": str(order.currency or CURRENCY),
        "note": str(order.note or ""),
        "created_at": _iso(order.created_at),
        "updated_at": _iso(order.updated_at),
        "allowed_actions": allowed_actions(str(order.status or "")),
        "trace_id": trace_id,
    }


def _order_item_view(raw: Any) -> dict[str, Any]:
    """明细行（JSON 列里存的 dict）-> PartOrderItem 可校验的 dict。"""

    item = raw if isinstance(raw, dict) else {}
    qty = max(1, _as_int(item.get("qty"), 1))
    unit_price = _price_of(item)
    return {
        "code": str(item.get("code") or ""),
        "part": str(item.get("part") or ""),
        "qty": qty,
        "unit": str(item.get("unit") or "个"),
        "unit_price": unit_price,
        "amount": round(_as_float(item.get("amount"), qty * unit_price), 2),
        "is_substitute": bool(item.get("is_substitute")),
        "basis": str(item.get("basis") or ""),
        "stock": _as_int(item.get("stock")),
        "lead_time_days": _as_int(item.get("lead_time_days")),
    }


async def _resolve_order_item(req: Any) -> dict[str, Any]:
    """把一条下单请求解析成明细行，并守住替代件的依据要求。

    规则：
        * 编码在台账里查不到，且也不是任何主件登记的替代件
          -> 400 PART_NOT_FOUND
        * 走替代件下单（`is_substitute=True`，或编码本身就是台账登记的替代件）
          必须能取到非空 `basis`，否则 -> 400 SUBSTITUTE_BASIS_REQUIRED
    """

    code = str(getattr(req, "code", "") or "").strip()
    if not code:
        raise ServiceError(400, "PART_NOT_FOUND", "备件不存在：<空编码>")

    payload = await _query_part(code)
    parent = payload if payload.get("found") else None
    sub_entry: dict[str, Any] | None = None
    if parent is None:
        found = await _find_substitute(code)
        if found is None:
            raise ServiceError(400, "PART_NOT_FOUND", f"备件不存在：{code}")
        parent, sub_entry = found
    else:
        # 主件编码被标成替代件时，依据必须来自它自己登记的 substitutes 条目
        sub_entry = _match_substitute(parent, code)

    # 编码本身就是替代件 -> 该行必然是替代件（不允许把替代件当主件入库）
    is_substitute = bool(getattr(req, "is_substitute", False)) or sub_entry is not None
    basis = ""
    if is_substitute:
        basis = str((sub_entry or {}).get("basis") or "").strip()
        if not basis:
            raise ServiceError(
                400,
                "SUBSTITUTE_BASIS_REQUIRED",
                f"替代件缺少兼容性依据，需原厂确认：{code}",
                {"code": code, "hint": "请改用主件编码，或补充台账里的 basis 后再下单"},
            )

    entry = sub_entry or parent
    qty = max(1, _as_int(getattr(req, "qty", 1), 1))
    unit_price = _price_of(entry)
    return {
        "code": str(entry.get("code") or code),
        "part": str(entry.get("part") or parent.get("part") or ""),
        "qty": qty,
        "unit": str(entry.get("unit") or parent.get("unit") or "个"),
        "unit_price": unit_price,
        "amount": round(qty * unit_price, 2),
        "is_substitute": is_substitute,
        "basis": basis,
        "stock": _as_int(entry.get("stock")),
        "lead_time_days": _as_int(entry.get("lead_time_days")),
    }


def _match_substitute(parent: dict[str, Any], code: str) -> dict[str, Any] | None:
    """在主件自己登记的 substitutes 里找编码等于 code 的条目。"""

    needle = (code or "").strip().lower()
    for sub in parent.get("substitutes") or []:
        if isinstance(sub, dict) and str(sub.get("code") or "").strip().lower() == needle:
            return sub
    return None


async def _next_order_no(session: AsyncSession) -> str:
    """生成订单号 `PO-YYYYMMDD-NNNN`（当天序号，重号则顺延；唯一由 DB 唯一索引兜底）。"""

    prefix = f"PO-{_now():%Y%m%d}"
    used = int(
        (
            await session.execute(
                select(func.count())
                .select_from(PartOrder)
                .where(PartOrder.order_no.like(f"{prefix}-%"))
            )
        ).scalar_one()
        or 0
    )
    seq = used + 1
    while True:
        candidate = f"{prefix}-{seq:04d}"
        exists = (
            await session.execute(select(PartOrder.id).where(PartOrder.order_no == candidate))
        ).scalar_one_or_none()
        if exists is None:
            return candidate
        seq += 1


async def create_order(payload: OrderCreateRequest, *, trace_id: str) -> dict:
    """新建采购申请单（POST /parts/orders），状态 `draft`。

    只落**内部申请单**：每条明细逐条回台账校验编码与替代件依据，
    单价取台账价格、`amount = qty * unit_price`、`total_amount` 汇总。
    **不向供应商发起任何真实下单**（开发文档 §1.3 边界）。

    `items` 允许为空 —— 空草稿单就是「空购物车」（购物车页可以先建车，再用
    `POST …/items` 逐件加入）；但**空车不能提交**（`submit_order` 返回 400 `EMPTY_ORDER`）。
    """

    _require_db()
    # 允许空明细：空草稿单 = 空购物车（前端可以先建车再逐件加入）。
    # 空车不能提交 —— 那道闸门在 submit_order 里（400 EMPTY_ORDER），
    # 而不是在这里拦「建车」，否则购物车页一进来就不可能有车。
    items = [await _resolve_order_item(req) for req in payload.items]
    total_amount = round(sum(_as_float(item["amount"]) for item in items), 2)

    async with db.session_scope() as session:
        order = PartOrder(
            order_no=await _next_order_no(session),
            device_model=str(payload.device_model or "").strip(),
            purpose=str(payload.purpose or "").strip(),
            applicant=str(payload.applicant or "").strip(),
            status="draft",
            items=items,
            total_amount=total_amount,
            currency=CURRENCY,
            note=str(payload.note or ""),
            trace_id=trace_id,
        )
        session.add(order)
        await session.flush()
        view = _order_view(order, trace_id)
    logger.info(
        "采购申请单已创建：%s（%d 条明细，合计 %.2f %s）",
        view["order_no"],
        len(items),
        total_amount,
        CURRENCY,
    )
    return view


async def list_orders(
    *,
    status: str | None,
    applicant: str | None,
    limit: int,
    offset: int,
    trace_id: str,
) -> dict:
    """采购申请单列表（GET /parts/orders），按 id 倒序（新的在前）。"""

    _require_db()
    limit, offset = _page(limit, offset)
    conditions = []
    if status:
        conditions.append(PartOrder.status == status.strip())
    if applicant:
        conditions.append(PartOrder.applicant == applicant.strip())

    async with db.session_scope() as session:
        total = int(
            (
                await session.execute(
                    select(func.count()).select_from(PartOrder).where(*conditions)
                )
            ).scalar_one()
            or 0
        )
        rows = (
            (
                await session.execute(
                    select(PartOrder)
                    .where(*conditions)
                    .order_by(PartOrder.id.desc())
                    .limit(limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
        items = [_order_view(order, trace_id) for order in rows]

    return {"items": items, "total": total, "limit": limit, "offset": offset, "trace_id": trace_id}


async def get_order(order_id: int, *, trace_id: str) -> dict | None:
    """单个采购申请单（GET /parts/orders/{order_id}）；不存在 -> None（路由转 404）。"""

    _require_db()
    async with db.session_scope() as session:
        order = await session.get(PartOrder, _as_int(order_id))
        if order is None:
            return None
        return _order_view(order, trace_id)


# --------------------------------------------------------------------------- #
# 购物车（= `draft` 状态的采购申请单明细维护）
# --------------------------------------------------------------------------- #
#
# 设计口径：**购物车就是草稿单**，不额外建一张 cart 表 ——
#   * 「加入购物车」= 在草稿单里加一条明细（`POST …/items`，同编码累加数量）；
#   * 「改数量」  = `PATCH …/items/{code}`（数量 1~999）；
#   * 「移出购物车」= `DELETE …/items/{code}`（允许清空，清空的草稿单不可提交）；
#   * 「我的购物车」= `GET /parts/orders?status=draft`。
# 只有 `draft` 状态可改：提交之后明细即冻结（审批审的就是这份清单，不能偷偷改）。
# 单价一律重新回台账取（`_resolve_order_item`），**不信任前端传的价格**。


def _recalc_total(items: Sequence[dict[str, Any]]) -> float:
    """按明细重算合计（金额取明细里的 amount，缺失则 qty × unit_price）。"""

    total = 0.0
    for item in items:
        qty = max(1, _as_int(item.get("qty"), 1))
        unit_price = _price_of(item)
        total += _as_float(item.get("amount"), qty * unit_price)
    return round(total, 2)


async def _edit_items(
    order_id: int,
    mutate: Any,
    *,
    action_label: str,
    trace_id: str,
    operator: str = "",
) -> dict | None:
    """草稿单明细编辑的公共实现：加锁校验状态 -> 交给 mutate 改明细 -> 重算合计。

    `mutate(items)` 就地修改明细列表，返回一句审计说明（可为空字符串）。
    订单不存在返回 None（路由转 404）；非 `draft` -> 409 INVALID_STATE。
    """

    _require_db()
    async with db.session_scope() as session:
        order = await session.get(PartOrder, _as_int(order_id))
        if order is None:
            return None
        if str(order.status or "") != "draft":
            raise ServiceError(
                409,
                "INVALID_STATE",
                f"订单 {order.order_no} 当前状态为 {order.status}，不允许{action_label}"
                "（仅草稿状态可修改明细，提交后明细即冻结）",
                {"order_no": order.order_no, "status": order.status, "allowed": ["draft"]},
            )

        items = [dict(item) for item in (order.items or []) if isinstance(item, dict)]
        audit = mutate(items) or ""
        who = str(operator or "").strip()
        if audit and who:
            audit = f"{audit}（操作人：{who}）"
        now = _now()
        order.items = items
        order.total_amount = _recalc_total(items)
        order.updated_at = now
        if audit:
            order.note = _append_note(str(order.note or ""), str(audit))

        await session.flush()
        view = _order_view(order, trace_id)

    logger.info(
        "采购申请单 %s：%s（%d 条明细，合计 %.2f %s）",
        view["order_no"],
        action_label,
        len(view["items"]),
        view["total_amount"],
        CURRENCY,
    )
    return view


async def add_order_item(
    order_id: int, payload: OrderItemRequest, *, trace_id: str
) -> dict | None:
    """加入购物车（POST /parts/orders/{order_id}/items，仅 `draft`）。

    * 明细价格与库存**重新回台账取**（`_resolve_order_item`），不信任前端传值；
    * 同一编码已在购物车里 -> **累加数量**（上限 999），不产生重复行；
    * 替代件同样受依据约束：台账查不到 `basis` -> 400 SUBSTITUTE_BASIS_REQUIRED；
    * 非草稿状态 -> 409 INVALID_STATE。
    """

    resolved = await _resolve_order_item(payload)
    code = str(resolved.get("code") or "")

    def mutate(items: list[dict[str, Any]]) -> str:
        for item in items:
            if str(item.get("code") or "") == code:
                merged = max(1, _as_int(item.get("qty"), 1)) + max(1, _as_int(resolved.get("qty"), 1))
                item["qty"] = min(MAX_ITEM_QTY, merged)
                item["amount"] = round(_as_float(item["qty"]) * _price_of(item), 2)
                return f"购物车加量：{code} × {item['qty']}"
        items.append(dict(resolved))
        return f"购物车新增：{code} × {resolved.get('qty')}"

    return await _edit_items(
        order_id,
        mutate,
        action_label="修改购物车明细",
        trace_id=trace_id,
        operator=str(getattr(payload, "operator", "") or ""),
    )


async def update_order_item(
    order_id: int, code: str, payload: OrderItemUpdateRequest, *, trace_id: str
) -> dict | None:
    """改数量（PATCH /parts/orders/{order_id}/items/{code}，仅 `draft`）。

    数量取 1~999；明细不存在 -> 404 ITEM_NOT_FOUND；非草稿 -> 409 INVALID_STATE。
    """

    wanted = str(code or "").strip()
    qty = max(1, min(MAX_ITEM_QTY, _as_int(getattr(payload, "qty", 1), 1)))

    def mutate(items: list[dict[str, Any]]) -> str:
        for item in items:
            if str(item.get("code") or "").lower() == wanted.lower():
                item["qty"] = qty
                item["amount"] = round(qty * _price_of(item), 2)
                return f"购物车改量：{wanted} × {qty}"
        raise ServiceError(404, "ITEM_NOT_FOUND", f"购物车里没有该备件：{wanted}", {"code": wanted})

    return await _edit_items(
        order_id,
        mutate,
        action_label="修改购物车明细",
        trace_id=trace_id,
        operator=str(getattr(payload, "operator", "") or ""),
    )


async def remove_order_item(order_id: int, code: str, *, trace_id: str) -> dict | None:
    """移出购物车（DELETE /parts/orders/{order_id}/items/{code}，仅 `draft`）。

    允许把草稿单清空（购物车为空是正常状态），但**空草稿单不能提交**（提交时 400 EMPTY_ORDER）。
    明细不存在 -> 404 ITEM_NOT_FOUND；非草稿 -> 409 INVALID_STATE。
    """

    wanted = str(code or "").strip()

    def mutate(items: list[dict[str, Any]]) -> str:
        keep = [item for item in items if str(item.get("code") or "").lower() != wanted.lower()]
        if len(keep) == len(items):
            raise ServiceError(404, "ITEM_NOT_FOUND", f"购物车里没有该备件：{wanted}", {"code": wanted})
        items[:] = keep
        return f"购物车移除：{wanted}"

    return await _edit_items(order_id, mutate, action_label="修改购物车明细", trace_id=trace_id)


async def _advance(
    order_id: int,
    payload: OrderActionRequest,
    *,
    target: str,
    allowed_from: tuple[str, ...],
    stamp_field: str,
    action_label: str,
    audit_line: str = "",
    trace_id: str,
) -> dict | None:
    """状态机公共实现：校验当前状态 -> 迁移 -> 落时间戳与审计信息。

    非法迁移一律 409 INVALID_STATE（订单不存在则返回 None，由路由转 404）。
    """

    _require_db()
    async with db.session_scope() as session:
        order = await session.get(PartOrder, _as_int(order_id))
        if order is None:
            return None
        if str(order.status or "") not in allowed_from:
            raise ServiceError(
                409,
                "INVALID_STATE",
                f"订单 {order.order_no} 当前状态为 {order.status}，不允许{action_label}"
                f"（仅允许 {'/'.join(allowed_from)}）",
                {"order_no": order.order_no, "status": order.status, "allowed": list(allowed_from)},
            )

        now = _now()
        order.status = target
        if stamp_field:
            setattr(order, stamp_field, now)
        order.updated_at = now

        note = str(payload.note or "").strip() if payload is not None else ""
        for line in (note, audit_line):
            order.note = _append_note(str(order.note or ""), line)

        await session.flush()
        view = _order_view(order, trace_id)

    logger.info("采购申请单 %s：%s -> %s", view["order_no"], action_label, target)
    return view


async def submit_order(
    order_id: int, payload: OrderActionRequest, *, trace_id: str
) -> dict | None:
    """提交申请（draft -> submitted）。提交即进入人工审批队列。

    提交前校验**购物车不为空**：空草稿单 -> 400 EMPTY_ORDER（避免提交一张空申请单）。
    """

    operator = str(payload.operator or "").strip()
    _require_db()
    async with db.session_scope() as session:
        order = await session.get(PartOrder, _as_int(order_id))
        if order is not None and str(order.status or "") == "draft" and not (order.items or []):
            raise ServiceError(
                400,
                "EMPTY_ORDER",
                f"申请单 {order.order_no} 没有明细，无法提交（请先加入备件）",
                {"order_no": order.order_no},
            )
    return await _advance(
        order_id,
        payload,
        target="submitted",
        allowed_from=("draft",),
        stamp_field="submitted_at",
        action_label="提交",
        audit_line=f"提交人：{operator}" if operator else "",
        trace_id=trace_id,
    )


async def approve_order(
    order_id: int, payload: OrderActionRequest, *, trace_id: str
) -> dict | None:
    """审批通过（submitted -> approved）——**这就是人工确认点**。

    边界（开发文档 §1.3）：系统**不对供应商发起任何真实下单**。
    本方法只把内部申请单置为「已批准，可执行采购」，后续询价/下单/付款
    仍由人工走原有流程；`approved` 只是本系统内部的审批结论。
    """

    operator = str(payload.operator or "").strip()
    return await _advance(
        order_id,
        payload,
        target="approved",
        allowed_from=("submitted",),
        stamp_field="approved_at",
        action_label="审批通过",
        audit_line=f"审批人：{operator}" if operator else "",
        trace_id=trace_id,
    )


async def reject_order(
    order_id: int, payload: OrderActionRequest, *, trace_id: str
) -> dict | None:
    """驳回 / 取消（**同一个端点**，用 reason 区分意图）。

    `OrderActionRequest` 里没有 cancel 字段，所以取消不新增端点：
        * reason 形如 `cancel: 原因` / `取消：原因`（或恰好 `cancel`/`取消`）
          -> 视为**取消**：draft / submitted -> cancelled
        * 其他 reason -> 视为**驳回**：submitted / approved -> rejected

    两种情况都**必须**带 reason，否则 400 REASON_REQUIRED；
    非法迁移 409 INVALID_STATE。原因落到订单 note（表里没有独立 reason 列）。
    """

    _require_db()
    reason = str(payload.reason or "").strip()
    if not reason:
        raise ServiceError(
            400,
            "REASON_REQUIRED",
            "驳回或取消必须填写原因（reason）；取消请用 `cancel: 原因` 前缀",
        )

    cancel = _is_cancel_reason(reason)
    body = _reason_body(reason) or reason
    return await _advance(
        order_id,
        payload,
        target="cancelled" if cancel else "rejected",
        allowed_from=("draft", "submitted") if cancel else ("submitted", "approved"),
        stamp_field="closed_at",
        action_label="取消" if cancel else "驳回",
        audit_line=f"{'取消' if cancel else '驳回'}原因：{body}",
        trace_id=trace_id,
    )


async def receive_order(
    order_id: int, payload: OrderActionRequest, *, trace_id: str
) -> dict | None:
    """收货登记（approved -> received）。

    只登记「已到货」这一事实，**不做入库扣减**（本模块不碰库存台账）。
    """

    operator = str(payload.operator or "").strip()
    return await _advance(
        order_id,
        payload,
        target="received",
        allowed_from=("approved",),
        stamp_field="closed_at",
        action_label="收货",
        audit_line=f"收货人：{operator}" if operator else "",
        trace_id=trace_id,
    )


# --------------------------------------------------------------------------- #
# ③ 采购结算台账
# --------------------------------------------------------------------------- #


def _settlement_view(record: PartSettlement, trace_id: str = "") -> dict[str, Any]:
    return {
        "id": int(record.id or 0),
        "order_id": int(record.order_id or 0),
        "order_no": str(record.order_no or ""),
        "amount": round(_as_float(record.amount), 2),
        "currency": str(record.currency or CURRENCY),
        "method": str(record.method or "月结"),
        "invoice_no": str(record.invoice_no or ""),
        "operator": str(record.operator or ""),
        "note": str(record.note or ""),
        "settled_at": _iso(record.settled_at),
        "trace_id": trace_id,
    }


async def settle_order(order_id: int, payload: SettlementRequest, *, trace_id: str) -> dict:
    """订单结算（POST /parts/orders/{order_id}/settle），写一条 `part_settlement`。

    规则与幂等：
        * 只允许 `approved` / `received` 的订单结算，其他状态 -> 409 INVALID_STATE
        * **幂等保护**：同一订单已有结算记录 -> 409 ALREADY_SETTLED（不重复记账）
        * `amount` 缺省用订单 `total_amount`
        * 结算后订单状态保持 received；若仍为 approved 则置为 received

    只记台账，不做真实财务过账（开发文档 §1.3：不与财务系统集成）。
    """

    _require_db()
    async with db.session_scope() as session:
        order = await session.get(PartOrder, _as_int(order_id))
        if order is None:
            raise ServiceError(404, "ORDER_NOT_FOUND", f"采购申请单不存在：{order_id}")

        if str(order.status or "") not in ("approved", "received"):
            raise ServiceError(
                409,
                "INVALID_STATE",
                f"订单 {order.order_no} 当前状态为 {order.status}，不可结算"
                "（仅允许 approved / received）",
                {"order_no": order.order_no, "status": order.status},
            )

        existing = (
            (
                await session.execute(
                    select(PartSettlement)
                    .where(PartSettlement.order_id == order.id)
                    .order_by(PartSettlement.id)
                )
            )
            .scalars()
            .first()
        )
        if existing is not None:
            raise ServiceError(
                409,
                "ALREADY_SETTLED",
                f"订单 {order.order_no} 已结算（结算单 #{existing.id}），不可重复结算",
                {"order_no": order.order_no, "settlement_id": int(existing.id)},
            )

        amount = (
            round(_as_float(order.total_amount), 2)
            if payload.amount is None
            else round(_as_float(payload.amount), 2)
        )
        now = _now()
        record = PartSettlement(
            order_id=int(order.id),
            order_no=str(order.order_no),
            amount=amount,
            currency=str(order.currency or CURRENCY),
            method=str(payload.method or "月结"),
            invoice_no=str(payload.invoice_no or ""),
            operator=str(payload.operator or ""),
            note=str(payload.note or ""),
            settled_at=now,
            created_at=now,
        )
        session.add(record)

        if str(order.status or "") == "approved":
            order.status = "received"
            order.closed_at = now
        order.updated_at = now

        await session.flush()
        view = _settlement_view(record, trace_id)

    logger.info("订单 %s 已结算：%.2f %s（%s）", view["order_no"], amount, view["currency"], view["method"])
    return view


async def list_settlements(
    *, order_no: str | None, limit: int, offset: int, trace_id: str
) -> dict:
    """结算台账（GET /parts/settlements），按 id 倒序。

    `total` 是过滤后的记录数，`total_amount` 是**过滤后全部记录**的金额合计
    （不受分页限制，便于对账）。
    """

    _require_db()
    limit, offset = _page(limit, offset)
    conditions = []
    if order_no and order_no.strip():
        conditions.append(PartSettlement.order_no.like(f"%{order_no.strip()}%"))

    async with db.session_scope() as session:
        total = int(
            (
                await session.execute(
                    select(func.count()).select_from(PartSettlement).where(*conditions)
                )
            ).scalar_one()
            or 0
        )
        total_amount = round(
            _as_float(
                (
                    await session.execute(
                        select(func.coalesce(func.sum(PartSettlement.amount), 0.0)).where(
                            *conditions
                        )
                    )
                ).scalar_one()
            ),
            2,
        )
        rows = (
            (
                await session.execute(
                    select(PartSettlement)
                    .where(*conditions)
                    .order_by(PartSettlement.id.desc())
                    .limit(limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
        items = [_settlement_view(record, trace_id) for record in rows]

    return {
        "items": items,
        "total": total,
        "total_amount": total_amount,
        "trace_id": trace_id,
    }


__all__ = [
    "CATALOG_CODES",
    "MAX_ITEM_QTY",
    "ORDER_STATUSES",
    "add_order_item",
    "allowed_actions",
    "approve_order",
    "create_order",
    "get_order",
    "get_part",
    "list_catalog",
    "list_orders",
    "list_settlements",
    "receive_order",
    "reject_order",
    "remove_order_item",
    "settle_order",
    "submit_order",
    "update_order_item",
]
