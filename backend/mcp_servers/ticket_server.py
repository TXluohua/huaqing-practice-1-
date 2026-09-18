"""历史工单检索 MCP server（stdio）—— 开发文档 4.4.3 / 6.2。

为什么用 MCP（开发文档 4.4.3 的判断标准）
------------------------------------------
历史工单属**系统边界**：① 跨系统（未来对接真实工单系统）；② 需独立部署与独立权限；
接口按 MCP 定义后，替换底层实现不影响上层。知识库检索则留在进程内（同进程同语言，
包成 MCP 只会增加进程与延迟）。

启动方式（由 backend/mcp_client.py 以 stdio 拉起，一般不用手动执行）
----------------------------------------------------------------
    .venv/bin/python backend/mcp_servers/ticket_server.py

**注意**：stdio 传输用 stdout 传协议，因此本文件严禁往 stdout 打日志——
日志一律走 stderr，否则会污染协议流。
"""

from __future__ import annotations

import logging
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

# 以脚本方式被 stdio 拉起时，项目根不在 sys.path 上，需要自己补
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.setting import get_settings  # noqa: E402

logger = logging.getLogger(__name__)

#: fastmcp 是可选依赖：缺失时本模块仍可导入（search_tickets 供测试与降级路径使用），
#: 只是不提供 MCP server。这样「工具可独立调用」与「MCP 不可用时主链路照常」都能成立。
try:  # pragma: no cover - 取决于环境是否装了 fastmcp
    from fastmcp import FastMCP
except ImportError:  # pragma: no cover
    FastMCP = None  # type: ignore[assignment]


def _tokens(text: str) -> set[str]:
    """中文分词（与检索侧同一套 jieba，避免两套口径）。"""

    import jieba

    return {token.strip().lower() for token in jieba.lcut(text or "") if len(token.strip()) > 1}


@lru_cache(maxsize=1)
def load_tickets() -> list[dict[str, Any]]:
    """读工单数据源（backend/config/tickets.yaml）。缺失时返回空列表，由调用方明确「查不到」。"""

    settings = get_settings()
    path = Path(settings.tickets_path)
    if not path.is_file():
        logger.warning("工单数据源不存在：%s", path)
        return []
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 - 数据源坏了转成「查不到」，不让工具崩
        logger.warning("工单数据源解析失败：%s", exc)
        return []
    tickets = data.get("tickets") if isinstance(data, dict) else None
    return [item for item in (tickets or []) if isinstance(item, dict)]


def reset_cache() -> None:
    """清空工单缓存（改数据后或测试用）。"""

    load_tickets.cache_clear()


def search_tickets(
    symptom: str,
    device_model: str | None = None,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """按故障现象检索历史工单（纯函数，供 MCP 工具与测试直接调用）。

    打分：现象/根因/处置/结论几处的词重合度；设备型号命中加权。
    不做语义检索——工单案例是短文本，关键词重合已经够用，且**确定性可解释**。
    """

    query = _tokens(symptom)
    if not query:
        return []

    scored: list[tuple[float, dict[str, Any]]] = []
    for ticket in load_tickets():
        haystack = " ".join(
            str(ticket.get(key) or "")
            for key in ("symptom", "root_cause", "action", "conclusion")
        )
        overlap = query & _tokens(haystack)
        if not overlap:
            continue
        score = len(overlap) / max(1, len(query))
        if device_model and str(ticket.get("device_model") or "") == device_model:
            score += 0.5  # 同型号案例优先
        scored.append((round(score, 4), ticket))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    results: list[dict[str, Any]] = []
    for score, ticket in scored[: max(1, limit)]:
        results.append({**ticket, "score": score, "source": "ticket"})
    return results


def format_ticket(ticket: dict[str, Any]) -> str:
    """把工单整理成可进上下文的文本块（供 augment 注入证据）。"""

    parts_used = ticket.get("parts_used") or []
    lines = [
        f"历史工单 {ticket.get('ticket_no')}（{ticket.get('device_model')}，{ticket.get('date')}）",
        f"现象：{ticket.get('symptom')}",
        f"根因：{ticket.get('root_cause')}",
        f"处置：{ticket.get('action')}",
    ]
    if parts_used:
        lines.append("涉及备件：" + "、".join(str(item) for item in parts_used))
    if ticket.get("conclusion"):
        lines.append(f"经验：{ticket.get('conclusion')}")
    if ticket.get("related_doc"):
        lines.append(f"关联手册：{ticket.get('related_doc')}")
    return "\n".join(lines)


if FastMCP is not None:  # pragma: no cover - 需要 fastmcp
    mcp = FastMCP("smka-ticket")

    @mcp.tool
    def ticket_search(
        symptom: str,
        device_model: str | None = None,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        """检索历史工单案例（故障现象 -> 根因 / 处置 / 涉及备件 / 经验）。

        何时使用：用户描述设备故障现象、需要「以前是怎么处理的」「有没有类似案例」时；
                 或诊断类问题需要补充真实处置经验时。
        参数：
            symptom      故障现象描述（中文自然语言即可，如「腔体真空度异常波动」）
            device_model 设备型号（可选，同型号案例会被优先返回）
            limit        返回条数，默认 3
        返回：工单列表，每项含 ticket_no / device_model / symptom / root_cause /
              action / parts_used / conclusion / date / related_doc / score。
              **查不到时返回空列表** —— 不要因为返回空就编造案例。
        """

        return search_tickets(symptom, device_model=device_model, limit=limit)


def main() -> None:
    """以 stdio 启动 MCP server（日志走 stderr，别污染协议流）。"""

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    if FastMCP is None:  # pragma: no cover
        print("未安装 fastmcp：pip install fastmcp==2.14.7", file=sys.stderr)
        raise SystemExit(1)
    # show_banner=False：banner 由 rich 输出，默认可能走 stdout，
    # 而 stdio 传输用 stdout 传协议 —— 必须关掉，否则有污染协议流的风险。
    mcp.run(show_banner=False)


if __name__ == "__main__":  # pragma: no cover - 由 mcp_client 拉起
    main()


__all__ = ["FastMCP", "format_ticket", "load_tickets", "main", "reset_cache", "search_tickets"]
