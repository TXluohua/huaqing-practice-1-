"""会话记忆：检查点（checkpointer）与 session_id <-> thread_id 映射。

对应开发文档 5.6 / 6.2：
    会话状态由 SqliteSaver 管理，业务数据使用 MySQL；
    session_map 表保存 session_id 与 thread_id 的对应关系。

降级策略（与开发文档 R7「MCP 工具加载失败影响主链路」同一原则）：
    检查点后端不可用时不得中断主链路，按 sqlite(异步) -> sqlite(同步) -> 内存
    逐级降级，并把实际使用的后端名暴露出来，供 /api/health 显示。

    当前环境未安装 langgraph-checkpoint-sqlite，因此实际会落到 InMemorySaver。
    需要落盘时执行：pip install langgraph-checkpoint-sqlite
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

#: 检查点数据库默认位置（backend/data/ 为运行时数据目录，不入 git）
DEFAULT_SQLITE_PATH = Path(__file__).resolve().parent / "data" / "sqlite" / "checkpoints.sqlite"

#: 后端标识
BACKEND_ASYNC_SQLITE = "sqlite-async"
BACKEND_SYNC_SQLITE = "sqlite-sync"
BACKEND_MEMORY = "memory"


@dataclass
class CheckpointerHandle:
    """检查点实例及其释放方式。"""

    saver: Any
    backend: str
    _closer: Callable[[], Awaitable[None]] | None = field(default=None, repr=False)

    @property
    def persistent(self) -> bool:
        """是否为落盘后端（服务重启后会话仍可回看）。"""

        return self.backend != BACKEND_MEMORY

    async def aclose(self) -> None:
        """释放底层连接（内存后端无需释放）。"""

        if self._closer is not None:
            closer, self._closer = self._closer, None
            await closer()


async def create_checkpointer(
    sqlite_path: str | Path | None = None,
) -> CheckpointerHandle:
    """创建检查点，按 sqlite(异步) -> sqlite(同步) -> 内存 逐级降级。

    参数
    ----
    sqlite_path:
        落盘路径，默认 backend/data/sqlite/checkpoints.sqlite。

    返回
    ----
    CheckpointerHandle，其 backend 字段说明实际生效的后端。
    """

    path = Path(sqlite_path) if sqlite_path is not None else DEFAULT_SQLITE_PATH

    # ---- 1) 异步 sqlite（全异步链路首选）----
    try:
        import aiosqlite  # type: ignore
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver  # type: ignore
    except ImportError:
        pass
    else:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = await aiosqlite.connect(str(path))
            saver = AsyncSqliteSaver(conn)

            async def _close_async() -> None:
                await conn.close()

            logger.info("检查点后端：异步 sqlite (%s)", path)
            return CheckpointerHandle(saver, BACKEND_ASYNC_SQLITE, _close_async)
        except Exception as exc:  # noqa: BLE001 - 任何失败都降级，不中断主链路
            logger.warning("异步 sqlite 检查点初始化失败，继续降级：%s", exc)

    # ---- 2) 同步 sqlite ----
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver  # type: ignore
    except ImportError:
        pass
    else:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(path), check_same_thread=False)
            saver = SqliteSaver(conn)

            async def _close_sync() -> None:
                conn.close()

            logger.info("检查点后端：同步 sqlite (%s)", path)
            return CheckpointerHandle(saver, BACKEND_SYNC_SQLITE, _close_sync)
        except Exception as exc:  # noqa: BLE001
            logger.warning("同步 sqlite 检查点初始化失败，继续降级：%s", exc)

    # ---- 3) 内存兜底 ----
    from langgraph.checkpoint.memory import InMemorySaver

    logger.warning(
        "未安装 langgraph-checkpoint-sqlite，检查点降级为内存："
        "服务重启后会话历史将丢失（pip install langgraph-checkpoint-sqlite 可启用落盘）"
    )
    return CheckpointerHandle(InMemorySaver(), BACKEND_MEMORY)


def thread_config(thread_id: str) -> dict[str, Any]:
    """构造 LangGraph 调用配置（会话隔离靠 thread_id）。"""

    if not thread_id:
        raise ValueError("thread_id 不能为空")
    return {"configurable": {"thread_id": thread_id}}


class SessionRegistry:
    """session_id <-> thread_id 映射（开发文档 5.6 的 session_map 表）。

    骨架阶段为进程内实现；接入 MySQL 时只需替换本类，
    上层（chat_service / agent_factory）无需改动，
    因为只依赖 thread_id() 与 bind() 两个方法。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_session: dict[str, dict[str, Any]] = {}

    @staticmethod
    def new_session_id() -> str:
        """生成新的 session_id。"""

        return uuid.uuid4().hex

    def bind(
        self,
        session_id: str,
        *,
        thread_id: str | None = None,
        title: str | None = None,
    ) -> str:
        """绑定会话并返回 thread_id。

        thread_id 一经绑定不再变更，保证同一会话始终命中同一份检查点。
        """

        if not session_id:
            raise ValueError("session_id 不能为空")
        with self._lock:
            record = self._by_session.get(session_id)
            if record is None:
                record = {
                    "thread_id": thread_id or "thread-" + session_id,
                    "title": title or "",
                }
                self._by_session[session_id] = record
            elif title:
                record["title"] = title
            return str(record["thread_id"])

    def thread_id(self, session_id: str) -> str:
        """取（必要时创建）会话对应的 thread_id。"""

        return self.bind(session_id)

    def session_id_for(self, thread_id: str) -> str | None:
        """反查：thread_id -> session_id。"""

        with self._lock:
            for session_id, record in self._by_session.items():
                if record["thread_id"] == thread_id:
                    return session_id
        return None

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """当前映射快照（供会话列表接口使用）。"""

        with self._lock:
            return {k: dict(v) for k, v in self._by_session.items()}

    def reset(self) -> None:
        """清空映射（测试用）。"""

        with self._lock:
            self._by_session.clear()


#: 进程内单例。跨进程 / 持久化场景请替换为 MySQL session_map 表实现。
session_registry = SessionRegistry()
