"""数据表模型与数据库会话（开发文档 5.6 数据库设计 / 6.2 db.py）。

表
--
    kb_document   文档与版本
    kb_image      文档内抽出的图片与 caption
    qa_record     问答记录（含引用、置信度、状态、耗时）
    qa_feedback   四态反馈
    session_map   会话与 thread_id 映射

注意：**会话检查点不在这里** —— 那由 backend/memory.py 的 SqliteSaver 负责，
本模块只管业务数据（开发文档 5.6 的分工）。

数据库
------
开发期默认 SQLite（零依赖即可跑通全部接口）；生产按 §5.3 换 MySQL，
只需改 setting.database_url —— 模型与查询代码一行都不用动。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, select, text
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON, TypeDecorator

from .setting import get_settings

logger = logging.getLogger(__name__)


class UTCDateTime(TypeDecorator):
    """带时区的 DateTime：SQLite 存 naive UTC，读出时补回 UTC。

    SQLite 不保存时区，直接用 DateTime(timezone=True) 会在读回时丢 tzinfo，
    导致接口层输出带不带时区不一致。这里统一处理。
    """

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:  # noqa: ANN401
        return dialect.type_descriptor(DateTime(timezone=False))

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:  # noqa: ANN401
        if value is None:
            return None
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:  # noqa: ANN401
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """所有表的基类。"""


#: 长文本用 LONGTEXT（MySQL）/ TEXT（SQLite）
LongText = Text().with_variant(mysql.LONGTEXT(), "mysql")
#: JSON 列：MySQL 用原生 JSON，SQLite 用 TEXT 存储
JSONColumn = JSON().with_variant(sqlite.JSON(), "sqlite")


class SessionMap(Base):
    """session_id <-> thread_id 映射（开发文档 5.6）。"""

    __tablename__ = "session_map"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, onupdate=_utcnow)


class QaRecord(Base):
    """一次问答（一问一答视为一条记录）—— FR-04 / FR-06 / NFR-06。"""

    __tablename__ = "qa_record"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    trace_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    question: Mapped[str] = mapped_column(LongText)
    answer: Mapped[str] = mapped_column(LongText, default="")
    #: 引用清单，元素字段与 state.Citation 一致
    citations: Mapped[list[Any]] = mapped_column(JSONColumn, default=list)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    confidence_label: Mapped[str] = mapped_column(String(16), default="low")
    status: Mapped[str] = mapped_column(String(16), default="NOT_COVERED")
    #: 存疑点（含 respond 追加的安全前置提示）
    uncertain: Mapped[list[Any]] = mapped_column(JSONColumn, default=list)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    image_ids: Mapped[list[Any]] = mapped_column(JSONColumn, default=list)
    answer_mode: Mapped[str] = mapped_column(String(16), default="qa")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, index=True)


class QaFeedback(Base):
    """四态反馈（FR-07）。"""

    __tablename__ = "qa_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    qa_id: Mapped[int] = mapped_column(Integer, index=True)
    #: 列名保持接口契约的 type，属性名避开内置 type
    feedback_type: Mapped[str] = mapped_column("type", String(16))
    comment: Mapped[str | None] = mapped_column(LongText, nullable=True)
    corrected_answer: Mapped[str | None] = mapped_column(LongText, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, index=True)


class QuestionStat(Base):
    """高频问题统计（FR-09 本期交付：高频问题记录）。

    用途：反推培训内容与知识补充方向；管理页展示「被问得最多的 N 个问题」。
    以**归一化后的问题**为主键（去空白标点、转小写、截断），
    同一问题的不同标点/空格写法会归并到同一条。
    """

    __tablename__ = "question_stat"

    question_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    #: 保留一条原始问法，便于展示
    sample_question: Mapped[str] = mapped_column(LongText)
    ask_count: Mapped[int] = mapped_column(Integer, default=1, index=True)
    first_asked_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow)
    last_asked_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, index=True)


class KbDocument(Base):
    """知识库文档与版本（FR-08）。"""

    __tablename__ = "kb_document"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_title: Mapped[str] = mapped_column(String(255), index=True)
    source_path: Mapped[str] = mapped_column(String(512), default="")
    category: Mapped[str] = mapped_column(String(64), default="")
    version: Mapped[str] = mapped_column(String(64), default="", index=True)
    device_model: Mapped[str] = mapped_column(String(64), default="")
    uploader: Mapped[str] = mapped_column(String(64), default="")
    #: processing / ready / failed
    status: Mapped[str] = mapped_column(String(16), default="processing", index=True)
    n_chunks: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(LongText, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, onupdate=_utcnow)


class KbImage(Base):
    """文档内抽出的图片（图片知识侧，开发文档 5.6）。"""

    __tablename__ = "kb_image"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[int] = mapped_column(Integer, index=True)
    page: Mapped[int] = mapped_column(Integer, default=0)
    section: Mapped[str] = mapped_column(String(128), default="")
    image_path: Mapped[str] = mapped_column(String(512), default="")
    caption: Mapped[str] = mapped_column(LongText, default="")
    extracted_text: Mapped[str] = mapped_column(LongText, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow)


# --------------------------------------------------------------------------- #
# 引擎与会话
# --------------------------------------------------------------------------- #

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
#: 数据库是否可用（不可用时接口返回 503，而不是让应用起不来）
_available = False
_detail = "未初始化"


def get_engine() -> AsyncEngine:
    """取（必要时创建）异步引擎。"""

    global _engine
    if _engine is None:
        settings = get_settings()
        url = settings.database_url
        kwargs: dict[str, Any] = {"echo": settings.db_echo, "future": True}
        if url.startswith("sqlite"):
            # SQLite 需要允许跨线程（FastAPI 在线程池里跑同步片段）
            kwargs["connect_args"] = {"check_same_thread": False}
        else:
            kwargs["pool_pre_ping"] = True
        _engine = create_async_engine(url, **kwargs)
        logger.info("数据库引擎已创建：%s", url.split("@")[-1] if "@" in url else url)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """取（必要时创建）会话工厂。"""

    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(), expire_on_commit=False, class_=AsyncSession
        )
    return _session_factory


# --------------------------------------------------------------------------- #
# 新增业务表（2026-09-20 追加，只追加不改既有表）
#
# 覆盖三块新业务：
#   ① 维护计划生成   maintenance_plan
#   ② 备件商城与采购 part_order / part_settlement
#   ③ 考核认证       training_quiz / training_attempt / certification
#
# 零幻觉约束同样适用：计划项与考题都必须带 evidence（引用手册章节/切片），
# 没有依据的条目不允许入库。
# --------------------------------------------------------------------------- #


class MaintenancePlan(Base):
    """维护计划项（①维护计划生成）。

    `cycle_basis` 决定用哪种口径推算到期：运行小时 / 生产片数 / 自然日 / 每次开腔。
    计划依据（`evidence`）保存引用的手册章节，前端可直接展示「这条计划出自哪一页」。
    """

    __tablename__ = "maintenance_plan"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_model: Mapped[str] = mapped_column(String(64), index=True)
    device_code: Mapped[str] = mapped_column(String(64), default="", index=True)
    item_name: Mapped[str] = mapped_column(String(255))
    #: PM 项 / 耗材更换 / 校准 / 安全检查 / 清洗
    item_type: Mapped[str] = mapped_column(String(32), default="PM 项")
    #: hours（运行小时）/ wafers（生产片数）/ days（自然日）/ open（每次开腔）
    cycle_basis: Mapped[str] = mapped_column(String(16), default="hours")
    cycle_value: Mapped[float] = mapped_column(Float, default=0.0)
    #: 上次维护时的计量基线（运行小时或片数；自然日口径用完成时间）
    baseline_value: Mapped[float] = mapped_column(Float, default=0.0)
    #: 当前计量值（生成时由调用方传入；「按天」口径可为 0）
    current_value: Mapped[float] = mapped_column(Float, default=0.0)
    #: 距离到期还差多少计量单位（负数表示已超期）
    remaining: Mapped[float] = mapped_column(Float, default=0.0)
    due_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: planned / due / done / skipped
    status: Mapped[str] = mapped_column(String(16), default="planned", index=True)
    #: 计划依据：Citation 列表（与 state.Citation 字段一致）
    evidence: Mapped[list[Any]] = mapped_column(JSONColumn, default=list)
    chunk_id: Mapped[str] = mapped_column(String(64), default="")
    note: Mapped[str] = mapped_column(LongText, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, onupdate=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class PartOrder(Base):
    """备件采购申请单（②备件商城与采购）。

    边界（开发文档 §1.3「不自动下单」）：本表只承载**内部采购申请**，
    必须经人工确认（approved）才进入后续环节，系统不对供应商发起任何真实下单。
    """

    __tablename__ = "part_order"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_no: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    device_model: Mapped[str] = mapped_column(String(64), default="")
    purpose: Mapped[str] = mapped_column(String(255), default="")
    applicant: Mapped[str] = mapped_column(String(64), default="")
    #: draft / submitted / approved / rejected / received / cancelled
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    #: 明细：[{code, part, qty, unit, unit_price, amount, is_substitute, basis}]
    items: Mapped[list[Any]] = mapped_column(JSONColumn, default=list)
    total_amount: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    note: Mapped[str] = mapped_column(LongText, default="")
    trace_id: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, onupdate=_utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class PartSettlement(Base):
    """采购结算台账（②备件商城与采购）。

    只记账，不做真实财务过账（开发文档 §1.3「不做采购结算」原意是不做财务系统集成，
    这里按业务需要补一张**台账**，供对账与统计使用）。
    """

    __tablename__ = "part_settlement"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(Integer, index=True)
    order_no: Mapped[str] = mapped_column(String(32), index=True)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    #: 对公转账 / 月结 / 现结 / 其他
    method: Mapped[str] = mapped_column(String(16), default="月结")
    invoice_no: Mapped[str] = mapped_column(String(64), default="")
    operator: Mapped[str] = mapped_column(String(64), default="")
    note: Mapped[str] = mapped_column(LongText, default="")
    settled_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow)


class TrainingQuiz(Base):
    """练习题/考核题（③考核认证）。

    `items[*].evidence` 是硬要求：每道题的答案必须能追溯到知识库切片，
    这样「题目错了」可以像回答一样被复核（零幻觉贯穿到培训环节）。
    """

    __tablename__ = "training_quiz"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_model: Mapped[str] = mapped_column(String(64), default="")
    topic: Mapped[str] = mapped_column(String(255), default="")
    #: basic / advanced
    level: Mapped[str] = mapped_column(String(16), default="basic")
    n_items: Mapped[int] = mapped_column(Integer, default=0)
    #: [{no, question, type, options, answer, explanation, evidence:[Citation]}]
    items: Mapped[list[Any]] = mapped_column(JSONColumn, default=list)
    #: llm:<model> 或 extractive-fallback（降级路径）
    generator: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, index=True)


class TrainingAttempt(Base):
    """一次作答记录与判分结果（③考核认证）。"""

    __tablename__ = "training_attempt"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    quiz_id: Mapped[int] = mapped_column(Integer, index=True)
    trainee: Mapped[str] = mapped_column(String(64), default="", index=True)
    answers: Mapped[list[Any]] = mapped_column(JSONColumn, default=list)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    #: 逐题批改明细：[{no, correct, expected, got, explanation, evidence}]
    detail: Mapped[list[Any]] = mapped_column(JSONColumn, default=list)
    duration_s: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, index=True)


class Certification(Base):
    """认证记录（③考核认证）。

    发证规则由 service 层判定（必须通过考核且分数达标），
    并记录有效期用于「到期提醒」；系统**不代表原厂签发认证**，
    `issuer` 与 `note` 用于标注内部授权口径。
    """

    __tablename__ = "certification"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trainee: Mapped[str] = mapped_column(String(64), index=True)
    device_model: Mapped[str] = mapped_column(String(64), index=True)
    #: L1 / L2 / L3（按设备类型分级授权）
    level: Mapped[str] = mapped_column(String(8), default="L1")
    attempt_id: Mapped[int] = mapped_column(Integer, default=0)
    quiz_id: Mapped[int] = mapped_column(Integer, default=0)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    issuer: Mapped[str] = mapped_column(String(64), default="内部授权")
    issued_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)
    #: valid / revoked
    status: Mapped[str] = mapped_column(String(16), default="valid", index=True)
    note: Mapped[str] = mapped_column(LongText, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow)


async def init_db() -> tuple[bool, str]:
    """建表并返回 (是否可用, 说明)。失败不抛错，由健康检查暴露。"""

    global _available, _detail
    settings = get_settings()
    try:
        if settings.database_url.startswith("sqlite"):
            # 确保 SQLite 文件所在目录存在
            from pathlib import Path

            raw = settings.database_url.split("///")[-1]
            Path(raw).parent.mkdir(parents=True, exist_ok=True)
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        _available, _detail = True, "已连接"
        logger.info("数据库已就绪：%d 张表", len(Base.metadata.tables))
    except Exception as exc:  # noqa: BLE001 - 数据库不可用不能让应用起不来
        _available, _detail = False, f"数据库不可用：{exc}"
        logger.error("数据库初始化失败：%s", exc)
    return _available, _detail


async def dispose_db() -> None:
    """释放连接池（应用关闭时调用）。"""

    global _engine, _session_factory, _available, _detail
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
    _available, _detail = False, "已释放"


def is_available() -> bool:
    return _available


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """事务性会话（service 层用）。

    流式响应里不能用 FastAPI 的依赖注入（连接会在响应结束前被回收），
    所以 service 内部统一走这个上下文管理器自管理生命周期。
    """

    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：请求级会话。"""

    async with session_scope() as session:
        yield session


async def check_health() -> dict[str, Any]:
    """给 /api/health 用的数据库探测。"""

    if not _available:
        return {"ok": False, "detail": _detail}
    try:
        async with session_scope() as session:
            await session.execute(select(SessionMap.session_id).limit(1))
            total = await session.execute(text("SELECT 1"))
            total.close()
        return {"ok": True, "detail": _detail}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"数据库探测失败：{exc}"}


__all__ = [
    "Base",
    "KbDocument",
    "KbImage",
    "QaFeedback",
    "QaRecord",
    "QuestionStat",
    "SessionMap",
    "check_health",
    "dispose_db",
    "get_db",
    "get_engine",
    "get_session_factory",
    "init_db",
    "is_available",
    "session_scope",
]
