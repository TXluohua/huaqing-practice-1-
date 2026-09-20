"""维护计划生成服务（开发文档 6.2 的 plan_service.py）。

功能
----
按设备型号生成**可溯源的维护计划**：从知识库里检索该设备的 PM / 更换 / 校准 /
清洗周期条款，用正则从**原文**抽取周期数值，再按运行小时 / 生产片数 / 自然日
三种口径推算剩余量与到期时间，落库到 `maintenance_plan`。

冻结契约（`backend/routers/__init__.py` 的 `_PLAN_SERVICE_API`，签名不要改）
--------------------------------------------------------------------------
    generate_plans(payload: PlanGenerateRequest, *, trace_id) -> dict
        # PlanGenerateResponse.model_dump()，payload.persist=False 时只预览不落库
    list_plans(*, device_model, status, limit, offset, trace_id) -> dict
        # PlanListResponse.model_dump()，按 remaining 升序（最紧急在前）
    complete_plan(plan_id, payload, *, trace_id) -> dict | None   # None -> 路由 404
    skip_plan(plan_id, payload, *, trace_id) -> dict | None       # None -> 路由 404

**零幻觉是硬约束**（本项目贯穿问答 / 考题 / 计划三块业务）
---------------------------------------------------------
1. 周期数值**只能来自知识库原文**：`kb_search` 拿到的 `Evidence.text` 里用正则
   抽周期（`每 4000 运行小时` / `每 25 片` / `每 3 个月` / `校准周期 90 天` /
   `累计射频小时达到 1500 h` …），本模块**不猜、不补、不四舍五入**。
2. 每条计划项必带 `evidence`（`evidence_to_citations([该片段])`，字段与
   `state.Citation` 同源）与 `chunk_id`；抽不到依据的项目**不进 `items`**，
   而是把名称放进 `uncovered` 并注明原因（未收录 / 未给周期 / 只有执行频次）。
3. 只有 `每班次`、`每次开腔` 这类**执行频次**而没有可换算数值的条款，不折算成
   天数硬塞进计划 —— 归入 `uncovered`（`每 24 小时折算成 1 天` 这种假设不写进
   计划，避免把假设计划当成手册周期执行）。
4. 备件到货周期、库存盘点、记录保存年限、许可有效期等**不是维护周期**的
   "每 N 天/月"一律排除（`_EXCLUDE_CONTEXT` + 动作词白名单双重把关）。

周期口径（与 `db.MaintenancePlan.cycle_basis` 一致）
---------------------------------------------------
* `hours` 运行小时：`remaining = cycle_value - (hours_since_pm or runtime_hours)`，
  `due_at = now + max(0, remaining)/24 天`（按 24 h 连续运行折算），
  `remaining < 0` 时 `due_at = now`；`current_value = runtime_hours`，
  `baseline_value = current_value - 已用小时`。
* `wafers` 生产片数：`remaining = cycle_value - (wafers_since_pm or wafer_count)`；
  **估算不出产线速率，`due_at` 一律留空**（不猜）。
* `days` 自然日：`remaining = cycle_value`，`due_at = now + cycle_value 天`。
* `remaining <= 0` -> `status = "due"`（已超期），否则 `"planned"`。

其它约定
--------
* 重复生成**不删旧数据**（去重不是本功能要求，历史计划本来就要留档），
  新行照常插入；`note` 里写明来源与周期原文。
* 时间统一 UTC（`db.UTCDateTime` 落库为 naive UTC，输出 `isoformat()` 带 `+00:00`）。
* 数据库不可用一律 `503 DB_UNAVAILABLE`，知识库检索全失败一律 `503 KB_UNAVAILABLE`，
  都不让它变成 500。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy import func, select

from .. import db
from ..db import MaintenancePlan
from ..rag.retriever import build_filters, evidence_to_citations
from ..schemas import (
    PlanGenerateRequest,
    PlanGenerateResponse,
    PlanItem,
    PlanUpdateRequest,
    ServiceError,
)
from ..tools.kb_tools import kb_corpus_text, kb_search

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: 「通用」资料（安全作业标准等）对所有型号都适用，与 rag.retriever.build_filters 同口径
GENERIC_MODEL = "通用"

#: 周期口径（与 db.MaintenancePlan.cycle_basis 注释一致；本服务只产出这三种）
BASIS_HOURS = "hours"
BASIS_WAFERS = "wafers"
BASIS_DAYS = "days"
CYCLE_BASES: tuple[str, ...] = (BASIS_HOURS, BASIS_WAFERS, BASIS_DAYS)

#: 计划状态全集
PLAN_STATUSES: tuple[str, ...] = ("planned", "due", "done", "skipped")

#: 折算常数（只在把「月/周/年」换成自然日时使用，来源在 note 里写明）
_DAYS_PER_MONTH = 30.0
_DAYS_PER_WEEK = 7.0
_DAYS_PER_YEAR = 365.0
_HOURS_PER_DAY = 24.0

#: 分页上限（防止一次拉全表）
_MAX_LIMIT = 200

#: 证据池缓存有效期（秒）。同一设备 + 同一 k 的检索结果在 TTL 内复用：
#: 生成一次计划要打 2~3 次检索（每次都要过精排），缓存让重复生成不必重跑。
_EVIDENCE_TTL_S = 120.0


def _queries(device_model: str) -> tuple[str, ...]:
    """生成该设备计划时使用的检索式（多路召回后合并去重）。"""

    return (
        f"{device_model} 维护周期 更换周期 保养 运行小时 片数 校准 清洗 点检",
        f"{device_model} PM 更换周期 换油 过滤器 O-ring 校准周期 清洗周期 累计小时 累计片数",
    )


#: 证据池缓存：key = (device_model, k)，value = (写入时间, Evidence 列表)
_EVIDENCE_CACHE: dict[tuple[str, int], tuple[float, list[Any]]] = {}


def reset_plan_cache() -> None:
    """清空检索证据缓存（索引重建 / 语料更新后调用；测试也可用来强制重新检索）。"""

    _EVIDENCE_CACHE.clear()


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _require_db() -> None:
    """数据库不可用时给明确的 503，而不是让异常漏成 500。"""

    if not db.is_available():
        raise ServiceError(503, "DB_UNAVAILABLE", "数据库不可用，请检查 setting.database_url")


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:  # 兜底：直接构造的 naive datetime 按 UTC 解释
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _page(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(_as_int(limit, 20), _MAX_LIMIT)), max(0, _as_int(offset, 0))


def _append_note(existing: str, extra: str) -> str:
    """追加 note（保留历史，用「；」分隔，不覆盖原有溯源信息）。"""

    existing = (existing or "").strip()
    extra = (extra or "").strip()
    if not extra:
        return existing
    return f"{existing}；{extra}" if existing else extra


def _unit_label(basis: str) -> str:
    return {BASIS_HOURS: "小时", BASIS_WAFERS: "片", BASIS_DAYS: "天"}.get(basis, "")


# --------------------------------------------------------------------------- #
# ① 周期抽取（零幻觉的核心：数值只从原文里抓）
# --------------------------------------------------------------------------- #

_NUM = r"\d+(?:\.\d+)?"


@dataclass(frozen=True)
class _CycleSpec:
    """一种周期写法：正则 + 折算系数（转成 cycle_value）。"""

    basis: str
    pattern: re.Pattern[str]
    factor: float
    unit: str
    source_unit: str


#: 数值型周期。`factor` 把原文单位折算成 cycle_value 的单位：
#: hours -> 小时，wafers -> 片，days -> 自然日（1 个月按 30 天、1 周按 7 天、1 年按 365 天）。
_CYCLE_SPECS: tuple[_CycleSpec, ...] = (
    _CycleSpec(BASIS_HOURS, re.compile(rf"({_NUM})\s*(?:个)?\s*(?:运行)?\s*小时"), 1.0, "小时", "小时"),
    _CycleSpec(BASIS_HOURS, re.compile(rf"({_NUM})\s*h(?![A-Za-z])"), 1.0, "小时", "h"),
    _CycleSpec(BASIS_WAFERS, re.compile(rf"({_NUM})\s*万片"), 10000.0, "片", "万片"),
    _CycleSpec(BASIS_WAFERS, re.compile(rf"({_NUM})\s*片"), 1.0, "片", "片"),
    _CycleSpec(BASIS_DAYS, re.compile(rf"({_NUM})\s*天"), 1.0, "天", "天"),
    _CycleSpec(BASIS_DAYS, re.compile(rf"({_NUM})\s*个?\s*月"), _DAYS_PER_MONTH, "天", "个月"),
    _CycleSpec(BASIS_DAYS, re.compile(rf"({_NUM})\s*个?\s*周"), _DAYS_PER_WEEK, "天", "周"),
    _CycleSpec(BASIS_DAYS, re.compile(rf"({_NUM})\s*个?\s*年"), _DAYS_PER_YEAR, "天", "年"),
)

#: 无数字的固定周期写法（每月 / 每周 / 每年 …）
_FIXED_CYCLES: tuple[tuple[re.Pattern[str], str, float, str], ...] = (
    (re.compile(r"每季度"), BASIS_DAYS, 90.0, "天"),
    (re.compile(r"每半年"), BASIS_DAYS, 180.0, "天"),
    (re.compile(r"每月"), BASIS_DAYS, _DAYS_PER_MONTH, "天"),
    (re.compile(r"每周"), BASIS_DAYS, _DAYS_PER_WEEK, "天"),
    (re.compile(r"每年"), BASIS_DAYS, _DAYS_PER_YEAR, "天"),
)

#: 只有执行频次、没有可换算数值的写法 -> 进 uncovered，不折算成天数
_FREQUENCY_ONLY: tuple[re.Pattern[str], ...] = (
    re.compile(r"每班次"),
    re.compile(r"每次开腔"),
    re.compile(r"每次拆装"),
    re.compile(r"每次作业前"),
    re.compile(r"每批"),
)

#: 不是维护周期的「每 N 天/月/年」：记录保存年限、库存盘点、请购到货、许可有效期……
_EXCLUDE_CONTEXT: tuple[str, ...] = (
    "保存",
    "台账",
    "有效期",
    "盘点",
    "核对",
    "请购",
    "申报",
    "提前期",
    "到货",
    "入库",
    "验收",
    "领用",
    "存放",
    "库存",
    "交底",
    "许可",
    "报价",
    "预算",
    "费用",
    # 故障处理 / 触发条件，不是周期：如「同一报警在 7 天内重复出现 3 次以上」
    "工单",
    "重复出现",
    "上报",
    "判定是否",
    # 验证步骤（空跑空白片）不是周期
    "空跑",
    "空白片",
)

#: 周期语境标记：数值前面（同一个小句内）必须出现其中之一，否则不算周期写法。
#: 覆盖手册里的常见写法：`每 4000 运行小时` / `换油周期为 4000 运行小时或 12 个月` /
#: `累计射频小时达到 1500 h` / `使用满 3 个月` / `间隔 90 天`。
_CYCLE_MARKERS: tuple[str, ...] = (
    "每",
    "周期",
    "累计",
    "达到",
    "满",
    "间隔",
    "至少",
    "不超过",
    "不低于",
)

#: 小句边界（判定「数值前面」的范围时用；不含空格，避免把「换油周期为 4000」截断）
_CLAUSE_BOUNDARY = re.compile(r"[，,、；;：:。（）()【】\[\]]")

#: 维护动作词白名单：句子里必须出现其中之一，才认为它在讲维护周期
_ACTION_WORDS: tuple[str, ...] = (
    "更换",
    "换油",
    "替换",
    "检查",
    "巡检",
    "点检",
    "保养",
    "维护",
    "清洗",
    "校准",
    "校验",
    "标定",
    "检定",
    "试验",
    "试水",
    "检测",
    "测试",
    "验证",
    "演练",
    "检修",
    "寿命",
    "报废",
    "PM",
)

#: 句子里出现这些词说明讲的是到货/交期，而不是维护周期
_DELIVERY_CONTEXT: tuple[str, ...] = ("到货周期", "交期", "提前期", "请购", "采购")

_SENTENCE_SPLIT = re.compile(r"[。\n\r]+")


@dataclass(frozen=True)
class _Cycle:
    """从原文抽到的一条周期。"""

    basis: str
    #: 折算后的周期值（days 口径为自然日）
    value: float
    #: 原文片段（原样保留，写进 note 供人工复核）
    raw: str
    #: 在句子里的起始下标（用于「离关键词最近」的归属判定）
    start: int
    unit: str
    factor: float
    source_unit: str


def _sentences(text: str) -> list[str]:
    """把切片正文切成句子（按句号与换行；分号保留，便于保持条款上下文）。"""

    return [s.strip() for s in _SENTENCE_SPLIT.split(text or "") if s and s.strip()]


def _is_cycle_sentence(sentence: str) -> bool:
    """判定一句话是否在讲维护周期（排除保存年限/库存盘点/备件交期/故障处理等）。"""

    if any(word in sentence for word in _EXCLUDE_CONTEXT):
        return False
    if any(word in sentence for word in _DELIVERY_CONTEXT):
        return False
    return any(word in sentence for word in _ACTION_WORDS)


def _has_cycle_context(sentence: str, start: int) -> bool:
    """数值前面（同一个小句内）是否有周期语境标记。

    这一条把「空跑 3 片空白片」「同一报警在 7 天内重复出现」「记录保存 12 个月」
    这类**不是周期**的数值挡在门外 —— 宁可少生成一条，也不把假周期写进计划。
    """

    head = sentence[:start]
    boundary = None
    for match in _CLAUSE_BOUNDARY.finditer(head):
        boundary = match.end()
    clause = head[boundary:] if boundary is not None else head
    return any(marker in clause for marker in _CYCLE_MARKERS)


def _extract_cycles(sentence: str) -> list[_Cycle]:
    """从句子里抽出**全部**周期（按出现位置排序，第一个即为主周期）。

    数值一律来自 `sentence` 原文，函数只做「单位 -> cycle_value」的换算；
    换算系数与原文片段都随 `_Cycle` 一起返回，写进 note 供人工复核。
    """

    found: list[_Cycle] = []
    for spec in _CYCLE_SPECS:
        for match in spec.pattern.finditer(sentence):
            value = _as_float(match.group(1)) * spec.factor
            if value <= 0 or not _has_cycle_context(sentence, match.start()):
                continue
            found.append(
                _Cycle(
                    basis=spec.basis,
                    value=round(value, 3),
                    raw=match.group(0).strip(),
                    start=match.start(),
                    unit=spec.unit,
                    factor=spec.factor,
                    source_unit=spec.source_unit,
                )
            )
    for pattern, basis, value, unit in _FIXED_CYCLES:
        for match in pattern.finditer(sentence):
            found.append(
                _Cycle(
                    basis=basis,
                    value=value,
                    raw=match.group(0).strip(),
                    start=match.start(),
                    unit=unit,
                    factor=1.0,
                    source_unit=unit,
                )
            )

    order = {basis: index for index, basis in enumerate(CYCLE_BASES)}
    found.sort(key=lambda item: (item.start, order.get(item.basis, len(order))))
    # 同一位置同一口径只留一条（不同 spec 可能落在同一片段上）
    unique: list[_Cycle] = []
    seen: set[tuple[int, str]] = set()
    for item in found:
        key = (item.start, item.basis)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _has_frequency_only(sentence: str) -> bool:
    return any(pattern.search(sentence) for pattern in _FREQUENCY_ONLY)


# --------------------------------------------------------------------------- #
# ② 计划项名称与类型（名称也只从原文/清单里取，不由模型生成）
# --------------------------------------------------------------------------- #

_NAME_SPLIT = re.compile(r"[，,、：:；;]")

#: 紧贴在周期数值前的连接词，取名时要剥掉
_PREFIX_CONNECTORS: tuple[str, ...] = (
    "更换周期为",
    "更换周期",
    "换油周期为",
    "换油周期",
    "校准周期为",
    "校准周期",
    "清洗周期为",
    "清洗周期",
    "点检周期为",
    "点检周期",
    "维护周期为",
    "维护周期",
    "更换周期",
    "周期为",
    "周期约",
    "周期",
    "间隔为",
    "间隔",
    "使用满",
    "连续使用",
    "累计使用",
    "累计",
    "达到",
    "不超过",
    "不少于",
    "至少",
    "约为",
    "建议",
    "应",
    "须",
    "需",
    "为",
    "是",
    "每",
)

#: 以这些词开头的片段不是「被维护的对象」，不能当名称
_WEAK_PREFIXES: tuple[str, ...] = (
    "使用",
    "累计",
    "达到",
    "不超过",
    "不少于",
    "至少",
    "约为",
    "约",
    "连续",
    "运行",
    "每",
    "更换",
    "换油",
    "检查",
    "巡检",
    "校准",
    "清洗",
    "周期",
    "时间",
    "间隔",
    "距离",
    "磨损",
    "视",
    "如",
    "若",
    "当",
    "确认",
    "记录",
)

#: 出现这些词说明名称后面还挂着工况/条件说明，直接截断
_NAME_CUTS: tuple[str, ...] = (
    "距离",
    "位于",
    "安装",
    "运行至",
    "超过",
    "低于",
    "高于",
    "不得",
    "不低于",
    "不少于",
)


def _clean_name(segment: str, *, allow_digits: bool) -> str:
    """把一个文本片段清理成计划项名称；清理后不像名称就返回空串。"""

    text = (segment or "").strip(" 　-—–·*")
    text = re.sub(r"[（(][^）)]*[）)]?", "", text)  # 去掉括号说明
    text = text.strip(" 　的")

    for cut in _NAME_CUTS:  # 先截掉工况/条件说明（取最靠前的截断点）
        index = text.find(cut)
        if index >= 2:
            text = text[:index]
            break

    changed = True
    while changed and text:  # 再剥掉动作/连接前缀
        changed = False
        for token in _WEAK_PREFIXES:
            if text.startswith(token) and len(text) > len(token):
                text = text[len(token) :].strip(" 　的")
                changed = True
                break

    if re.search(r"\d", text):
        if not allow_digits:
            return ""
        text = re.sub(r"\d.*$", "", text)
        text = text.strip(" 　的-—·*")
        if re.search(r"\d", text):
            return ""

    text = text.strip(" 　的-—·*不")
    if len(text) < 2 or len(text) > 60:
        return ""
    if text in _WEAK_PREFIXES:
        return ""
    return text


def _derive_name(sentence: str, cycle: _Cycle, *, heading: str, doc_title: str) -> str:
    """从「周期所在条款里、周期之前的那段话」推导计划项名称（纯文本规则，不调模型）。

    取名顺序：
        1. 条款内冒号前的主题词（手册条目写法是 `主题：说明`，如 `便携式检测仪：…`）
        2. 条款内离周期最近的片段（如 `…绝缘工具；绝缘手套每 6 个月…` -> 绝缘手套）
        3. 整句开头的话题词（条款里只剩 `更换周期为` 这类连接词时，如 `SC-1 药液：…`）
        4. 切片标题 / 文档名（兜底，明确标注是维护项）
    """

    clause_start, _ = _clause_span_of(sentence, cycle)
    prefix = sentence[clause_start : cycle.start].strip(" 　-—–·*：:，,、;；")
    # 剥掉紧贴在周期前的连接词（如「换油周期为」「…手套每」）
    changed = True
    while changed and prefix:
        changed = False
        for token in _PREFIX_CONNECTORS:
            if prefix.endswith(token):
                prefix = prefix[: -len(token)].strip(" 　的")
                changed = True
                break
    prefix = prefix.strip(" 　-—–·*：:，,、;；")

    segments = [seg.strip() for seg in _NAME_SPLIT.split(prefix) if seg.strip()]
    topic = re.split(r"[：:]", prefix, maxsplit=1)[0].strip()
    ordered: list[str] = []
    if topic and topic != prefix:
        ordered.append(topic)
    ordered.extend(reversed(segments))

    for allow_digits in (False, True):  # 先要不含数字的片段，再放宽
        for segment in ordered:
            cleaned = _clean_name(segment, allow_digits=allow_digits)
            if cleaned:
                return _with_action(cleaned, sentence, cycle)
    return _sentence_topic(sentence) or (heading or "").strip() or f"{doc_title} 维护项"


#: 紧跟在周期后面的动作词（用于把过短的名称补全，如 `浓度每 4 小时检测` -> 浓度检测）
_TRAILING_ACTIONS: tuple[str, ...] = (
    "更换",
    "换油",
    "检查",
    "检测",
    "测试",
    "校准",
    "校验",
    "清洗",
    "试验",
    "验证",
    "保养",
    "润滑",
)


def _with_action(name: str, sentence: str, cycle: _Cycle) -> str:
    """名称过短时，用紧跟在周期后面的动作词补全（`浓度` -> `浓度检测`）。"""

    if len(name) > 3:
        return name
    tail = sentence[cycle.start : cycle.start + 12]
    for action in _TRAILING_ACTIONS:
        index = tail.find(action)
        if 0 <= index <= 4 and action not in name:
            return f"{name}{action}"
    return name


def _sentence_topic(sentence: str) -> str:
    """整句开头的话题词（`SC-1 药液：…` -> `SC-1 药液`），用作取名兜底。"""

    head = re.split(r"[：:，,、；;]", sentence.strip())[0]
    head = re.sub(r"^[\s\-–—·*0-9.、）)]+", "", head).strip(" 　的")
    if head.startswith(("每", "周期")) or re.match(r"^\d", head):
        return ""
    return head if 2 <= len(head) <= 40 else ""


#: 动作词 -> 计划项类型（取值见 db.MaintenancePlan.item_type 注释）
_TYPE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("校准", ("校准", "校验", "标定", "检定", "标样")),
    ("清洗", ("清洗",)),
    ("安全检查", ("试水", "演练", "耐压试验", "检漏", "试验", "安全", "防护", "应急", "LOTO")),
    ("耗材更换", ("更换", "换油", "替换", "滤芯")),
    ("PM 项", ("检查", "巡检", "点检", "保养", "维护", "寿命")),
)


def _item_type_of(sentence: str, cycle: _Cycle) -> str:
    """按**离周期最近**的动作词归类计划项类型。

    只看句子容易出错（如「排气过滤器 4000 h 更换；每 8000 h 检查轴承」同句），
    因此以周期位置为锚点，取距离最小的动作词。
    """

    best: tuple[int, int, str] | None = None
    for index, (label, words) in enumerate(_TYPE_RULES):
        for word in words:
            for position in _positions(sentence, word):
                distance = abs(position - cycle.start)
                key = (distance, index)
                if best is None or key < (best[0], best[1]):
                    best = (distance, index, label)
    if best is not None and best[0] <= 40:
        return best[2]
    # 兜底：整句扫描（按规则优先级）
    for label, words in _TYPE_RULES:
        if any(word in sentence for word in words):
            return label
    return "PM 项"


def _positions(sentence: str, word: str) -> list[int]:
    """`word` 在句子里出现的全部位置。"""

    positions: list[int] = []
    start = sentence.find(word)
    while start >= 0:
        positions.append(start)
        start = sentence.find(word, start + 1)
    return positions


# --------------------------------------------------------------------------- #
# ③ 设备维护项清单（PM 模板）—— 用来判定「哪些项知识库给不出依据」
# --------------------------------------------------------------------------- #
#
# 说明：本清单是**待核对的 PM 项名称**，不是周期来源。周期只能来自知识库；
# 清单里的项如果在知识库里找不到周期数值，就进 `uncovered`（如实说明缺什么资料），
# 而不是编一个周期塞进计划。清单同时用于按项发起**定向检索**，避免一次泛检索
# 漏掉冷门条款（如油雾过滤器、消音器）。


@dataclass(frozen=True)
class _Candidate:
    name: str
    keywords: tuple[str, ...]
    query: str
    prefer: str


_CANDIDATES: tuple[_Candidate, ...] = (
    _Candidate(
        "日常点检",
        # 注意：不能用「点检」当关键词 —— 会被「重点检查」这种写法误命中
        ("日常点检", "点检项目", "点检表", "首件点检"),
        "{device} 日常点检 项目 周期 班次 点检表",
        BASIS_DAYS,
    ),
    _Candidate(
        "干泵排气过滤器更换",
        ("排气过滤器", "fl-600"),
        "{device} 干泵 排气过滤器 FL-600 更换周期 运行小时",
        BASIS_HOURS,
    ),
    _Candidate(
        "罗茨泵换油",
        ("换油", "泵油", "rp-300"),
        "{device} 罗茨泵 换油周期 真空泵油 运行小时",
        BASIS_HOURS,
    ),
    _Candidate(
        "分子泵轴承检查",
        ("分子泵", "轴承"),
        "{device} 分子泵 轴承 检查周期 月",
        BASIS_DAYS,
    ),
    _Candidate(
        "腔体门 O-ring 更换",
        ("腔体门 o-ring", "腔体门 o型圈", "腔体门密封"),
        "{device} 腔体门 O-ring 更换周期 开腔 月",
        BASIS_DAYS,
    ),
    _Candidate(
        "气体管路 O-ring 更换",
        ("气体管路 o-ring", "管路 o型圈", "管路密封"),
        "{device} 气体管路 O-ring 更换周期 拆装 月",
        BASIS_DAYS,
    ),
    _Candidate(
        "油雾过滤器更换",
        ("油雾过滤器",),
        "{device} 油雾过滤器 更换 运行小时",
        BASIS_HOURS,
    ),
    _Candidate(
        "消音器检查",
        ("消音器",),
        "{device} 消音器 检查 运行小时",
        BASIS_HOURS,
    ),
    _Candidate(
        "电容薄膜规（真空压力传感器）校准",
        ("薄膜规", "压力传感器"),
        "{device} 电容薄膜规 压力传感器 校准周期 天 零点漂移",
        BASIS_DAYS,
    ),
    _Candidate(
        "质量流量控制器（MFC）校准",
        ("mfc", "质量流量控制器"),
        "{device} MFC 质量流量控制器 校准 周期 年",
        BASIS_DAYS,
    ),
    _Candidate(
        "原位等离子清洗",
        ("原位等离子清洗", "等离子清洗", "原位清洗"),
        "{device} 原位等离子清洗 周期 片数 晶圆",
        BASIS_WAFERS,
    ),
    _Candidate(
        "开腔湿法清洗",
        ("开腔湿法清洗", "湿法清洗", "开腔清洗"),
        "{device} 开腔湿法清洗 周期 射频小时 累计片数",
        BASIS_HOURS,
    ),
    _Candidate(
        "静电卡盘（ESC）温度校准",
        ("esc", "静电卡盘", "晶圆卡盘"),
        "{device} 静电卡盘 ESC 温度 校准 周期",
        BASIS_DAYS,
    ),
    _Candidate(
        "冷却水系统点检",
        ("冷却水", "水路"),
        "{device} 冷却水 点检 流量 周期 检查",
        BASIS_DAYS,
    ),
    _Candidate(
        "尾气处理装置（Scrubber）滤芯更换",
        ("scrubber", "尾气处理", "尾气"),
        "{device} 尾气处理 Scrubber 滤芯 更换周期",
        BASIS_HOURS,
    ),
)


def _candidates_for(device_model: str) -> tuple[_Candidate, ...]:
    return tuple(
        _Candidate(
            name=candidate.name,
            keywords=candidate.keywords,
            query=candidate.query.format(device=device_model),
            prefer=candidate.prefer,
        )
        for candidate in _CANDIDATES
    )


# --------------------------------------------------------------------------- #
# ④ 检索证据池
# --------------------------------------------------------------------------- #


def _block_matches_device(block: Any, device_model: str) -> bool:
    """兜底校验：切片必须属于该设备或「通用」资料。

    元数据过滤已由 `build_filters` 承担，这一层防止检索器忽略 filters 时
    把别的设备的周期条款安到本设备上（用错周期会直接损坏设备）。
    """

    meta = getattr(block, "metadata", None) or {}
    model = str(meta.get("device_model") or "")
    return not model or model == device_model or model == GENERIC_MODEL


async def _evidence_pool(device_model: str, k: int) -> list[Any]:
    """检索该设备（含「通用」资料）的 PM / 周期条款并去重。

    多路检索式合并，按 chunk_id 去重（保留先到的精排结果）；过程内缓存 TTL 内复用。
    """

    key = (device_model, k)
    cached = _EVIDENCE_CACHE.get(key)
    now = time.monotonic()
    if cached is not None and now - cached[0] < _EVIDENCE_TTL_S:
        return list(cached[1])

    filters = build_filters(device_model=device_model)
    pool: dict[str, Any] = {}
    errors: list[str] = []
    for query in _queries(device_model):
        try:
            hits = await kb_search(query, filters=filters, k=k)
        except Exception as exc:  # noqa: BLE001 - 检索失败降级为「查不到」，不编造
            errors.append(str(exc))
            logger.warning("维护计划检索失败（%s）：%s", query, exc)
            continue
        for block in hits:
            if _block_matches_device(block, device_model):
                pool.setdefault(block.chunk_id, block)

    if not pool and errors:
        raise ServiceError(
            503,
            "KB_UNAVAILABLE",
            "知识库检索失败，无法生成带依据的维护计划",
            {"errors": errors[:3]},
        )

    blocks = list(pool.values())
    _EVIDENCE_CACHE[key] = (now, blocks)
    return list(blocks)


async def _corpus_text() -> str:
    """知识库全文（用于区分「未收录」与「收录了但没给周期」）；取不到时返回空串。"""

    try:
        return await kb_corpus_text()
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取知识库全文失败：%s", exc)
        return ""


def _matched_sentences(candidate: _Candidate, pool: Sequence[Any]) -> list[str]:
    """证据池里命中该候选项关键词的句子（不要求含周期）。"""

    hits: list[str] = []
    for block in pool:
        for sentence in _sentences(block.text or ""):
            lowered = sentence.lower()
            if any(keyword in lowered for keyword in candidate.keywords):
                hits.append(sentence)
    return hits


def _miss_reason(candidate: _Candidate, pool: Sequence[Any], corpus: str) -> str:
    """候选项没能生成计划项时，如实说明原因（不编造周期，但要说清缺什么）。"""

    if not pool:
        return "检索未命中该设备的维护条款（知识库可能未收录该设备）"
    matched = _matched_sentences(candidate, pool)
    if any(_has_frequency_only(sentence) for sentence in matched):
        return "知识库只给出执行频次（每班次 / 每次开腔 / 每批），没有可换算的周期数值"
    corpus_lower = (corpus or "").lower()
    if corpus_lower and all(keyword not in corpus_lower for keyword in candidate.keywords):
        return "知识库未收录该维护项，请补充手册资料"
    if matched:
        return "知识库提到该维护项，但没有可换算的周期数值"
    return "未检索到该维护项的周期条款"


# --------------------------------------------------------------------------- #
# ⑤ 候选项 -> 计划项
# --------------------------------------------------------------------------- #


def _pick_cycle(
    sentence: str, cycles: Sequence[_Cycle], keywords: Sequence[str], prefer: str
) -> _Cycle | None:
    """在候选项命中的句子里，挑出**归属于该项目**的那条周期。

    归属规则：取关键词之后最近的周期；都取不到时退化为句子末尾的周期。
    这样「排气过滤器 4000 h；消音器 8000 h」同句时不会张冠李戴。
    命中多条时优先 `prefer` 口径（如清洗项优先片数）。
    """

    lowered = sentence.lower()
    positions = [lowered.find(keyword) for keyword in keywords if keyword in lowered]
    if not positions:
        return None
    anchor = min(positions)
    after = sorted((c for c in cycles if c.start >= anchor), key=lambda c: c.start)
    ordered = after or sorted(cycles, key=lambda c: -c.start)
    for cycle in ordered:
        if cycle.basis == prefer:
            return cycle
    return ordered[0]


@dataclass(frozen=True)
class _Hit:
    """候选项命中的证据与周期。"""

    block: Any
    sentence: str
    cycles: tuple[_Cycle, ...]
    cycle: _Cycle

    @property
    def alternates(self) -> tuple[_Cycle, ...]:
        """**同一条并列条款**里给出的其他口径周期。

        例：「换油周期为 4000 运行小时或 12 个月，以先到者为准」-> 主周期 4000 运行小时，
        备选 12 个月；「累计射频小时达到 1500 h，或累计生产达到 8000 片」同理。
        同句但属于另一项（`；` 分隔）的周期不算备选，避免张冠李戴。
        """

        start, end = _clause_span_of(self.sentence, self.cycle)
        return tuple(item for item in self.cycles if item is not self.cycle and start <= item.start < end)


#: 条款分隔符：分号切出来的一段就是一个条款
#: （如 `…更换周期每 24 小时或每 200 片，以先到者为准`）
_CLAUSE_SPLIT = re.compile(r"[；;]+")


def _clause_spans(sentence: str) -> list[tuple[int, int]]:
    """把句子按 `；` 切成多个条款，返回各条款的 [start, end) 区间。"""

    spans: list[tuple[int, int]] = []
    start = 0
    for match in _CLAUSE_SPLIT.finditer(sentence):
        spans.append((start, match.start()))
        start = match.end()
    spans.append((start, len(sentence)))
    return [(s, e) for s, e in spans if sentence[s:e].strip()]


def _clause_span_of(sentence: str, cycle: _Cycle) -> tuple[int, int]:
    """周期所在条款的 [start, end)。"""

    for start, end in _clause_spans(sentence):
        if start <= cycle.start < end:
            return start, end
    return 0, len(sentence)


def _find_hit(candidate: _Candidate, pool: Sequence[Any]) -> _Hit | None:
    """在证据池里找该候选项的周期条款（找到多条时优先口径匹配的那条）。"""

    hits: list[_Hit] = []
    for block in pool:
        for sentence in _sentences(block.text or ""):
            lowered = sentence.lower()
            if not any(keyword in lowered for keyword in candidate.keywords):
                continue
            if not _is_cycle_sentence(sentence):
                continue
            cycles = _extract_cycles(sentence)
            if not cycles:
                continue
            picked = _pick_cycle(sentence, cycles, candidate.keywords, candidate.prefer)
            if picked is None:
                continue
            hits.append(_Hit(block=block, sentence=sentence, cycles=tuple(cycles), cycle=picked))
    if not hits:
        return None
    for hit in hits:
        if hit.cycle.basis == candidate.prefer:
            return hit
    return hits[0]


# --------------------------------------------------------------------------- #
# ⑥ 计算与视图
# --------------------------------------------------------------------------- #


def _compute(
    cycle: _Cycle,
    *,
    runtime_hours: float,
    wafer_count: float,
    hours_since_pm: float | None,
    wafers_since_pm: float | None,
    now: datetime,
) -> dict[str, Any]:
    """按口径推算剩余量、基线与到期时间（算法见模块 docstring）。"""

    if cycle.basis == BASIS_HOURS:
        used = float(hours_since_pm) if hours_since_pm is not None else float(runtime_hours or 0.0)
        remaining = cycle.value - used
        current = float(runtime_hours or 0.0)
        baseline = current - used
        # 按 24 小时连续运行折算天数；已超期则到期时间就是现在
        due = now if remaining < 0 else now + timedelta(days=max(0.0, remaining) / _HOURS_PER_DAY)
    elif cycle.basis == BASIS_WAFERS:
        used = float(wafers_since_pm) if wafers_since_pm is not None else float(wafer_count or 0.0)
        remaining = cycle.value - used
        current = float(wafer_count or 0.0)
        baseline = current - used
        # 不知道产线速率（片/天）-> 不猜，如实留空
        due = None
    else:  # BASIS_DAYS：自然日口径，从今天起算一个完整周期
        remaining = cycle.value
        current = 0.0
        baseline = 0.0
        due = now + timedelta(days=cycle.value)

    return {
        "remaining": round(remaining, 3),
        "current_value": round(current, 3),
        "baseline_value": round(baseline, 3),
        "due_at": due,
        "status": "due" if remaining <= 0 else "planned",
    }


def _note_for(citation: Any, cycle: _Cycle, alternates: Sequence[_Cycle]) -> str:
    """计划项 note：写明来源（文档/版本/页码/章节）与周期原文，供人工复核。"""

    note = f"由知识库生成，依据《{getattr(citation, 'doc', None) or '未知文档'}》"
    if getattr(citation, "version", None):
        note += f" {citation.version}"
    if getattr(citation, "page", None):
        note += f" 第 {citation.page} 页"
    if getattr(citation, "section", None):
        note += f" 章节 {citation.section}"
    note += f"；周期原文「{cycle.raw}」"
    if cycle.factor != 1.0:
        note += (
            f"，已按 1{cycle.source_unit}={cycle.factor:g} 天折算为 {cycle.value:g} 天"
            "（折算系数为工程约定，非知识库原文）"
        )
    if alternates:
        raws = list(dict.fromkeys(item.raw for item in alternates))
        note += "；知识库同时给出：" + "、".join(raws) + "（以先到者为准）"
    return note


def _build_item(
    *,
    name: str,
    item_type: str,
    block: Any,
    cycle: _Cycle,
    alternates: Sequence[_Cycle],
    device_model: str,
    device_code: str,
    payload: PlanGenerateRequest,
    now: datetime,
) -> PlanItem:
    """组装一条计划项（引用 = evidence_to_citations([该片段])，与问答同源）。"""

    citations = evidence_to_citations([block])
    computed = _compute(
        cycle,
        runtime_hours=payload.runtime_hours,
        wafer_count=float(payload.wafer_count),
        hours_since_pm=payload.hours_since_pm,
        wafers_since_pm=float(payload.wafers_since_pm) if payload.wafers_since_pm is not None else None,
        now=now,
    )
    return PlanItem(
        id=None,
        device_model=device_model,
        device_code=device_code,
        item_name=name[:255],
        item_type=item_type,
        cycle_basis=cycle.basis,
        cycle_value=cycle.value,
        baseline_value=computed["baseline_value"],
        current_value=computed["current_value"],
        remaining=computed["remaining"],
        due_at=_iso(computed["due_at"]),
        status=computed["status"],
        evidence=citations,
        chunk_id=block.chunk_id,
        note=_note_for(citations[0], cycle, alternates),
    )


def _generic_items(
    pool: Sequence[Any],
    candidates: Sequence[_Candidate],
    used: set[tuple[str, str]],
    *,
    device_model: str,
    device_code: str,
    payload: PlanGenerateRequest,
    now: datetime,
    taken_names: set[str],
) -> list[PlanItem]:
    """候选清单之外的补充项：知识库里还有周期条款的，也照原文生成（名称从原文推导）。

    粒度为**条款**（`；` 分隔的一段）：一个条款出一条计划项，主周期取该条款里第一个
    周期，并列口径（「或…以先到者为准」）写进 note。
    已被候选项用掉的句子、命中任何候选关键词的句子整句跳过，避免重复与张冠李戴。
    """

    items: list[PlanItem] = []
    all_keywords = tuple(keyword for candidate in candidates for keyword in candidate.keywords)
    for block in pool:
        meta = getattr(block, "metadata", None) or {}
        for sentence in _sentences(getattr(block, "text", "") or ""):
            if (block.chunk_id, sentence) in used:
                continue
            lowered = sentence.lower()
            if any(keyword in lowered for keyword in all_keywords):
                continue
            if not _is_cycle_sentence(sentence):
                continue
            cycles = _extract_cycles(sentence)
            if not cycles:
                continue
            for start, end in _clause_spans(sentence):
                in_clause = [c for c in cycles if start <= c.start < end]
                if not in_clause:
                    continue
                cycle = in_clause[0]
                name = _derive_name(
                    sentence,
                    cycle,
                    heading=str(meta.get("heading") or ""),
                    doc_title=str(meta.get("doc_title") or ""),
                )
                key = f"{name}|{cycle.basis}|{cycle.value:g}"
                if key in taken_names:
                    continue
                taken_names.add(key)
                items.append(
                    _build_item(
                        name=name,
                        item_type=_item_type_of(sentence, cycle),
                        block=block,
                        cycle=cycle,
                        alternates=tuple(c for c in in_clause if c is not cycle),
                        device_model=device_model,
                        device_code=device_code,
                        payload=payload,
                        now=now,
                    )
                )
    return items


# --------------------------------------------------------------------------- #
# ⑦ 落库与读取
# --------------------------------------------------------------------------- #


def _plan_view(row: MaintenancePlan) -> dict[str, Any]:
    """`maintenance_plan` 行 -> `PlanItem` 可直接校验的 dict。"""

    evidence = [dict(item) for item in (row.evidence or []) if isinstance(item, dict)]
    return {
        "id": _as_int(row.id) or None,
        "device_model": str(row.device_model or ""),
        "device_code": str(row.device_code or ""),
        "item_name": str(row.item_name or ""),
        "item_type": str(row.item_type or "PM 项"),
        "cycle_basis": str(row.cycle_basis or BASIS_HOURS),
        "cycle_value": round(_as_float(row.cycle_value), 3),
        "baseline_value": round(_as_float(row.baseline_value), 3),
        "current_value": round(_as_float(row.current_value), 3),
        "remaining": round(_as_float(row.remaining), 3),
        "due_at": _iso(row.due_at),
        "status": str(row.status or "planned"),
        "evidence": evidence,
        "chunk_id": str(row.chunk_id or ""),
        "note": str(row.note or ""),
        # 时间戳（UTC ISO）；completed_at 仅完成/跳过后有值
        "created_at": _iso(getattr(row, "created_at", None)),
        "updated_at": _iso(getattr(row, "updated_at", None)),
        "completed_at": _iso(getattr(row, "completed_at", None)),
    }


async def _insert_items(items: Sequence[PlanItem]) -> None:
    """把预览出来的计划项落库（不删旧数据；返回时回填 id）。"""

    async with db.session_scope() as session:
        for item in items:
            row = MaintenancePlan(
                device_model=item.device_model,
                device_code=item.device_code,
                item_name=item.item_name,
                item_type=item.item_type,
                cycle_basis=item.cycle_basis,
                cycle_value=item.cycle_value,
                baseline_value=item.baseline_value,
                current_value=item.current_value,
                remaining=item.remaining,
                due_at=_parse_iso(item.due_at),
                status=item.status,
                evidence=[citation.model_dump() for citation in item.evidence],
                chunk_id=item.chunk_id,
                note=item.note,
            )
            session.add(row)
            await session.flush()
            item.id = _as_int(row.id) or None


# --------------------------------------------------------------------------- #
# ⑧ 对外方法（契约）
# --------------------------------------------------------------------------- #


async def generate_plans(payload: PlanGenerateRequest, *, trace_id: str) -> dict:
    """按设备生成维护计划（POST /plans/generate）。

    * 周期数值只从知识库原文抽取；抽不到依据的项进 `uncovered`，不进 `items`。
    * `payload.persist=False` 只预览不落库；`True` 时插入 `maintenance_plan`
      （**不删旧数据**，重复生成会追加新行，note 里写明来源）。
    * `payload.top_k` 限制生成条数；检索用 `k = max(12, top_k)` 保证召回面。
    """

    _require_db()

    device_model = (payload.device_model or "").strip()
    if not device_model:
        raise ServiceError(400, "INVALID_ARGUMENT", "device_model 不能为空")
    device_code = (payload.device_code or "").strip()
    top_k = max(1, _as_int(payload.top_k, 8))

    pool = await _evidence_pool(device_model, min(20, max(12, top_k)))
    corpus = await _corpus_text()
    candidates = _candidates_for(device_model)
    now = _now()

    items: list[PlanItem] = []
    uncovered: list[str] = []
    used: set[tuple[str, str]] = set()
    taken_names: set[str] = set()

    for candidate in candidates:
        hit = _find_hit(candidate, pool)
        if hit is None:
            uncovered.append(f"{candidate.name}：{_miss_reason(candidate, pool, corpus)}")
            continue
        used.add((hit.block.chunk_id, hit.sentence))
        taken_names.add(f"{candidate.name}|{hit.cycle.basis}|{hit.cycle.value:g}")
        items.append(
            _build_item(
                name=candidate.name,
                item_type=_item_type_of(hit.sentence, hit.cycle),
                block=hit.block,
                cycle=hit.cycle,
                alternates=hit.alternates,
                device_model=device_model,
                device_code=device_code,
                payload=payload,
                now=now,
            )
        )

    if len(items) < top_k:
        items.extend(
            _generic_items(
                pool,
                candidates,
                used,
                device_model=device_model,
                device_code=device_code,
                payload=payload,
                now=now,
                taken_names=taken_names,
            )
        )

    items = items[:top_k]
    if payload.persist and items:
        await _insert_items(items)

    logger.info(
        "维护计划生成：%s/%s -> %d 条（未覆盖 %d 项，persist=%s，trace_id=%s）",
        device_model,
        device_code or "-",
        len(items),
        len(uncovered),
        payload.persist,
        trace_id,
    )
    response = PlanGenerateResponse(
        device_model=device_model,
        device_code=device_code,
        items=items,
        uncovered=uncovered,
        trace_id=trace_id,
    )
    return response.model_dump()


async def list_plans(
    *,
    device_model: str | None,
    status: str | None,
    limit: int,
    offset: int,
    trace_id: str,
) -> dict:
    """维护计划列表（GET /plans）：按 `remaining` 升序（最紧急在前）分页。"""

    _require_db()

    model = (device_model or "").strip()
    wanted = (status or "").strip().lower()
    if wanted and wanted not in PLAN_STATUSES:
        raise ServiceError(
            400,
            "INVALID_ARGUMENT",
            f"status 取值非法：{status}（允许 {'/'.join(PLAN_STATUSES)}）",
            {"allowed": list(PLAN_STATUSES)},
        )
    limit, offset = _page(limit, offset)

    conditions = []
    if model:
        conditions.append(MaintenancePlan.device_model == model)
    if wanted:
        conditions.append(MaintenancePlan.status == wanted)

    async with db.session_scope() as session:
        total = int(
            (
                await session.execute(
                    select(func.count()).select_from(MaintenancePlan).where(*conditions)
                )
            ).scalar_one()
            or 0
        )
        rows = (
            (
                await session.execute(
                    select(MaintenancePlan)
                    .where(*conditions)
                    .order_by(MaintenancePlan.remaining.asc(), MaintenancePlan.id.asc())
                    .limit(limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )

    return {
        "items": [_plan_view(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "trace_id": trace_id,
    }


async def complete_plan(plan_id: int, payload: PlanUpdateRequest, *, trace_id: str) -> dict | None:
    """完成一条计划项（POST /plans/{plan_id}/complete）；不存在返回 None（路由 404）。

    完成即**滚动到下一个周期**：`status="done"`、`completed_at=now`；
    `completed_value` 给了就作为新基线（缺省按旧基线 + 一个周期推进），
    `remaining` 回到 `cycle_value`，`due_at` 按口径重算（片数口径没有速率仍留空）。
    """

    _require_db()

    now = _now()
    note = ""
    completed: float | None = None
    if payload is not None:
        note = (payload.note or "").strip()
        if payload.completed_value is not None:
            completed = _as_float(payload.completed_value)

    async with db.session_scope() as session:
        row = await session.get(MaintenancePlan, _as_int(plan_id))
        if row is None:
            return None

        basis = str(row.cycle_basis or BASIS_HOURS)
        cycle_value = _as_float(row.cycle_value)
        if basis == BASIS_DAYS:
            row.baseline_value = 0.0
            due_at: datetime | None = now + timedelta(days=cycle_value)
        else:
            if completed is not None:
                row.baseline_value = completed
                row.current_value = completed
            else:
                row.baseline_value = _as_float(row.baseline_value) + cycle_value
            # hours 口径按 24 小时连续运行折算；wafers 口径不知道速率 -> 留空
            due_at = (
                now + timedelta(days=cycle_value / _HOURS_PER_DAY)
                if basis == BASIS_HOURS
                else None
            )

        row.status = "done"
        row.completed_at = now
        row.remaining = cycle_value
        row.due_at = due_at
        row.updated_at = now

        extra = (
            f"已完成维护（{_iso(now)}），已滚动到下一周期：剩余 {cycle_value:g} "
            f"{_unit_label(basis) or ''}".rstrip()
        )
        if completed is not None:
            extra += f"，本次回填计量值 {completed:g}"
        if due_at is None:
            extra += "；片数口径缺少产线速率，下次到期时间未估算（due_at 留空）"
        row.note = _append_note(_append_note(str(row.note or ""), extra), note)
        view = _plan_view(row)

    logger.info("维护计划已完成：id=%s -> 下一周期 %s（trace_id=%s）", plan_id, view["due_at"], trace_id)
    return view


async def skip_plan(plan_id: int, payload: PlanUpdateRequest, *, trace_id: str) -> dict | None:
    """跳过一条计划项（POST /plans/{plan_id}/skip）；不存在返回 None（路由 404）。

    只改状态与备注（`status="skipped"`，note 追加原因），不改周期与到期时间。
    """

    _require_db()

    reason = ""
    if payload is not None:
        reason = (payload.note or "").strip()
    now = _now()

    async with db.session_scope() as session:
        row = await session.get(MaintenancePlan, _as_int(plan_id))
        if row is None:
            return None

        row.status = "skipped"
        row.updated_at = now
        row.note = _append_note(
            str(row.note or ""), f"已跳过：{reason}" if reason else "已跳过（未填写原因）"
        )
        view = _plan_view(row)

    logger.info("维护计划已跳过：id=%s（%s，trace_id=%s）", plan_id, reason or "未填写原因", trace_id)
    return view


__all__ = [
    "BASIS_DAYS",
    "BASIS_HOURS",
    "BASIS_WAFERS",
    "CYCLE_BASES",
    "GENERIC_MODEL",
    "PLAN_STATUSES",
    "complete_plan",
    "generate_plans",
    "list_plans",
    "reset_plan_cache",
    "skip_plan",
]
