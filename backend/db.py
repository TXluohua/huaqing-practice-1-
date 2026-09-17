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

from sqlalchemy import DateTime, Float, Integer, String, Text, select, text
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
