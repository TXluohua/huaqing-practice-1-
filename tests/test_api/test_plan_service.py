"""维护计划生成测试（开发文档 6.2 plan_service：周期抽取 / 剩余量计算 / 落库）。

覆盖：
    1. 生成的计划项**必须带 evidence + chunk_id**，且 note 里的周期原文能在引用切片里
       逐字找到（零幻觉：周期数值只能来自知识库原文）
    2. 知识库查不到周期的项进 `uncovered`，**不进 items**（含「每班次」这类只有频次
       没有数值的条款）
    3. `remaining` / `status` 计算：运行小时与片数口径各自的公式、超期 -> due、
       自然日口径 -> planned 且 due_at 有值
    4. `persist=False` 不写库、`persist=True` 写库且 `list_plans` 能查到（含排序/过滤/分页）
    5. `complete_plan` 滚动到下一周期、`skip_plan` 记原因、id 不存在返回 None
    6. 数据库不可用 -> 503 DB_UNAVAILABLE；知识库为空 -> 只出 uncovered，不编造周期
    7. 路由契约（路径 / 方法 / response_model / summary 齐备，且本模块不自带 prefix）

运行（与仓库其它测试一致）：
    .venv/bin/python -m pytest tests/test_api/test_plan_service.py -q

测试直接打 service 层（不依赖路由是否已注册到 api_router），用真实语料
（data/raw 已入库，索引在 backend/data/chroma）。写入数据库的行都带
`device_code=TEST-PLAN-<随机>`，只查自己的数据，不动别人的记录。
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Iterator

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
from sqlalchemy import select  # noqa: E402

from backend import db  # noqa: E402
from backend.db import MaintenancePlan  # noqa: E402
from backend.schemas import (  # noqa: E402
    PlanGenerateRequest,
    PlanUpdateRequest,
    ServiceError,
)
from backend.services import plan_service  # noqa: E402
from backend.tools.kb_tools import kb_get_chunk  # noqa: E402

#: 语料里的主力设备型号（data/raw/etch_maintenance_manual.md）
DEVICE = "Etcher-A"

#: 周期原文抽取（note 里写成「周期原文「…」」）
_RAW_RE = re.compile(r"周期原文「(.+?)」")


# --------------------------------------------------------------------------- #
# 公共工具
# --------------------------------------------------------------------------- #


def _code() -> str:
    """本次测试自己的设备编号标记（避免与别人写入的数据混淆）。"""

    return f"TEST-PLAN-{uuid.uuid4().hex[:8]}"


async def _with_db(scenario: Awaitable[Any]) -> Any:
    """每个场景自带一次 init_db / dispose_db（连接不跨事件循环复用）。

    必须与场景**在同一个事件循环内**：`db._engine` 是模块级全局缓存，
    若在 loop A 里 init、在 loop B 里用，MySQL（aiomysql）会报
    「Future attached to a different loop」；SQLite 的连接实现恰好容忍这种用法，
    所以这个错误只在 MySQL 下暴露（与 test_parts_service.py 同一范式）。
    """

    ok, detail = await db.init_db()
    assert ok, f"数据库不可用：{detail}"
    try:
        return await scenario
    finally:
        await db.dispose_db()


def _run_db(scenario: Awaitable[Any]) -> Any:
    """跑一个场景：与 init_db / dispose_db 同处一个事件循环。

    名字带 _db 后缀是为了不与本文件里的局部 `async def _run()`（查库辅助）
    重名 —— 否则替换后会解析到局部函数上。
    """

    return asyncio.run(_with_db(scenario))


@contextlib.contextmanager
def _patched(**replacements: Any) -> Iterator[None]:
    """临时替换模块属性（不引入 pytest 依赖），退出时还原。"""

    saved = {name: getattr(plan_service, name) for name in replacements}
    for name, value in replacements.items():
        setattr(plan_service, name, value)
    plan_service.reset_plan_cache()
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(plan_service, name, value)
        plan_service.reset_plan_cache()


def _generate(**overrides: Any) -> dict:
    """跑一次 generate_plans（默认预览不落库）。"""

    params: dict[str, Any] = {
        "device_model": DEVICE,
        "device_code": _code(),
        "runtime_hours": 1200.0,
        "wafer_count": 30,
        "top_k": 8,
        "persist": False,
    }
    params.update(overrides)
    payload = PlanGenerateRequest(**params)
    return _run_db(plan_service.generate_plans(payload, trace_id=f"test-{uuid.uuid4().hex[:6]}"))


def _count_rows(device_code: str) -> int:
    async def _run() -> int:
        async with db.session_scope() as session:
            rows = (
                await session.execute(
                    select(MaintenancePlan.id).where(MaintenancePlan.device_code == device_code)
                )
            ).scalars().all()
            return len(rows)

    return _run_db(_run())


def _rows(device_code: str) -> list[MaintenancePlan]:
    async def _run() -> list[MaintenancePlan]:
        async with db.session_scope() as session:
            return list(
                (
                    await session.execute(
                        select(MaintenancePlan)
                        .where(MaintenancePlan.device_code == device_code)
                        .order_by(MaintenancePlan.id.asc())
                    )
                ).scalars().all()
            )

    return _run_db(_run())


def _chunk_text(chunk_id: str) -> str:
    async def _run() -> str:
        blocks = await kb_get_chunk(chunk_id, neighbors=0)
        return "\n".join(block.text for block in blocks)

    return _run_db(_run())


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _item(items: list[dict], basis: str | None = None, name_contains: str = "") -> dict:
    for item in items:
        if basis and item["cycle_basis"] != basis:
            continue
        if name_contains and name_contains not in item["item_name"]:
            continue
        return item
    raise AssertionError(f"没有找到满足条件的计划项：basis={basis} name~{name_contains}")


def _list_all(device_model: str, status: str | None = None) -> dict:
    """按页取完该设备的全部计划（开发库里别的测试也写数据，所以必须翻页取全）。"""

    limit = 200
    offset = 0
    items: list[dict] = []
    total = 0
    for _ in range(50):  # 上限保护：最多 1 万条
        page = _run_db(
            plan_service.list_plans(
                device_model=device_model,
                status=status,
                limit=limit,
                offset=offset,
                trace_id=f"test-list-{offset}",
            )
        )
        total = page["total"]
        items.extend(page["items"])
        offset += limit
        if offset >= total:
            break
    return {"items": items, "total": total}


# --------------------------------------------------------------------------- #
# ① 依据绑定与 uncovered（零幻觉）
# --------------------------------------------------------------------------- #


def test_generate_items_carry_evidence_and_uncovered_lists_rest() -> None:
    """每项都带 evidence + chunk_id，且周期原文能在引用切片里找到；查不到的进 uncovered。"""

    result = _generate()

    items = result["items"]
    assert items, "真实语料下应当能生成计划项"
    assert len(items) <= 8, "item 数不应超过 top_k"
    assert result["device_model"] == DEVICE
    assert result["trace_id"]

    for item in items:
        assert item["evidence"], f"{item['item_name']} 缺少 evidence（零幻觉要求每条都有依据）"
        assert item["chunk_id"], f"{item['item_name']} 缺少 chunk_id"
        citation = item["evidence"][0]
        assert citation["doc"], "引用必须带文档名"
        assert citation["page"], "引用必须带页码"
        assert citation["section"], "引用必须带章节"
        assert citation["chunk_id"] == item["chunk_id"]
        assert item["cycle_basis"] in plan_service.CYCLE_BASES
        assert item["cycle_value"] > 0
        assert "由知识库生成，依据《" in item["note"]
        # 零幻觉：note 里引用的周期原文必须逐字出现在被引用的切片里
        raw = _RAW_RE.search(item["note"])
        assert raw, f"note 缺少周期原文：{item['note']}"
        assert raw.group(1) in _chunk_text(item["chunk_id"]), (
            f"{item['item_name']} 的周期原文「{raw.group(1)}」不在引用的切片里"
        )

    uncovered = result["uncovered"]
    assert uncovered, "应列出知识库里给不出依据的维护项"
    names = " | ".join(uncovered)
    # 语料里完全没有的项（尾气处理装置）必须如实标为「未收录」
    assert "尾气处理装置" in names
    assert "知识库未收录" in names
    # 只有执行频次（每班次）没有数值的项也不能编造周期
    assert "日常点检" in names
    assert "频次" in names
    # 已生成计划项的项不应同时出现在 uncovered 里
    for item in items:
        assert item["item_name"] not in uncovered


def test_generate_does_not_invent_cycle_for_frequency_only_clauses() -> None:
    """「每班次 / 每次开腔」这类只有频次的条款不得被折算成周期写进计划。"""

    result = _generate(top_k=20)
    for item in result["items"]:
        raw = _RAW_RE.search(item["note"])
        assert raw, "每项都必须写明周期原文"
        assert not raw.group(1).startswith("每次"), f"频次写法被当成周期：{item['note']}"
        assert "每班次" not in raw.group(1)


# --------------------------------------------------------------------------- #
# ② remaining / status 计算
# --------------------------------------------------------------------------- #


def test_remaining_and_due_at_follow_basis_rules() -> None:
    """运行小时按 24 h/天折算 due_at；片数口径留空；自然日口径按周期天数。"""

    now = datetime.now(timezone.utc)
    result = _generate(runtime_hours=0.0, wafer_count=0, top_k=20)

    hours_item = _item(result["items"], basis="hours", name_contains="排气过滤器")
    assert hours_item["remaining"] == hours_item["cycle_value"]  # 0 运行小时
    assert hours_item["current_value"] == 0.0
    assert hours_item["baseline_value"] == 0.0
    assert hours_item["status"] == "planned"
    expected = now + timedelta(days=hours_item["cycle_value"] / 24)
    assert abs((_parse(hours_item["due_at"]) - expected).total_seconds()) < 300

    wafers_item = _item(result["items"], basis="wafers")
    assert wafers_item["due_at"] is None, "片数口径不知道产线速率，due_at 必须留空"

    days_item = _item(result["items"], basis="days")
    assert days_item["remaining"] == days_item["cycle_value"]
    assert days_item["status"] == "planned"
    assert abs((_parse(days_item["due_at"]) - (now + timedelta(days=days_item["cycle_value"]))).total_seconds()) < 300


def test_remaining_uses_hours_since_pm_and_baseline() -> None:
    """给了 hours_since_pm 就按「距上次 PM」算，基线回推。"""

    result = _generate(runtime_hours=5000.0, hours_since_pm=100.0, top_k=20)
    item = _item(result["items"], basis="hours", name_contains="排气过滤器")
    assert item["remaining"] == item["cycle_value"] - 100.0
    assert item["current_value"] == 5000.0
    assert item["baseline_value"] == 5000.0 - 100.0
    assert item["status"] == "planned"


def test_overdue_items_become_due() -> None:
    """remaining <= 0 -> status=due；超期的运行小时项 due_at 取当前时间。"""

    now = datetime.now(timezone.utc)
    result = _generate(runtime_hours=100000.0, wafer_count=100000, top_k=20)

    overdue = [item for item in result["items"] if item["cycle_basis"] in ("hours", "wafers")]
    assert overdue
    for item in overdue:
        assert item["remaining"] < 0
        assert item["status"] == "due"
        if item["cycle_basis"] == "hours":
            assert abs((_parse(item["due_at"]) - now).total_seconds()) < 300

    for item in result["items"]:
        if item["cycle_basis"] == "days":
            assert item["status"] == "planned"

    check = _item(result["items"], basis="hours", name_contains="排气过滤器")
    assert check["remaining"] == check["cycle_value"] - 100000.0


# --------------------------------------------------------------------------- #
# ③ persist 与 list_plans
# --------------------------------------------------------------------------- #


def test_persist_flag_controls_database_write() -> None:
    """persist=False 只预览；persist=True 落库且 list_plans 查得到（按 remaining 升序）。"""

    code = _code()

    preview = _generate(device_code=code, persist=False, top_k=8)
    assert preview["items"]
    assert all(item["id"] is None for item in preview["items"]), "预览不应有 id"
    assert _count_rows(code) == 0, "persist=False 不许写库"

    saved = _generate(device_code=code, persist=True, top_k=8)
    assert _count_rows(code) == len(saved["items"])
    assert all(item["id"] for item in saved["items"]), "落库后应回填 id"
    stored = _rows(code)
    assert all(row.evidence and row.chunk_id for row in stored), "落库的计划项必须带依据"

    listed = _list_all(DEVICE, status=None)
    mine = [item for item in listed["items"] if item["device_code"] == code]
    assert len(mine) == len(saved["items"])
    assert listed["total"] >= len(mine)

    remaining = [item["remaining"] for item in listed["items"]]
    assert remaining == sorted(remaining), "列表必须按 remaining 升序（最紧急在前）"

    first_page = _run_db(
        plan_service.list_plans(
            device_model=DEVICE, status=None, limit=5, offset=0, trace_id="test-page"
        )
    )
    assert first_page["limit"] == 5 and first_page["offset"] == 0
    assert first_page["trace_id"] == "test-page"
    assert len(first_page["items"]) == 5
    assert first_page["items"] == listed["items"][:5], "第 1 页应与全量结果的前 5 条一致"


def test_list_plans_filters_and_paginates() -> None:
    """status 过滤、limit/offset 分页与 total 一致。"""

    code = _code()
    _generate(device_code=code, persist=True, runtime_hours=100000.0, wafer_count=100000, top_k=8)

    page1 = _run_db(
        plan_service.list_plans(
            device_model=DEVICE, status=None, limit=2, offset=0, trace_id="t1"
        )
    )
    page2 = _run_db(
        plan_service.list_plans(
            device_model=DEVICE, status=None, limit=2, offset=2, trace_id="t2"
        )
    )
    assert len(page1["items"]) == 2 and len(page2["items"]) == 2
    assert page1["total"] == page2["total"]
    first_ids = {item["id"] for item in page1["items"]}
    second_ids = {item["id"] for item in page2["items"]}
    assert not (first_ids & second_ids), "分页不应重复返回同一条"

    due = _run_db(
        plan_service.list_plans(
            device_model=DEVICE, status="due", limit=200, offset=0, trace_id="t3"
        )
    )
    assert all(item["status"] == "due" for item in due["items"])
    assert all(item["remaining"] <= 0 for item in due["items"])
    assert any(item["device_code"] == code for item in due["items"]), (
        "超期设备应能按 status=due 过滤出来"
    )

    with pytest.raises(ServiceError) as excinfo:
        _run_db(
            plan_service.list_plans(
                device_model=DEVICE, status="不存在的状态", limit=20, offset=0, trace_id="t4"
            )
        )
    assert excinfo.value.status_code == 400
    assert excinfo.value.code == "INVALID_ARGUMENT"


# --------------------------------------------------------------------------- #
# ④ 完成 / 跳过
# --------------------------------------------------------------------------- #


def test_complete_plan_rolls_to_next_cycle() -> None:
    """完成 -> status=done、remaining 回到 cycle_value、due_at 重算；id 不存在返回 None。"""

    code = _code()
    result = _generate(device_code=code, persist=True, runtime_hours=1200.0, top_k=20)
    hours_item = _item(result["items"], basis="hours")

    done = _run_db(
        plan_service.complete_plan(
            hours_item["id"],
            PlanUpdateRequest(note="已按手册执行", completed_value=1200.0),
            trace_id="test-complete",
        )
    )
    assert done is not None
    assert done["id"] == hours_item["id"]
    assert done["status"] == "done"
    assert done["remaining"] == done["cycle_value"], "完成后应进入下一个周期"
    assert done["baseline_value"] == 1200.0, "completed_value 应作为新基线"
    assert done["current_value"] == 1200.0
    assert done["due_at"] is not None
    expected = datetime.now(timezone.utc) + timedelta(days=done["cycle_value"] / 24)
    assert abs((_parse(done["due_at"]) - expected).total_seconds()) < 300
    assert "已滚动到下一周期" in done["note"]
    assert "已按手册执行" in done["note"]
    assert "周期原文" in done["note"], "完成操作不应丢掉原有溯源信息"

    stored = [row for row in _rows(code) if row.id == hours_item["id"]][0]
    assert stored.completed_at is not None
    assert stored.status == "done"

    missing = _run_db(
        plan_service.complete_plan(10_000_000, PlanUpdateRequest(), trace_id="test-complete")
    )
    assert missing is None, "id 不存在必须返回 None（路由翻译成 404）"


def test_complete_wafers_item_keeps_due_at_empty() -> None:
    """片数口径没有产线速率：完成后 remaining 回滚，但 due_at 仍然留空（不猜）。"""

    code = _code()
    result = _generate(device_code=code, persist=True, top_k=20)
    wafers_item = _item(result["items"], basis="wafers")

    done = _run_db(
        plan_service.complete_plan(
            wafers_item["id"], PlanUpdateRequest(completed_value=0.0), trace_id="t"
        )
    )
    assert done is not None
    assert done["status"] == "done"
    assert done["remaining"] == done["cycle_value"]
    assert done["due_at"] is None


def test_skip_plan_records_reason() -> None:
    """跳过 -> status=skipped，note 追加原因；id 不存在返回 None。"""

    code = _code()
    result = _generate(device_code=code, persist=True, top_k=8)
    item = result["items"][0]

    skipped = _run_db(
        plan_service.skip_plan(item["id"], PlanUpdateRequest(note="备件未到货"), trace_id="test-skip")
    )
    assert skipped is not None
    assert skipped["status"] == "skipped"
    assert "备件未到货" in skipped["note"]
    assert skipped["remaining"] == item["remaining"], "跳过不改变周期与剩余量"

    missing = _run_db(
        plan_service.skip_plan(10_000_001, PlanUpdateRequest(note="x"), trace_id="test-skip")
    )
    assert missing is None


# --------------------------------------------------------------------------- #
# ⑤ 降级：数据库不可用 / 知识库为空
# --------------------------------------------------------------------------- #


def test_db_unavailable_raises_503() -> None:
    """数据库不可用一律 503 DB_UNAVAILABLE，而不是 500。"""

    saved = db._available
    db._available = False
    try:
        calls = [
            lambda: plan_service.generate_plans(
                PlanGenerateRequest(device_model=DEVICE, persist=False), trace_id="t"
            ),
            lambda: plan_service.list_plans(
                device_model=DEVICE, status=None, limit=20, offset=0, trace_id="t"
            ),
            lambda: plan_service.complete_plan(1, PlanUpdateRequest(), trace_id="t"),
            lambda: plan_service.skip_plan(1, PlanUpdateRequest(), trace_id="t"),
        ]
        for call in calls:
            with pytest.raises(ServiceError) as excinfo:
                # 不走 _run_db：它会先 init_db()，而 init_db() 会把 _available
                # 重新置为 True，正好抵消本用例模拟的「库不可用」。
                # 服务层在 _require_db() 里于任何取连接之前就抛 503，故无需真实引擎。
                asyncio.run(call())
            assert excinfo.value.status_code == 503
            assert excinfo.value.code == "DB_UNAVAILABLE"
    finally:
        db._available = saved


def test_empty_knowledge_base_yields_uncovered_without_inventing() -> None:
    """知识库为空：items 为空、uncovered 列出全部候选项，绝不编造周期。"""

    async def _empty(*_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    async def _empty_corpus(**_kwargs: Any) -> str:
        return ""

    with _patched(kb_search=_empty, kb_corpus_text=_empty_corpus):
        result = _generate(persist=False)

    assert result["items"] == [], "查不到依据时不许生成计划项"
    assert result["uncovered"], "必须如实列出查不到依据的维护项"
    assert any("未命中" in line or "未收录" in line for line in result["uncovered"])


def test_kb_failure_raises_503() -> None:
    """检索整体失败时给 503 KB_UNAVAILABLE（明确报错，而不是默默返回空计划）。"""

    async def _boom(*_args: Any, **_kwargs: Any) -> list[Any]:
        raise RuntimeError("索引损坏")

    with _patched(kb_search=_boom):
        with pytest.raises(ServiceError) as excinfo:
            _run_db(
                plan_service.generate_plans(
                    PlanGenerateRequest(device_model=DEVICE, persist=False), trace_id="t"
                )
            )
    assert excinfo.value.status_code == 503
    assert excinfo.value.code == "KB_UNAVAILABLE"


# --------------------------------------------------------------------------- #
# ⑥ 路由契约（不需要 HTTP 服务，也不依赖是否已注册到 api_router）
# --------------------------------------------------------------------------- #


def test_router_declares_contract() -> None:
    """4 个端点齐备：路径 / 方法 / response_model / summary，且本模块不自带 prefix。"""

    from backend.routers import plans as plans_router

    router = plans_router.router
    assert router.prefix == "", "路径前缀 /plans 由注册方带，本模块不自带 prefix"

    routes = {(route.path, tuple(sorted(route.methods))) for route in router.routes}
    assert ("/plans/generate", ("POST",)) in routes
    assert ("/plans", ("GET",)) in routes
    assert ("/plans/{plan_id}/complete", ("POST",)) in routes
    assert ("/plans/{plan_id}/skip", ("POST",)) in routes

    expected_models = {
        "/plans/generate": "PlanGenerateResponse",
        "/plans": "PlanListResponse",
        "/plans/{plan_id}/complete": "PlanItem",
        "/plans/{plan_id}/skip": "PlanItem",
    }
    for route in router.routes:
        assert route.summary, f"{route.path} 缺 summary（要出现在 OpenAPI 里）"
        assert route.response_model is not None, f"{route.path} 缺 response_model"
        assert route.response_model.__name__ == expected_models[route.path]
