"""文本工具（开发文档 6.2：分句 / 脱敏 / 日志）。

说明：中文分句由 RAG 侧统一提供（backend/rag/ingest.split_sentences），
本模块**不再重复实现**，只放接口层自用的脱敏、截断与时间格式化。
"""

from __future__ import annotations

import logging
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


#: 归一化问题时要抹掉的字符（空白与中英文标点）
_QUESTION_NOISE = re.compile(r"[\s，。？！、；：,.?!;:\"'（）()\[\]【】《》<>「」『』~～\-—_]+")


def normalize_question(text: str | None, limit: int = 120) -> str:
    """归一化问题，用于高频问题聚合计数（FR-09）。

    「腔体门 O-ring 还有库存吗？」与「腔体门O-ring还有库存吗」会归并成同一条。
    """

    value = _QUESTION_NOISE.sub("", (text or "").strip().lower())
    return value[:limit]


def make_title(question: str | None, limit: int = 30) -> str:
    """由首个问题生成会话标题。"""

    return truncate(question, limit=limit, suffix="") or "新会话"


def now_iso() -> str:
    """本地时区的 ISO 8601 时间戳（接口文档 §1.2 时间格式）。"""

    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


#: 图片/文档落盘路径：日志里只保留目录，文件名打码（NFR-03）
_PATH_PATTERN = re.compile(
    r"(/(?:[^\s\"']*/)?backend/data/(?:uploads|images|documents)/)[^\s\"',;)\]]+"
)

#: 需要压到 WARNING 的第三方 logger（DEBUG 噪音大户）
_NOISY_LOGGERS: tuple[str, ...] = (
    "langgraph",
    "aiosqlite",
    "httpx",
    "httpcore",
    "jieba",
    "chromadb",
    "urllib3",
    "asyncio",
    "watchfiles",
    "multipart",
)


class SecretMaskingFilter(logging.Filter):
    """把日志消息里的密钥与图片路径打码（NFR-03）。

    注意：过滤器改写的是 record.msg（并清空 args），必须在**格式化之前**生效，
    所以挂在 handler 上而不是 logger 上。
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - 格式化失败不能影响日志输出
            return True
        masked = _PATH_PATTERN.sub(r"\1<file>", mask_secrets(message))
        if masked != message:
            record.msg = masked
            record.args = ()
        return True


def setup_logging(level: str | None = None) -> str:
    """配置根日志：级别 + 脱敏过滤器 + 压第三方噪音。返回最终生效的级别。

    刻意**不使用 force=True**：uvicorn 已经装了 handler，force 会把它顶掉。
    """

    resolved = (level or "INFO").upper()
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=resolved, format="%(levelname)s %(name)s: %(message)s")
    root.setLevel(resolved)
    for handler in root.handlers:
        handler.setLevel(resolved)
        if not any(isinstance(item, SecretMaskingFilter) for item in handler.filters):
            handler.addFilter(SecretMaskingFilter())

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    return resolved


def to_iso(value: datetime | None) -> str:
    """把数据库里的 datetime 转成接口约定的 ISO 8601 字符串。"""

    if value is None:
        return now_iso()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone().isoformat(timespec="seconds")
