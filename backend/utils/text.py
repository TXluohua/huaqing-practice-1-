"""文本工具（开发文档 6.2：分句 / 脱敏 / 日志）。

说明：中文分句由 RAG 侧统一提供（backend/rag/ingest.split_sentences），
本模块**不再重复实现**，只放接口层自用的脱敏、截断与时间格式化。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

#: 需要脱敏的密钥形态（NFR-03：日志与前端不得出现密钥）
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9_\-]{6,}"),                        # DeepSeek / OpenAI 风格
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}"),           # Authorization: Bearer xxx
    re.compile(r"(?i)(api[_-]?key\"?\s*[:=]\s*\"?)[A-Za-z0-9._\-]{6,}"),
    re.compile(r"(?i)(dashscope[_-]?api[_-]?key\"?\s*[:=]\s*\"?)[A-Za-z0-9._\-]{6,}"),
)

_REDACTED = "***"


def mask_secrets(text: str | None) -> str:
    """把密钥替换成 ***，用于日志与错误信息透出。"""

    if not text:
        return ""
    masked = str(text)
    masked = _SECRET_PATTERNS[0].sub("sk-***", masked)
    for pattern in _SECRET_PATTERNS[1:]:
        masked = pattern.sub(lambda m: f"{m.group(1)}{_REDACTED}", masked)
    return masked


def truncate(text: str | None, limit: int = 200, suffix: str = "…") -> str:
    """按字符数截断（用于会话标题、日志摘要）。"""

    value = (text or "").strip()
    if limit <= 0 or len(value) <= limit:
        return value
    return value[:limit].rstrip() + suffix


def make_title(question: str | None, limit: int = 30) -> str:
    """由首个问题生成会话标题。"""

    return truncate(question, limit=limit, suffix="") or "新会话"


def now_iso() -> str:
    """本地时区的 ISO 8601 时间戳（接口文档 §1.2 时间格式）。"""

    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def to_iso(value: datetime | None) -> str:
    """把数据库里的 datetime 转成接口约定的 ISO 8601 字符串。"""

    if value is None:
        return now_iso()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone().isoformat(timespec="seconds")
