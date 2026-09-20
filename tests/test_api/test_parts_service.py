"""备件商城与采购 service 测试（FR-10 / 开发文档 6.2 parts_service.py）。

覆盖：
    ① 商城目录：型号/关键词过滤、分页、字段来自备件台账（价格不编造）
    ② 下单：明细编码校验、**替代件无依据被拒**、有依据成功（价格与金额算术）
    ③ 状态机：合法迁移全链路 + 非法迁移 409 INVALID_STATE
    ④ reject 不带 reason -> 400 REASON_REQUIRED
    ⑤ 结算：缺省金额、approved 结算后转 received、重复结算 409 ALREADY_SETTLED
    ⑥ 订单不存在 -> None（settle_order 为 404 ServiceError）
    ⑦ allowed_actions 随状态变化

运行：
    .venv/bin/python -m pytest tests/test_api/test_parts_service.py -q

约定：
    * 直接测 service 层，**不依赖路由是否已注册**（路由导入另测）。
    * 会写库：所有订单用唯一的 applicant 标记（TEST-PARTS-xxxx），
      只读不删改其他数据；每个用例开始 init_db、结束 dispose_db，
      保证每个用例的连接都绑在自己的事件循环上（asyncio.run 每次换 loop）。
"""

from __future__ import annotations

import asyncio
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import db  # noqa: E402
from backend.schemas import (  # noqa: E402
    OrderActionRequest,
    OrderCreateRequest,
    OrderItemRequest,
    PartCatalogResponse,
    PartItem,
    PartOrderResponse,
    ServiceError,
    SettlementListResponse,
    SettlementRequest,
    SettlementResponse,
)
from backend.services import parts_service  # noqa: E402
from backend.tools import kb_tools  # noqa: E402

#: 本文件创建的订单都带这个申请人标记，便于识别（不删改别人的数据）
APPLICANT = f"TEST-PARTS-{uuid.uuid4().hex[:8]}"

ORDER_NO_RE = re.compile(r"^PO-\d{8}-\d{4}$")

TRACE = "trace-parts-test"


# --------------------------------------------------------------------------- #
# 运行脚手架
# --------------------------------------------------------------------------- #


async def _with_db(scenario: Awaitable[Any]) -> Any:
    """每个用例自带一次 init_db / dispose_db（连接不跨事件循环复用）。"""

    ok, detail = await db.init_db()
    assert ok, f"数据库不可用：{detail}"
    try:
        return await scenario
    finally:
        await db.dispose_db()


def _run(scenario: Awaitable[Any]) -> Any:
    return asyncio.run(_with_db(scenario))


async def expect_error(
    awaitable: Awaitable[Any], *, status: int, code: str
) -> ServiceError:
    """断言调用抛出指定业务错误（并返回该异常，便于看 detail）。"""

    try:
        await awaitable
    except ServiceError as exc:
        assert exc.status_code == status, f"状态码不符：{exc.status_code} != {status}（{exc.message}）"
        assert exc.code == code, f"错误码不符：{exc.code} != {code}（{exc.message}）"
        return exc
    raise AssertionError(f"预期 ServiceError({status}, {code})，但调用成功返回了")


async def create_order(*items: OrderItemRequest, **kwargs: Any) -> dict:
    payload = OrderCreateRequest(items=list(items), applicant=APPLICANT, **kwargs)
    return await parts_service.create_order(payload, trace_id=TRACE)


def item(code: str, qty: int = 1, *, is_substitute: bool = False) -> OrderItemRequest:
    return OrderItemRequest(code=code, qty=qty, is_substitute=is_substitute)


# --------------------------------------------------------------------------- #
# ① 商城目录
# --------------------------------------------------------------------------- #


def test_catalog_lists_all_parts_with_ledger_fields() -> None:
    """目录逐条来自 parts_query：库存/单位/库位/到货时间/替代件依据都对得上。"""

    async def scenario() -> None:
        result = await parts_service.list_catalog(
            device_model=None, keyword=None, limit=20, offset=0, trace_id=TRACE
        )
        # 可被 PartCatalogResponse 直接校验（契约一致性）
        validated = PartCatalogResponse.model_validate(result)
        assert result["trace_id"] == TRACE
        assert validated.total == len(parts_service.CATALOG_CODES) == 5

        by_code = {entry.code: entry for entry in validated.items}
        assert "SP-ETA-0101" in by_code

        oring = by_code["SP-ETA-0101"]
        raw = await kb_tools.parts_query("SP-ETA-0101")
        assert oring.part == "腔体门 O-ring"
        assert oring.device_model == "Etcher-A"
        assert oring.stock == 12 and oring.unit == "个"
        assert oring.location == "备件库 A-03" and oring.lead_time_days == 3
        assert oring.stock_status == "ok"
        # 价格/供应商：台账（parts_query 返回结构）里没有 -> 0.0 / ""，**不编造价格**
        assert oring.price_cny == float(raw.get("price_cny") or 0.0)
        assert oring.supplier == str(raw.get("supplier") or "")
        # 替代件与依据原样透出（PartItem.forbidden 是 list[str]，已格式化）
        assert [s["code"] for s in oring.substitutes] == ["SP-ETA-0101F"]
        assert "备件目录 V1.4" in oring.substitutes[0]["basis"]
        assert all(isinstance(line, str) for line in oring.forbidden)
        assert any("禁止替代" in line for line in oring.forbidden)

        # 单条查询：PartItem 可校验；库存为 0 的件被标成 low
        single = await parts_service.get_part("SP-ETA-0310", trace_id=TRACE)
        assert single is not None
        assert PartItem.model_validate(single).stock_status == "low"

    _run(scenario())


def test_catalog_filters_and_pagination() -> None:
    """型号过滤（含「通用」语义）、关键词模糊匹配与分页，total 为过滤后总数。"""

    async def scenario() -> None:
        async def catalog(**kwargs: Any) -> dict:
            params = {"device_model": None, "keyword": None, "limit": 20, "offset": 0}
            params.update(kwargs)
            return await parts_service.list_catalog(trace_id=TRACE, **params)

        # 型号：Etcher-A 全中；不存在的型号为空；台账里的「通用」件在任何型号下都应包含
        assert (await catalog(device_model="Etcher-A"))["total"] == 5
        assert (await catalog(device_model="Etcher-B"))["total"] == 0
        generic = await catalog(device_model="Etcher-B")
        assert generic["items"] == [] and generic["total"] == 0
        assert parts_service._model_matches(parts_service.GENERIC_MODEL, "Etcher-Z") is True

        # 关键词：部件名 / 编码 / 规格 三种命中口径
        by_name = await catalog(keyword="O-ring")
        assert by_name["total"] == 2
        assert {entry["code"] for entry in by_name["items"]} == {"SP-ETA-0101", "SP-ETA-0102"}
        by_code = await catalog(keyword="SP-ETA-0220")
        assert by_code["total"] == 1 and by_code["items"][0]["part"] == "真空泵油"
        by_spec = await catalog(keyword="rp-300")  # 规格里的小写写法也要能命中
        assert by_spec["total"] == 1 and by_spec["items"][0]["code"] == "SP-ETA-0310"
        assert (await catalog(keyword="不存在的备件XYZ"))["total"] == 0

        # 分页：total 是过滤后的总数，不随分页变化
        page1 = await catalog(keyword="SP-ETA-01", limit=2, offset=0)
        page2 = await catalog(keyword="SP-ETA-01", limit=2, offset=2)
        assert page1["total"] == page2["total"] == 2
        assert len(page1["items"]) == 2 and page2["items"] == []
        first = await catalog(limit=2, offset=0)
        second = await catalog(limit=2, offset=2)
        third = await catalog(limit=2, offset=4)
        assert [len(page["items"]) for page in (first, second, third)] == [2, 2, 1]
        codes = [entry["code"] for page in (first, second, third) for entry in page["items"]]
        assert len(codes) == len(set(codes)) == 5

    _run(scenario())


# --------------------------------------------------------------------------- #
# ② 下单（含替代件依据）
# --------------------------------------------------------------------------- #


def test_create_order_rejects_unknown_part_and_basisless_substitute() -> None:
    """编码不在台账 -> PART_NOT_FOUND；替代件没有依据 -> SUBSTITUTE_BASIS_REQUIRED。"""

    async def scenario() -> None:
        await expect_error(
            create_order(item("SP-XXX-9999")),
            status=400,
            code="PART_NOT_FOUND",
        )
        # 主件编码被标成替代件，但它自己登记的 substitutes 里没有这条 -> 无依据，拒单
        await expect_error(
            create_order(item("SP-ETA-0102", is_substitute=True)),
            status=400,
            code="SUBSTITUTE_BASIS_REQUIRED",
        )
        # 有依据的替代件也存在，但若编码写成主件 + is_substitute=True 同样按无依据处理
        await expect_error(
            create_order(item("SP-ETA-0101", 2, is_substitute=True)),
            status=400,
            code="SUBSTITUTE_BASIS_REQUIRED",
        )
        # 被拒的请求不应留下订单（对比前后条数，不依赖他人数据）
        before = (
            await parts_service.list_orders(
                status=None, applicant=APPLICANT, limit=50, offset=0, trace_id=TRACE
            )
        )["total"]
        await expect_error(create_order(item("SP-XXX-9999")), status=400, code="PART_NOT_FOUND")
        after = (
            await parts_service.list_orders(
                status=None, applicant=APPLICANT, limit=50, offset=0, trace_id=TRACE
            )
        )["total"]
        assert after == before == 0  # 本文件此前没有成功建过单

    _run(scenario())


def test_create_order_with_substitute_basis_succeeds() -> None:
    """带依据的替代件可下单：编码/依据/库存/交期取台账，金额按单价算术。"""

    async def scenario() -> None:
        order = await create_order(
            item("SP-ETA-0101", 1),
            item("SP-ETA-0101F", 2, is_substitute=True),
            purpose="腔体门密封更换（工单 WO-2026-001）",
            device_model="Etcher-A",
            note="急件",
        )
        validated = PartOrderResponse.model_validate(order)
        assert validated.status == "draft"
        assert validated.currency == "CNY"
        assert validated.applicant == APPLICANT
        assert ORDER_NO_RE.match(validated.order_no), validated.order_no
        assert validated.allowed_actions == ["submit", "cancel"]
        assert validated.created_at is not None and validated.updated_at is not None

        primary, substitute = validated.items
        assert primary.code == "SP-ETA-0101" and primary.is_substitute is False
        assert primary.basis == "" and primary.stock == 12 and primary.lead_time_days == 3

        # 替代件明细：编码/名称/库存/交期来自它自己的台账条目，依据来自主件登记
        sub_raw = await kb_tools.parts_query("SP-ETA-0101F")  # 工具本身查不到替代件编码
        assert sub_raw["found"] is False
        assert substitute.code == "SP-ETA-0101F"
        assert substitute.is_substitute is True
        assert substitute.part == "腔体门 O-ring（FFKM 全氟醚）"
        assert substitute.stock == 4 and substitute.lead_time_days == 7
        assert substitute.unit == "个"
        assert substitute.basis and "备件目录 V1.4" in substitute.basis
        # 价格来自台账载荷（parts_query 已透传 price_cny/supplier -> 560.0），金额 = 数量 * 单价
        assert substitute.unit_price == 560.0
        assert substitute.amount == 2 * substitute.unit_price
        assert primary.unit_price == 185.0 and primary.amount == 185.0
        assert validated.total_amount == primary.amount + substitute.amount

        # 落库后再查一遍，明细与合计一致
        again = await parts_service.get_order(validated.id, trace_id=TRACE)
        assert again is not None
        assert again["total_amount"] == validated.total_amount
        assert [entry["code"] for entry in again["items"]] == ["SP-ETA-0101", "SP-ETA-0101F"]

    _run(scenario())


def test_order_prices_come_from_ledger_payload() -> None:
    """价格一律取台账载荷（price_cny）：数据源不给就是 0，给了就按它算，不编造。

    这里给工具返回打一个「补上价格」的桩，验证 price -> unit_price -> amount ->
    total_amount 这条链路（平台侧把价格接进 parts_query 后无需改 service）。
    """

    async def scenario() -> None:
        real_query = kb_tools.parts_query
        prices = {"SP-ETA-0101": 185.0, "SP-ETA-0101F": 560.0}

        async def query_with_price(part: str, device_model: str | None = None) -> dict:
            payload = await real_query(part, device_model)
            if payload.get("found"):
                payload["price_cny"] = prices.get(payload.get("code"), 0.0)
                for sub in payload.get("substitutes") or []:
                    sub["price_cny"] = prices.get(sub.get("code"), 0.0)
            return payload

        kb_tools.parts_query = query_with_price  # type: ignore[assignment]
        try:
            order = await create_order(
                item("SP-ETA-0101", 2), item("SP-ETA-0101F", 3, is_substitute=True)
            )
        finally:
            kb_tools.parts_query = real_query  # type: ignore[assignment]

        primary, substitute = order["items"]
        assert primary["unit_price"] == 185.0 and primary["amount"] == 370.0
        assert substitute["unit_price"] == 560.0 and substitute["amount"] == 1680.0
        assert order["total_amount"] == 2050.0

        # 恢复真实数据源后，同一订单的金额不受影响（金额已落库）
        again = await parts_service.get_order(order["id"], trace_id=TRACE)
        assert again is not None and again["total_amount"] == 2050.0

    _run(scenario())


# --------------------------------------------------------------------------- #
# ③ 状态机
# --------------------------------------------------------------------------- #


def test_state_machine_rejects_illegal_transitions() -> None:
    """只允许 draft->submitted->approved->received；非法迁移一律 409。"""

    async def scenario() -> None:
        order = await create_order(item("SP-ETA-0101", 1))
        oid = order["id"]

        # draft 直接 approve / receive / settle / 驳回 都是非法迁移
        await expect_error(
            parts_service.approve_order(oid, OrderActionRequest(operator="李工"), trace_id=TRACE),
            status=409,
            code="INVALID_STATE",
        )
        await expect_error(
            parts_service.receive_order(oid, OrderActionRequest(), trace_id=TRACE),
            status=409,
            code="INVALID_STATE",
        )
        await expect_error(
            parts_service.settle_order(oid, SettlementRequest(), trace_id=TRACE),
            status=409,
            code="INVALID_STATE",
        )
        await expect_error(
            parts_service.reject_order(
                oid, OrderActionRequest(reason="资质不符"), trace_id=TRACE
            ),
            status=409,
            code="INVALID_STATE",
        )

        # 合法路径
        submitted = await parts_service.submit_order(oid, OrderActionRequest(operator="王工"), trace_id=TRACE)
        assert submitted is not None and submitted["status"] == "submitted"
        approved = await parts_service.approve_order(
            oid, OrderActionRequest(operator="李工", note="预算内"), trace_id=TRACE
        )
        assert approved is not None and approved["status"] == "approved"
        assert "审批人：李工" in approved["note"]
        received = await parts_service.receive_order(
            oid, OrderActionRequest(operator="仓库"), trace_id=TRACE
        )
        assert received is not None and received["status"] == "received"

        # 重复操作（submit/approve/receive 各再来一次）都是非法迁移
        await expect_error(
            parts_service.submit_order(oid, OrderActionRequest(), trace_id=TRACE),
            status=409,
            code="INVALID_STATE",
        )
        await expect_error(
            parts_service.approve_order(oid, OrderActionRequest(), trace_id=TRACE),
            status=409,
            code="INVALID_STATE",
        )
        await expect_error(
            parts_service.receive_order(oid, OrderActionRequest(), trace_id=TRACE),
            status=409,
            code="INVALID_STATE",
        )
        # 已收货的订单不能再驳回（reject 只处理 submitted/approved）
        await expect_error(
            parts_service.reject_order(oid, OrderActionRequest(reason="不要了"), trace_id=TRACE),
            status=409,
            code="INVALID_STATE",
        )
        # 状态没被非法请求改动
        final = await parts_service.get_order(oid, trace_id=TRACE)
        assert final is not None and final["status"] == "received"

    _run(scenario())


def test_reject_requires_reason_and_cancel_uses_reason_prefix() -> None:
    """reject/取消都必须带 reason（400）；取消同端点用 cancel:/取消： 前缀区分。"""

    async def scenario() -> None:
        draft = await create_order(item("SP-ETA-0102", 1))
        # 缺 reason -> 400（含空白 reason）
        await expect_error(
            parts_service.reject_order(draft["id"], OrderActionRequest(), trace_id=TRACE),
            status=400,
            code="REASON_REQUIRED",
        )
        await expect_error(
            parts_service.reject_order(
                draft["id"], OrderActionRequest(reason="   "), trace_id=TRACE
            ),
            status=400,
            code="REASON_REQUIRED",
        )
        # draft 取消（reason 带取消前缀）
        cancelled = await parts_service.reject_order(
            draft["id"],
            OrderActionRequest(reason="cancel: 需求取消，工单已关闭", operator="王工"),
            trace_id=TRACE,
        )
        assert cancelled is not None and cancelled["status"] == "cancelled"
        assert cancelled["allowed_actions"] == []
        assert "取消原因：需求取消，工单已关闭" in cancelled["note"]

        # submitted 取消（中文前缀）
        draft2 = await create_order(item("SP-ETA-0220", 1))
        await parts_service.submit_order(draft2["id"], OrderActionRequest(), trace_id=TRACE)
        cancelled2 = await parts_service.reject_order(
            draft2["id"], OrderActionRequest(reason="取消：库存已够用"), trace_id=TRACE
        )
        assert cancelled2 is not None and cancelled2["status"] == "cancelled"

        # 普通 reason 走驳回：submitted -> rejected（原因是必填内容）
        draft3 = await create_order(item("SP-ETA-0210", 1))
        await parts_service.submit_order(draft3["id"], OrderActionRequest(), trace_id=TRACE)
        rejected = await parts_service.reject_order(
            draft3["id"], OrderActionRequest(reason="预算未批"), trace_id=TRACE
        )
        assert rejected is not None and rejected["status"] == "rejected"
        assert "驳回原因：预算未批" in rejected["note"]
        await expect_error(
            parts_service.reject_order(
                draft3["id"], OrderActionRequest(reason="cancel: 再取消一次"), trace_id=TRACE
            ),
            status=409,
            code="INVALID_STATE",
        )

        # 取消标记必须带分隔符：以「取消」开头但没有分隔符的原因按普通驳回处理
        assert parts_service._is_cancel_reason("取消替代件方案") is False
        assert parts_service._is_cancel_reason("取消：库存已够用") is True
        assert parts_service._is_cancel_reason("cancel") is True
        assert parts_service._is_cancel_reason("CANCEL: duplicated") is True

    _run(scenario())


# --------------------------------------------------------------------------- #
# ④ 结算台账
# --------------------------------------------------------------------------- #


def test_settlement_is_idempotent_and_closes_approved_order() -> None:
    """结算：缺省用订单金额、重复结算 409、approved 结算后转 received、台账可查。"""

    async def scenario() -> None:
        # (a) approved 直接结算 -> 转 received
        order = await create_order(item("SP-ETA-0101", 2), purpose="结算幂等用例")
        await parts_service.submit_order(order["id"], OrderActionRequest(), trace_id=TRACE)
        approved = await parts_service.approve_order(
            order["id"], OrderActionRequest(operator="李工"), trace_id=TRACE
        )
        assert approved is not None and approved["status"] == "approved"

        record = await parts_service.settle_order(
            order["id"], SettlementRequest(method="月结", invoice_no="INV-2026-001", operator="财务"), trace_id=TRACE
        )
        validated = SettlementResponse.model_validate(record)
        assert validated.order_no == order["order_no"]
        assert validated.amount == order["total_amount"]  # 缺省用订单金额
        assert validated.currency == "CNY" and validated.method == "月结"
        assert validated.settled_at is not None and validated.trace_id == TRACE

        after = await parts_service.get_order(order["id"], trace_id=TRACE)
        assert after is not None and after["status"] == "received"
        assert after["allowed_actions"] == ["settle"]

        # 重复结算（不管订单是 received 还是 approved）-> 409 ALREADY_SETTLED
        await expect_error(
            parts_service.settle_order(order["id"], SettlementRequest(), trace_id=TRACE),
            status=409,
            code="ALREADY_SETTLED",
        )

        # (b) received 结算：金额可显式给，状态保持 received
        order2 = await create_order(item("SP-ETA-0220", 1))
        for action in (parts_service.submit_order, parts_service.approve_order, parts_service.receive_order):
            result = await action(order2["id"], OrderActionRequest(), trace_id=TRACE)
            assert result is not None
        record2 = await parts_service.settle_order(
            order2["id"], SettlementRequest(amount=99.5, method="对公转账"), trace_id=TRACE
        )
        assert record2["amount"] == 99.5 and record2["method"] == "对公转账"
        still = await parts_service.get_order(order2["id"], trace_id=TRACE)
        assert still is not None and still["status"] == "received"

        # 台账：按订单号过滤 + 过滤后金额合计（含分页外的记录）
        listed = await parts_service.list_settlements(
            order_no=order2["order_no"], limit=1, offset=0, trace_id=TRACE
        )
        assert SettlementListResponse.model_validate(listed).total == 1
        assert listed["total_amount"] == 99.5
        assert listed["items"][0]["order_id"] == order2["id"]
        all_records = await parts_service.list_settlements(
            order_no="PO-", limit=200, offset=0, trace_id=TRACE
        )
        mine = {r["order_no"] for r in all_records["items"]}
        assert {order["order_no"], order2["order_no"]} <= mine
        assert all_records["total_amount"] >= 99.5 + order["total_amount"]

    _run(scenario())


# --------------------------------------------------------------------------- #
# ⑤ 不存在与 allowed_actions
# --------------------------------------------------------------------------- #


def test_missing_order_returns_none_or_404() -> None:
    """订单不存在：get/action 返回 None（路由转 404），settle_order 直接 404。"""

    async def scenario() -> None:
        missing = 999_999_999
        assert await parts_service.get_order(missing, trace_id=TRACE) is None
        assert (
            await parts_service.submit_order(missing, OrderActionRequest(), trace_id=TRACE)
        ) is None
        assert (
            await parts_service.approve_order(missing, OrderActionRequest(), trace_id=TRACE)
        ) is None
        assert (
            await parts_service.receive_order(missing, OrderActionRequest(), trace_id=TRACE)
        ) is None
        assert (
            await parts_service.reject_order(
                missing, OrderActionRequest(reason="cancel: 不存在"), trace_id=TRACE
            )
        ) is None
        await expect_error(
            parts_service.settle_order(missing, SettlementRequest(), trace_id=TRACE),
            status=404,
            code="ORDER_NOT_FOUND",
        )
        # 备件不存在 -> None（路由转 404 PART_NOT_FOUND）
        assert await parts_service.get_part("SP-XXX-9999", trace_id=TRACE) is None
        # 台账里登记的替代件编码可以单独查到（字段来自替代条目本身）
        substitute = await parts_service.get_part("SP-ETA-0101F", trace_id=TRACE)
        assert substitute is not None
        assert substitute["code"] == "SP-ETA-0101F" and substitute["stock"] == 4
        assert substitute["part"].startswith("腔体门 O-ring")

    _run(scenario())


def test_allowed_actions_follow_status() -> None:
    """allowed_actions 随状态变化，前端据此渲染按钮。"""

    async def scenario() -> None:
        expected = {
            "draft": ["submit", "cancel"],
            "submitted": ["approve", "reject", "cancel"],
            "approved": ["receive"],
            "received": ["settle"],
            "rejected": [],
            "cancelled": [],
        }
        # 常量表本身
        for status, actions in expected.items():
            assert parts_service.allowed_actions(status) == actions
        assert parts_service.allowed_actions("不存在的状态") == []

        # 真实订单走一遍，逐步核对
        order = await create_order(item("SP-ETA-0101", 1))
        assert order["allowed_actions"] == expected["draft"]

        submitted = await parts_service.submit_order(order["id"], OrderActionRequest(), trace_id=TRACE)
        assert submitted is not None and submitted["allowed_actions"] == expected["submitted"]

        approved = await parts_service.approve_order(order["id"], OrderActionRequest(), trace_id=TRACE)
        assert approved is not None and approved["allowed_actions"] == expected["approved"]

        received = await parts_service.receive_order(order["id"], OrderActionRequest(), trace_id=TRACE)
        assert received is not None and received["allowed_actions"] == expected["received"]

        settled = await parts_service.settle_order(order["id"], SettlementRequest(), trace_id=TRACE)
        assert settled["order_no"] == order["order_no"]
        after_settle = await parts_service.get_order(order["id"], trace_id=TRACE)
        assert after_settle is not None and after_settle["allowed_actions"] == ["settle"]

        # 终态：驳回 / 取消都没有下一步
        rejected_order = await create_order(item("SP-ETA-0210", 1))
        await parts_service.submit_order(rejected_order["id"], OrderActionRequest(), trace_id=TRACE)
        rejected = await parts_service.reject_order(
            rejected_order["id"], OrderActionRequest(reason="预算未批"), trace_id=TRACE
        )
        assert rejected is not None and rejected["allowed_actions"] == []

        cancelled_order = await create_order(item("SP-ETA-0220", 1))
        cancelled = await parts_service.reject_order(
            cancelled_order["id"], OrderActionRequest(reason="cancel: 不买了"), trace_id=TRACE
        )
        assert cancelled is not None and cancelled["allowed_actions"] == []

        # 列表接口同样带 allowed_actions，且只过滤到本文件自己的单子
        listed = await parts_service.list_orders(
            status="received", applicant=APPLICANT, limit=20, offset=0, trace_id=TRACE
        )
        assert listed["limit"] == 20 and listed["offset"] == 0
        assert listed["items"], "本用例应当已产生 received 状态的订单"
        assert all(entry["applicant"] == APPLICANT for entry in listed["items"])
        assert all(entry["status"] == "received" for entry in listed["items"])
        assert all(entry["allowed_actions"] == ["settle"] for entry in listed["items"])
        assert order["id"] in [entry["id"] for entry in listed["items"]]
        # 按申请人过滤不会串到别的测试数据
        others = await parts_service.list_orders(
            status=None, applicant=f"{APPLICANT}-不存在", limit=20, offset=0, trace_id=TRACE
        )
        assert others["items"] == [] and others["total"] == 0

    _run(scenario())
