"""会话与问答服务（开发文档 6.2 的 chat_service.py：会话管理 + 调图 + 包 SSE 响应）。

冻结契约（协作规范 §3.3，路由层按调用点依赖，**签名不要改**）
--------------------------------------------------------------
    stream_answer(payload, *, user_id, user_role, trace_id) -> AsyncIterator[str]
    list_sessions(*, limit, offset, keyword) -> tuple[list[SessionItem], int]
    list_messages(session_id, *, limit, offset, with_citations)
        -> tuple[SessionRef, list[MessageItem], int] | None
    add_feedback(*, qa_id, type, comment, corrected_answer, trace_id) -> FeedbackRecord | None

stream_answer 产出的是**已格式化好的 SSE 文本块**（开发文档 6.2 明确 SSE 封装属 service 层）。
事件顺序照接口文档 §6：
    meta -> image? -> token* -> citations -> done，失败时发 error。

落库失败不影响回答：业务库只是旁路，主链路必须照常返回（对齐开发文档 R7）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from sqlalchemy import func, select

from .. import db
from ..agents.agent_factory import astream
from ..agents.state import Citation
from ..memory import session_registry
from ..schemas import (
    ChatRequest,
    CitationsEvent,
    DoneEvent,
    ErrorEvent,
    ImageEvent,
    MessageItem,
    MetaEvent,
    ServiceError,
    SessionItem,
    TokenEvent,
)
from ..setting import get_settings
from ..utils.text import make_title, mask_secrets, now_iso, to_iso

logger = logging.getLogger(__name__)

#: token 事件策略
#: ---------------------------------------------------------------------------
#: True  = 等 verify 定稿后一次性发（**当前实现**）
#: False = 边生成边发增量
#:
#: 为什么默认 True：verify 在拒答时会**改写 answer**（把含操作步骤的答案换成
#: 「知识库未覆盖」），若已经流式发出去，用户会先看到一段操作步骤、再看到拒答，
#: 这是零幻觉底线不允许的。
#:
#: 代价几乎没有：generate 节点里的 _call_llm 用的是 stream=False，模型本来就不是
#: 逐 token 返回，因此「缓冲」并不损失真实流式体验。
#: 若将来改用流式 LLM，需按协作规范 §7 与乙 + 第三人拍板后再切到 False。
BUFFER_TOKENS_UNTIL_VERIFY = True

#: 多轮上下文取最近几轮问答（FR-06 多轮追问）
HISTORY_TURNS = 6


def sse(event: str, payload: dict[str, Any]) -> str:
    """按 SSE 规范格式化一个事件（事件之间以空行分隔）。"""

    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@dataclass(slots=True)
class SessionRef:
    """会话引用（路由层会读 thread_id）。"""

    session_id: str
    thread_id: str
    title: str = ""


@dataclass(slots=True)
class FeedbackRecord:
    """反馈落库结果。"""

    feedback_id: int
    created_at: str


# --------------------------------------------------------------------------- #
# 会话
# --------------------------------------------------------------------------- #


async def _ensure_session(session_id: str | None, *, question: str) -> SessionRef:
    """取会话；缺省则新建。数据库不可用时退化为临时会话（不落库）。"""

    if not db.is_available():
        resolved = session_id or session_registry.new_session_id()
        return SessionRef(resolved, session_registry.thread_id(resolved), make_title(question))

    async with db.session_scope() as session:
        if session_id:
            row = await session.get(db.SessionMap, session_id)
            if row is None:
                raise ServiceError(404, "SESSION_NOT_FOUND", f"会话不存在：{session_id}")
            return SessionRef(row.session_id, row.thread_id, row.title or "")

        resolved = session_registry.new_session_id()
        thread_id = session_registry.thread_id(resolved)
        title = make_title(question)
        session.add(db.SessionMap(session_id=resolved, thread_id=thread_id, title=title))
        return SessionRef(resolved, thread_id, title)


async def _load_history(session_id: str, *, turns: int = HISTORY_TURNS) -> list[dict[str, Any]]:
    """取最近几轮问答作为多轮上下文（FR-06）；失败时返回空列表，不阻断回答。"""

    if not db.is_available():
        return []
    try:
        async with db.session_scope() as session:
            stmt = (
                select(db.QaRecord)
                .where(db.QaRecord.session_id == session_id)
                .order_by(db.QaRecord.id.desc())
                .limit(turns)
            )
            records = list((await session.execute(stmt)).scalars())
        history: list[dict[str, Any]] = []
        for record in reversed(records):
            history.append({"role": "user", "content": record.question})
            if record.answer:
                history.append({"role": "assistant", "content": record.answer})
        return history
    except Exception as exc:  # noqa: BLE001 - 历史只是增强，拿不到也要能答
        logger.warning("读取多轮上下文失败（忽略）：%s", exc)
        return []


# --------------------------------------------------------------------------- #
# 问答（SSE）
# --------------------------------------------------------------------------- #


async def stream_answer(
    payload: ChatRequest,
    *,
    user_id: str,
    user_role: str,
    trace_id: str,
) -> AsyncIterator[str]:
    """跑一次问答并产出 SSE 文本块。"""

    started = time.perf_counter()

    # ---- 会话（此处的失败只能用 error 事件表达：响应头早已发出）----
    try:
        ref = await _ensure_session(payload.session_id, question=payload.question)
    except ServiceError as exc:
        yield sse("error", ErrorEvent(code=exc.code, message=exc.message).model_dump())
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("会话初始化失败")
        yield sse(
            "error",
            ErrorEvent(code="INTERNAL_ERROR", message=mask_secrets(str(exc))[:300]).model_dump(),
        )
        return

    yield sse(
        "meta",
        MetaEvent(session_id=ref.session_id, trace_id=trace_id, ts=now_iso()).model_dump(),
    )

    history = await _load_history(ref.session_id)
    latest_answer = ""
    final: dict[str, Any] = {}
    image_sent = False
    client_gone = False

    try:
        async for chunk in astream(
            payload.question,
            session_id=ref.session_id,
            image_ids=payload.image_ids,
            history=history,
            thread_id=ref.thread_id,
            trace_id=trace_id,
            device_model=payload.device_model,
            category=payload.category,
            answer_mode=payload.mode,
            user_role=user_role,
        ):
            if not isinstance(chunk, dict):
                continue
            final = chunk

            # image 事件尽早发（图片识别结果先于正文）
            if not image_sent:
                extraction = chunk.get("image_result")
                if extraction is not None:
                    image_sent = True
                    yield sse(
                        "image",
                        ImageEvent(
                            image_id=str(getattr(extraction, "image_id", "") or ""),
                            image_type=str(getattr(extraction, "image_type", "unknown") or "unknown"),
                            extracted=dict(getattr(extraction, "extracted", {}) or {}),
                            confidence=float(getattr(extraction, "confidence", 0.0) or 0.0),
                        ).model_dump(),
                    )

            answer = str(chunk.get("answer") or "")
            if not BUFFER_TOKENS_UNTIL_VERIFY and len(answer) > len(latest_answer):
                delta = answer[len(latest_answer) :]
                if delta:
                    yield sse("token", TokenEvent(delta=delta).model_dump())
            latest_answer = answer
    except asyncio.CancelledError:
        # 客户端主动断开：不是错误，但要落库已产出的部分
        client_gone = True
        logger.info("客户端断开 trace_id=%s session=%s", trace_id, ref.session_id)
    except Exception as exc:  # noqa: BLE001 - 链路异常必须转成 error 事件
        logger.exception("问答链路异常 trace_id=%s", trace_id)
        yield sse(
            "error",
            ErrorEvent(code="INTERNAL_ERROR", message=mask_secrets(str(exc))[:300]).model_dump(),
        )
        return

    # ---- 定稿输出 ----
    if not client_gone:
        if BUFFER_TOKENS_UNTIL_VERIFY and latest_answer:
            yield sse("token", TokenEvent(delta=latest_answer).model_dump())

        citations = list(final.get("citations") or [])
        yield sse("citations", CitationsEvent(items=citations).model_dump())

        latency_ms = int((time.perf_counter() - started) * 1000)
        qa_id = await _persist_qa(ref, payload, final, trace_id=trace_id, latency_ms=latency_ms)

        done_payload = DoneEvent(
            status=final.get("status") or "NOT_COVERED",
            confidence=float(final.get("confidence") or 0.0),
            label=final.get("confidence_label") or "low",
            uncertain=list(final.get("uncertain") or []),
        ).model_dump()
        if qa_id is not None:
            # 契约**追加**字段（接口文档 §8 缺口③）：done 原本没有 qa_id，
            # 导致问答页拿不到 qa_id、反馈按钮只能置灰。这里追加一个字段，
            # 未读取它的客户端行为与升级前完全一致（向后兼容）。
            done_payload["qa_id"] = qa_id
        yield sse("done", done_payload)
    else:
        latency_ms = int((time.perf_counter() - started) * 1000)
        await _persist_qa(ref, payload, final, trace_id=trace_id, latency_ms=latency_ms)


# --------------------------------------------------------------------------- #
# 落库
# --------------------------------------------------------------------------- #


async def _persist_qa(
    ref: SessionRef,
    payload: ChatRequest,
    state: dict[str, Any],
    *,
    trace_id: str,
    latency_ms: int,
) -> int | None:
    """写 qa_record 并刷新会话时间；失败只记日志，不影响已发出的回答。"""

    if not db.is_available():
        return None
    try:
        citations = [
            item.model_dump() if hasattr(item, "model_dump") else dict(item)
            for item in (state.get("citations") or [])
        ]
        async with db.session_scope() as session:
            record = db.QaRecord(
                session_id=ref.session_id,
                trace_id=trace_id,
                question=payload.question,
                answer=str(state.get("answer") or ""),
                citations=citations,
                confidence=float(state.get("confidence") or 0.0),
                confidence_label=str(state.get("confidence_label") or "low"),
                status=str(state.get("status") or "NOT_COVERED"),
                uncertain=list(state.get("uncertain") or []),
                latency_ms=latency_ms,
                image_ids=list(payload.image_ids or []),
                answer_mode=payload.mode,
            )
            session.add(record)
            await session.flush()
            qa_id = int(record.id)

            row = await session.get(db.SessionMap, ref.session_id)
            if row is not None:
                row.updated_at = datetime.now(timezone.utc)  # 会话列表按此排序
                session.add(row)
        return qa_id
    except Exception as exc:  # noqa: BLE001
        logger.warning("问答落库失败（不影响回答）：%s", exc)
        return None


# --------------------------------------------------------------------------- #
# 会话列表与历史
# --------------------------------------------------------------------------- #


def _require_db() -> None:
    if not db.is_available():
        raise ServiceError(503, "DATABASE_UNAVAILABLE", "数据库不可用，请检查 setting.database_url")


async def list_sessions(
    *,
    limit: int,
    offset: int,
    keyword: str | None = None,
) -> tuple[list[SessionItem], int]:
    """会话列表（FR-06）。"""

    _require_db()

    counts = (
        select(db.QaRecord.session_id, func.count(db.QaRecord.id).label("cnt"))
        .group_by(db.QaRecord.session_id)
        .subquery()
    )
    last_ids = (
        select(func.max(db.QaRecord.id)).group_by(db.QaRecord.session_id).scalar_subquery()
    )
    last_q = (
        select(db.QaRecord.session_id, db.QaRecord.question)
        .where(db.QaRecord.id.in_(last_ids))
        .subquery()
    )

    stmt = (
        select(db.SessionMap, func.coalesce(counts.c.cnt, 0), last_q.c.question)
        .outerjoin(counts, counts.c.session_id == db.SessionMap.session_id)
        .outerjoin(last_q, last_q.c.session_id == db.SessionMap.session_id)
        .order_by(db.SessionMap.updated_at.desc())
    )
    total_stmt = select(func.count()).select_from(db.SessionMap)
    if keyword:
        pattern = f"%{keyword}%"
        stmt = stmt.where(db.SessionMap.title.like(pattern))
        total_stmt = total_stmt.where(db.SessionMap.title.like(pattern))

    async with db.session_scope() as session:
        rows = (await session.execute(stmt.limit(limit).offset(offset))).all()
        total = int((await session.execute(total_stmt)).scalar() or 0)

    items = [
        SessionItem(
            session_id=row[0].session_id,
            thread_id=row[0].thread_id,
            title=row[0].title or "",
            message_count=int(row[1] or 0) * 2,  # 一问一答视为两条消息
            last_question=row[2],
            updated_at=to_iso(row[0].updated_at),
        )
        for row in rows
    ]
    return items, total


async def list_messages(
    session_id: str,
    *,
    limit: int,
    offset: int,
    with_citations: bool = True,
) -> tuple[SessionRef, list[MessageItem], int] | None:
    """历史消息（FR-04 / FR-06）。分页作用于问答记录，total 为记录数的两倍。"""

    _require_db()

    async with db.session_scope() as session:
        row = await session.get(db.SessionMap, session_id)
        if row is None:
            return None
        ref = SessionRef(row.session_id, row.thread_id, row.title or "")

        stmt = (
            select(db.QaRecord)
            .where(db.QaRecord.session_id == session_id)
            .order_by(db.QaRecord.id.asc())
            .limit(limit)
            .offset(offset)
        )
        records = list((await session.execute(stmt)).scalars())
        total_records = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(db.QaRecord)
                    .where(db.QaRecord.session_id == session_id)
                )
            ).scalar()
            or 0
        )

    items: list[MessageItem] = []
    for record in records:
        created = to_iso(record.created_at)
        items.append(
            MessageItem(
                qa_id=int(record.id),
                role="user",
                content=record.question,
                image_ids=list(record.image_ids or []),
                created_at=created,
            )
        )
        citations: list[Citation] = []
        if with_citations:
            for raw in record.citations or []:
                try:
                    citations.append(Citation.model_validate(raw))
                except Exception:  # noqa: BLE001 - 历史脏数据不该让整页 500
                    logger.debug("跳过无法解析的历史引用：%r", raw)
        items.append(
            MessageItem(
                qa_id=int(record.id),
                role="assistant",
                content=record.answer or "",
                citations=citations,
                confidence=float(record.confidence or 0.0),
                confidence_label=record.confidence_label or "low",  # type: ignore[arg-type]
                status=record.status or "NOT_COVERED",  # type: ignore[arg-type]
                latency_ms=int(record.latency_ms or 0),
                created_at=created,
            )
        )
    return ref, items, total_records * 2


# --------------------------------------------------------------------------- #
# 反馈
# --------------------------------------------------------------------------- #


def _append_badcase(
    *,
    qa_id: int,
    question: str,
    answer: str,
    status: str,
    comment: str | None,
    trace_id: str,
) -> None:
    """把驳回案例追加到 badcases.md（FR-07）；写文件失败只记日志。"""

    settings = get_settings()
    path = Path(settings.badcases_path)
    block = (
        f"\n## qa_id={qa_id}（{now_iso()}）\n\n"
        f"- trace_id: {trace_id}\n"
        f"- 状态: {status}\n"
        f"- 问题: {question}\n"
        f"- 驳回备注: {comment or '（无）'}\n"
        f"- 原回答: {answer[:300]}\n"
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(block)
    except OSError as exc:
        logger.warning("badcase 记录失败（不影响反馈落库）：%s", exc)


async def add_feedback(
    *,
    qa_id: int,
    type: str,  # noqa: A002 - 与接口契约字段同名
    comment: str | None,
    corrected_answer: str | None,
    trace_id: str,
) -> FeedbackRecord | None:
    """四态反馈落库（FR-07）。返回 None 表示 qa_id 不存在。"""

    _require_db()

    async with db.session_scope() as session:
        record = await session.get(db.QaRecord, qa_id)
        if record is None:
            return None
        feedback = db.QaFeedback(
            qa_id=qa_id,
            feedback_type=type,
            comment=comment,
            corrected_answer=corrected_answer,
        )
        session.add(feedback)
        await session.flush()
        result = FeedbackRecord(feedback_id=int(feedback.id), created_at=to_iso(feedback.created_at))
        snapshot = (record.question, record.answer or "", record.status or "")

    if type == "reject":
        _append_badcase(
            qa_id=qa_id,
            question=snapshot[0],
            answer=snapshot[1],
            status=snapshot[2],
            comment=comment,
            trace_id=trace_id,
        )
    return result


__all__ = [
    "FeedbackRecord",
    "SessionRef",
    "add_feedback",
    "list_messages",
    "list_sessions",
    "sse",
    "stream_answer",
]
