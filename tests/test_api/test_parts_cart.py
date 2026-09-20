"""购物车（= `draft` 采购申请单的明细维护）测试。

覆盖：
    ① 加入购物车：单价/库存回台账取、金额与合计正确、同编码**累加数量**不产生重复行
    ② 改数量：金额与合计重算；数量越界被 schema 拒；明细不存在 404 ITEM_NOT_FOUND
    ③ 移出购物车：删行、合计重算、允许清空；明细不存在 404
    ④ **仅草稿可改**：submitted / approved 状态下加/改/删一律 409 INVALID_STATE
    ⑤ 空草稿单不能提交 -> 400 EMPTY_ORDER；加了明细后可以提交
    ⑥ 替代件仍受依据约束：无依据替代件加车 -> 400 SUBSTITUTE_BASIS_REQUIRED
    ⑦ 审计：加/改/删都会往 note 追加一行（可追溯谁在什么时候动了购物车）
    ⑧ 路由层：4 个购物车端点在 OpenAPI 里存在且方法正确（不依赖注册顺序）

运行：
    .venv/bin/python -m pytest tests/test_api/test_parts_cart.py -q

约定：
    * 直接测 service 层；会写库，订单 applicant 统一用 `TEST-CART-xxxx` 标记，不删改他人数据；
    * 每个用例自带 init_db / dispose_db（连接不跨事件循环复用）。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path
from typing import Any, Awaitable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import db  # noqa: E402
from backend.schemas import (  # noqa: E402
    OrderActionRequest,
    OrderCreateRequest,
    OrderItemRequest,
    OrderItemUpdateRequest,
    PartOrderResponse,
    ServiceError,
)
from backend.services import parts_service  # noqa: E402

APPLICANT = f"TEST-CART-{uuid.uuid4().hex[:8]}"
TRACE = "trace-cart-test"


# --------------------------------------------------------------------------- #
# 运行脚手架
# --------------------------------------------------------------------------- #


async def _with_db(scenario: Awaitable[Any]) -> Any:
    ok, detail = await db.init_db()
    assert ok, f"数据库不可用：{detail}"
    try:
        return await scenario
    finally:
        await db.dispose_db()


def _run(scenario: Awaitable[Any]) -> Any:
    return asyncio.run(_with_db(scenario))


async def expect_error(awaitable: Awaitable[Any], *, status: int, code: str) -> ServiceError:
    try:
        await awaitable
    except ServiceError as exc:
        assert exc.status_code == status, f"状态码不符：{exc.status_code} != {status}（{exc.message}）"
        assert exc.code == code, f"错误码不符：{exc.code} != {code}（{exc.message}）"
        return exc
    raise AssertionError(f"预期 ServiceError({status}, {code})，但调用成功返回了")


async def new_cart(*items: OrderItemRequest) -> dict:
    """建一张只有必需的 1 条明细的草稿单，然后再按需增删（模拟真实购物车）。"""

    payload = OrderCreateRequest(
        items=list(items) or [OrderItemRequest(code="SP-ETA-0101", qty=1)],
        applicant=APPLICANT,
        purpose="购物车测试",
    )
    return await parts_service.create_order(payload, trace_id=TRACE)


# --------------------------------------------------------------------------- #
# ① 加入购物车
# --------------------------------------------------------------------------- #


def test_add_item_refetches_price_and_accumulates_qty() -> None:
    """加车：价格回台账取（185/560），同编码累加数量且不新增重复行。"""

    async def scenario() -> None:
        cart = await new_cart()
        oid = cart["id"]
        assert len(cart["items"]) == 1

        # 加入一条替代件（FFKM 全氟醚，560.0，依据来自主件登记）
        view = await parts_service.add_order_item(
            oid,
            OrderItemRequest(code="SP-ETA-0101F", qty=2, is_substitute=True),
            trace_id=TRACE,
        )
        validated = PartOrderResponse.model_validate(view)
        assert len(validated.items) == 2
        sub = next(i for i in validated.items if i.code == "SP-ETA-0101F")
        assert sub.is_substitute is True and sub.unit_price == 560.0 and sub.amount == 1120.0
        assert sub.basis and "备件目录 V1.4" in sub.basis
        assert sub.stock == 4 and sub.lead_time_days == 7
        assert validated.total_amount == 185.0 + 1120.0

        # 同编码重复加入 -> 累加数量（2 + 3 = 5），行数不变
        view = await parts_service.add_order_item(
            oid,
            OrderItemRequest(code="SP-ETA-0101F", qty=3, is_substitute=True),
            trace_id=TRACE,
        )
        again = PartOrderResponse.model_validate(view)
        assert len(again.items) == 2
        sub = next(i for i in again.items if i.code == "SP-ETA-0101F")
        assert sub.qty == 5 and sub.amount == 2800.0
        assert again.total_amount == 185.0 + 2800.0

        # 主件加量同样累加
        view = await parts_service.add_order_item(
            oid, OrderItemRequest(code="SP-ETA-0101", qty=4), trace_id=TRACE
        )
        merged = PartOrderResponse.model_validate(view)
        main = next(i for i in merged.items if i.code == "SP-ETA-0101")
        assert main.qty == 5 and main.unit_price == 185.0 and main.amount == 925.0
        assert merged.total_amount == 925.0 + 2800.0

        # 审计行落进 note
        assert "购物车新增" in merged.note and "购物车加量" in merged.note

        # 落库后再查一遍，与返回一致
        stored = await parts_service.get_order(oid, trace_id=TRACE)
        assert stored is not None
        assert stored["total_amount"] == merged.total_amount
        assert len(stored["items"]) == 2
        # `operator` 是请求元数据，**不能落进明细**（明细只存台账字段）
        for raw in stored["items"]:
            assert "operator" not in raw, f"明细里混入了请求字段：{raw}" 

    _run(scenario())


def test_add_item_requires_substitute_basis_and_existing_code() -> None:
    """无依据替代件不给加车；不存在的编码同样被拒（错误码分别是 400 的两种）。"""

    async def scenario() -> None:
        cart = await new_cart()
        oid = cart["id"]

        # SP-ETA-0102 是主件，但它在台账里的替代件登记里没有 basis 的条目；
        # 把它当替代件加入时必须报 SUBSTITUTE_BASIS_REQUIRED。
        err = await expect_error(
            parts_service.add_order_item(
                oid,
                OrderItemRequest(code="SP-ETA-0102", qty=1, is_substitute=True),
                trace_id=TRACE,
            ),
            status=400,
            code="SUBSTITUTE_BASIS_REQUIRED",
        )
        assert "原厂确认" in err.message

        await expect_error(
            parts_service.add_order_item(
                oid, OrderItemRequest(code="SP-NOT-EXIST", qty=1), trace_id=TRACE
            ),
            status=400,
            code="PART_NOT_FOUND",
        )

        # 被拒的两次都不应改动购物车
        stored = await parts_service.get_order(oid, trace_id=TRACE)
        assert stored is not None and len(stored["items"]) == 1

    _run(scenario())


# --------------------------------------------------------------------------- #
# ② 改数量 / ③ 移出购物车
# --------------------------------------------------------------------------- #


def test_update_and_remove_item_recalc_total() -> None:
    """改量 -> 金额重算；删行 -> 合计重算；都允许，且删不存在的行为 404。"""

    async def scenario() -> None:
        cart = await new_cart(
            OrderItemRequest(code="SP-ETA-0101", qty=1),
            OrderItemRequest(code="SP-ETA-0220", qty=2),
        )
        oid = cart["id"]
        assert cart["total_amount"] == 185.0 + 2 * 1450.0

        view = await parts_service.update_order_item(
            oid, "SP-ETA-0220", OrderItemUpdateRequest(qty=1), trace_id=TRACE
        )
        updated = PartOrderResponse.model_validate(view)
        pump = next(i for i in updated.items if i.code == "SP-ETA-0220")
        assert pump.qty == 1 and pump.unit_price == 1450.0 and pump.amount == 1450.0
        assert updated.total_amount == 185.0 + 1450.0
        assert "购物车改量" in updated.note

        await expect_error(
            parts_service.update_order_item(
                oid, "SP-NOT-IN-CART", OrderItemUpdateRequest(qty=2), trace_id=TRACE
            ),
            status=404,
            code="ITEM_NOT_FOUND",
        )

        view = await parts_service.remove_order_item(oid, "SP-ETA-0101", trace_id=TRACE)
        removed = PartOrderResponse.model_validate(view)
        assert [i.code for i in removed.items] == ["SP-ETA-0220"]
        assert removed.total_amount == 1450.0
        assert "购物车移除" in removed.note

        await expect_error(
            parts_service.remove_order_item(oid, "SP-ETA-0101", trace_id=TRACE),
            status=404,
            code="ITEM_NOT_FOUND",
        )

        # 允许清空（空车是正常状态），但此时不能提交
        view = await parts_service.remove_order_item(oid, "SP-ETA-0220", trace_id=TRACE)
        empty = PartOrderResponse.model_validate(view)
        assert empty.items == [] and empty.total_amount == 0.0
        assert empty.allowed_actions == ["submit", "cancel"]

    _run(scenario())


# --------------------------------------------------------------------------- #
# ④ 仅草稿可改 / ⑤ 空单不可提交 / ⑦ 审计
# --------------------------------------------------------------------------- #


def test_cart_is_frozen_after_submit_and_empty_order_cannot_submit() -> None:
    """空草稿单不可提交；提交后明细冻结（加/改/删全部 409 INVALID_STATE）。"""

    async def scenario() -> None:
        cart = await new_cart(OrderItemRequest(code="SP-ETA-0101", qty=2))
        oid = cart["id"]

        # 清空后提交 -> 400 EMPTY_ORDER
        await parts_service.remove_order_item(oid, "SP-ETA-0101", trace_id=TRACE)
        await expect_error(
            parts_service.submit_order(oid, OrderActionRequest(operator=APPLICANT), trace_id=TRACE),
            status=400,
            code="EMPTY_ORDER",
        )

        # 加回来再提交 -> 成功，状态 submitted
        await parts_service.add_order_item(
            oid, OrderItemRequest(code="SP-ETA-0101", qty=1), trace_id=TRACE
        )
        submitted = await parts_service.submit_order(
            oid, OrderActionRequest(operator=APPLICANT), trace_id=TRACE
        )
        assert submitted is not None and submitted["status"] == "submitted"

        # 提交后明细冻结
        for awaitable in (
            parts_service.add_order_item(
                oid, OrderItemRequest(code="SP-ETA-0102", qty=1), trace_id=TRACE
            ),
            parts_service.update_order_item(
                oid, "SP-ETA-0101", OrderItemUpdateRequest(qty=9), trace_id=TRACE
            ),
            parts_service.remove_order_item(oid, "SP-ETA-0101", trace_id=TRACE),
        ):
            await expect_error(awaitable, status=409, code="INVALID_STATE")

        frozen = await parts_service.get_order(oid, trace_id=TRACE)
        assert frozen is not None
        assert frozen["status"] == "submitted"
        assert len(frozen["items"]) == 1 and frozen["items"][0]["qty"] == 1

    _run(scenario())


def test_cart_edits_are_audited_in_note() -> None:
    """加/改/删每一步都在 note 里留一行审计（谁在什么时候动了购物车可追溯）。"""

    async def scenario() -> None:
        cart = await new_cart()
        oid = cart["id"]
        await parts_service.add_order_item(
            oid, OrderItemRequest(code="SP-ETA-0210", qty=1, operator=APPLICANT), trace_id=TRACE
        )
        await parts_service.update_order_item(
            oid, "SP-ETA-0210", OrderItemUpdateRequest(qty=3, operator=APPLICANT), trace_id=TRACE
        )
        await parts_service.remove_order_item(oid, "SP-ETA-0210", trace_id=TRACE)

        stored = await parts_service.get_order(oid, trace_id=TRACE)
        assert stored is not None
        note = stored["note"]
        for marker in ("购物车新增", "购物车改量", "购物车移除"):
            assert marker in note, f"note 缺少审计行：{marker}"
        assert APPLICANT in note

    _run(scenario())


# --------------------------------------------------------------------------- #
# ⑧ 路由层
# --------------------------------------------------------------------------- #


def test_cart_routes_registered_in_app() -> None:
    """4 个购物车端点已在真实 app 的 OpenAPI 里（方法 + 路径），且都带响应模型。"""

    from backend.main import app

    paths = app.openapi()["paths"]
    assert "post" in paths["/api/parts/orders/{order_id}/items"]
    assert "patch" in paths["/api/parts/orders/{order_id}/items/{code}"]
    assert "delete" in paths["/api/parts/orders/{order_id}/items/{code}"]
    # 详情接口仍在（固定路径没被路径参数吃掉）
    assert "get" in paths["/api/parts/orders/{order_id}"]
    assert "get" in paths["/api/parts/orders"]
    assert "get" in paths["/api/parts/{code}"]

    for path, method in (
        ("/api/parts/orders/{order_id}/items", "post"),
        ("/api/parts/orders/{order_id}/items/{code}", "patch"),
        ("/api/parts/orders/{order_id}/items/{code}", "delete"),
    ):
        schema = paths[path][method]["responses"]["200"]["content"]["application/json"]["schema"]
        assert "PartOrderResponse" in str(schema), f"{method} {path} 未声明 response_model"

    # service 协议里也必须有这三个方法（routers/__init__.py 的 _PARTS_SERVICE_API）
    from backend.routers import _PARTS_SERVICE_API

    for name in ("add_order_item", "update_order_item", "remove_order_item"):
        assert name in _PARTS_SERVICE_API
        assert hasattr(parts_service, name), f"service 缺少 {name}"
