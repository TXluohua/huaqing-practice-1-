"""考核认证服务（开发文档 6.2 的 training_service.py：出题、判分、认证记录与到期提醒）。

冻结契约（backend/routers/__init__.py 的 `_TRAINING_SERVICE_API`，签名不要改）
--------------------------------------------------------------------------------
    generate_quiz(payload, *, trace_id)                       -> dict（QuizResponse 可校验）
    get_quiz(quiz_id, *, trace_id)                            -> dict | None
    submit_attempt(quiz_id, payload, *, trace_id)             -> dict | None
    issue_certification(payload, *, trace_id)                 -> dict（CertificationResponse）
    list_certifications(*, trainee, device_model, status, limit, offset, trace_id) -> dict
    list_expiring(*, days, trace_id)                          -> dict

零幻觉贯穿到培训环节
--------------------
1. **出题必须先检索**：题目只允许来自 `kb_search` 返回的知识片段；
2. LLM 路径产出、但没有给出合法 `evidence_index` 的题目**一律丢弃**（不猜是哪一段）；
3. LLM 产出的题干/选项/答案里出现「该片段原文里查不到的数值」也丢弃
   （防止模型用编造的干扰项，例如把 2.0×10⁻⁴ 编成 1.5×10⁻⁴）；
4. LLM 不可用、调用失败、返回非法 JSON、或所有题都被丢弃时，走**确定性抽句降级**
   （只用原文句子 + 同片段内的其它数值做干扰项），`generator="extractive-fallback"`；
5. 一道题都生成不出来时抛 `ServiceError(422, "QUIZ_GENERATION_FAILED")`
   —— 宁可不出题，也不出无依据的题。

判分与发证的边界
----------------
- 判分（`submit_attempt`）**不自动发证**：`certification_id` 恒为 None。发证必须显式调用
  `issue_certification`，因为发证要指定等级（L1/L2/L3）与有效期（`valid_days`），
  这两项属于授权决策，不能由「提交答卷」这一步替用户默认掉。
- 系统**不代表原厂签发认证**：`issuer` 默认「内部授权」，note 里固定标注该口径。
- 模块常量 `PASS_LINE` 是及格线（百分制），按需调整；判分与 `passed` 都读它。
"""

from __future__ import annotations

import logging
import math
import random
import re
import zlib
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select

from .. import db
from ..agents.state import Evidence
from ..rag.retriever import evidence_to_citations
from ..schemas import (
    CertificationRequest,
    QuizGenerateRequest,
    QuizSubmitRequest,
    ServiceError,
)
from ..setting import get_settings
from ..tools.kb_tools import kb_search
from . import llm_client

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 常量（阈值集中在模块顶部，便于「调参不改代码」NFR-04）
# --------------------------------------------------------------------------- #

#: 及格线（百分制）。**可调**：改这一个常量即可，判分、passed、发证校验都读它。
PASS_LINE = 60.0

#: 支持的题型（与 schemas.QuizGenerateRequest.question_types 的说明一致）
_ALL_TYPES: tuple[str, ...] = ("single", "multiple", "judgement", "short")
#: 请求里没给题型时的默认组合（确定性、可离线生成）
_DEFAULT_TYPES: tuple[str, ...] = ("single", "judgement")

#: 检索片段条数上下限（要求 6~8）
_MIN_EVIDENCE_K = 6
_MAX_EVIDENCE_K = 8

#: 出题检索式后缀（对齐开发文档的「要求 / 参数 / 周期 / 判定标准」检索意图）
_QUERY_SUFFIX = "要求 参数 周期 判定标准"

#: 简答题：单个要点与学员文本的字符二元组重合度阈值（含 0.5 记命中）
_SHORT_HIT_THRESHOLD = 0.5
#: 简答题：命中率阈值（达到才算该题正确）
_SHORT_PASS_RATIO = 0.6

#: 降级路径的句子长度上下限（太短没信息量，太长不适合做题干）
_MIN_SENTENCE_LEN = 10
_MAX_SENTENCE_LEN = 200
#: 降级路径各题型的题干上限：填空题短一些更易读，选项句更要短
_MAX_STEM_LEN = 120
#: 判断题/简答题的句子上限（太长不适合做题干）
_MAX_STATEMENT_LEN = 160
#: 多选题单个选项的上限
_MAX_MULTIPLE_OPTION_LEN = 80

#: 到期提醒回溯窗口（天）：已过期但还在这个窗口内的也要提醒
_EXPIRING_LOOKBACK_DAYS = 30
#: 到期提醒单次返回上限（防止无限增长时把响应撑爆）
_EXPIRING_LIMIT = 500

#: 数值 token：排除型号/编码里的数字（DP-600 / E-2041 / SP-ETA-0101 / V3.2 / RP-300）
#: num  可带科学计数法（2.0×10⁻⁴，指数允许上标数字）
#: unit 数值后紧随的单位词（最多 6 个非数字字符，如「 运行小时」「 L」「% F.S.」）
_NUM_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9\-])"
    r"(?P<num>\d+(?:\.\d+)?(?:\s*[×xX]\s*10\s*[⁻\-^]?\s*[0-9⁰¹²³⁴⁵⁶⁷⁸⁹]+)?)"
    r"(?P<unit>\s*[^\d\s，。；、：:（）()\[\]「」“”\"'~\-]{0,6})"
)

#: 切句：中文句末标点 + 换行
_SENTENCE_SPLIT_RE = re.compile(r"[。；;！？!?\n]+")
#: 行首的列表符号 / 序号（re.M：每一行都要剥掉，否则选项/题干会残留 "- "）
_BULLET_RE = re.compile(r"^\s*(?:[-*•·]|\d+[.、)])\s*", re.MULTILINE)
#: 空白归一（比对用）
_WS_RE = re.compile(r"\s+")


# --------------------------------------------------------------------------- #
# 通用小工具
# --------------------------------------------------------------------------- #


def _require_db() -> None:
    """数据库不可用时给 503，而不是让调用方看到 500。"""

    if not db.is_available():
        raise ServiceError(503, "DB_UNAVAILABLE", "数据库不可用，无法进行考核认证操作")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    """时间统一按 UTC ISO8601 输出（SQLite 读回的 naive 时间按 UTC 处理）。"""

    if value is None:
        return None
    moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def _norm(text: str) -> str:
    """去掉全部空白（数值与文本比对用）。"""

    return _WS_RE.sub("", (text or "").strip())


def _bigrams(text: str) -> set[str]:
    """字符二元组集合（中文下比词重合更稳，且不依赖分词器）。"""

    return {text[i : i + 2] for i in range(len(text) - 1)} if len(text) > 1 else {text}


def _bigram_overlap(point: str, answer_text: str) -> float:
    """要点与学员答案的字符二元组重合度（分母取**要点**的二元组数）。"""

    a = _bigrams(_norm(point))
    b = _bigrams(_norm(answer_text))
    if not a:
        return 0.0
    return len(a & b) / len(a)


def _pick(values: Sequence[Any], seed: str) -> Any:
    """确定性取一个元素（不用全局 random，保证同一输入每次结果一致）。"""

    if not values:
        return None
    return values[zlib.crc32(seed.encode("utf-8")) % len(values)]


def _shuffle_deterministic(values: Sequence[Any], seed: str) -> list[Any]:
    """确定性打乱（选项顺序稳定可复现，便于复核与回归）。"""

    shuffled = list(values)
    random.Random(zlib.crc32(seed.encode("utf-8"))).shuffle(shuffled)
    return shuffled


# --------------------------------------------------------------------------- #
# 文本 → 句 / 数值
# --------------------------------------------------------------------------- #


def _sentences(text: str) -> list[str]:
    """把切片正文切成句子（去行首列表符号，丢弃过短/过长片段）。"""

    cleaned = _BULLET_RE.sub("", text or "")
    out: list[str] = []
    for raw in _SENTENCE_SPLIT_RE.split(cleaned):
        sentence = _WS_RE.sub(" ", raw.replace("**", "").replace("*", "")).strip()
        if len(sentence) < _MIN_SENTENCE_LEN or len(sentence) > _MAX_SENTENCE_LEN:
            continue
        out.append(sentence)
    return out


def _num_tokens(text: str) -> list[re.Match[str]]:
    """取文本中的「数值 + 单位」token（排除型号/编码里的数字）。"""

    return list(_NUM_TOKEN_RE.finditer(text or ""))


def _number_key(num: str) -> str:
    """数值归一：`2.0×10⁻⁴` 与 `2.0` 视为同一个被引用数值（只比十进制主体）。"""

    return _norm(num).split("×")[0].split("x")[0].split("X")[0]


def _numbers_in(texts: Iterable[str], *, exclude: str = "") -> list[str]:
    """收集文本里出现过的数值（去重、保序、可排除某个数值）。"""

    banned = _number_key(exclude)
    seen: list[str] = []
    keys: set[str] = set()
    for text in texts:
        for match in _num_tokens(text):
            num = _WS_RE.sub("", match.group("num"))
            key = _number_key(num)
            if not key or key == banned or key in keys:
                continue
            keys.add(key)
            seen.append(num)
    return seen


def _distractor_numbers(blocks: Sequence[Evidence], block_index: int, *, exclude: str) -> list[str]:
    """干扰项数值：优先同片段，其次本次检索到的其它片段（全部来自知识库原文）。"""

    ordered: list[str] = list(_numbers_in([blocks[block_index - 1].text], exclude=exclude))
    for index, block in enumerate(blocks, start=1):
        if index == block_index:
            continue
        for num in _numbers_in([block.text], exclude=exclude):
            if num not in ordered:
                ordered.append(num)
    return ordered[:3]


def _replace_number(sentence: str, match: re.Match[str], new_num: str) -> str:
    """把句子里的某个数值换成另一个数值（只换 num 部分，单位保持不变）。"""

    return f"{sentence[: match.start('num')]}{new_num}{sentence[match.end('num'):]}"


def _source_label(block: Evidence) -> str:
    """《文档》版本 第 N 页 章节 X —— 题目与解释里的出处标注。"""

    meta = block.metadata or {}
    parts = [f"《{meta.get('doc_title', '')}》"]
    if meta.get("version"):
        parts.append(str(meta["version"]))
    parts.append(f"第 {meta.get('page', 0)} 页")
    if meta.get("section"):
        parts.append(f"章节 {meta['section']}")
    return " ".join(parts)


def _evidence_for(blocks: Sequence[Evidence], block_index: int) -> list[dict[str, Any]]:
    """把某一片段转成该题的 evidence（照 retriever.evidence_to_citations 的用法）。

    引用编号回填为**片段序号**（1 开始），与出题时给模型的 [n] 编号一致，
    这样「题目凭什么这么出」可以按编号直接回溯到检索结果。
    """

    citations = evidence_to_citations([blocks[block_index - 1]])
    for citation in citations:
        citation.id = block_index
    return [citation.model_dump() for citation in citations]


# --------------------------------------------------------------------------- #
# 降级路径：确定性抽句出题（不依赖模型）
# --------------------------------------------------------------------------- #


def _build_judgement(
    blocks: Sequence[Evidence], block_index: int, sentence: str
) -> dict[str, Any] | None:
    """判断题：原句为真；或把其中一个数值替换成同片段内的另一个数值作为错误说法。

    真/假按句子在片段内的位置交替（同一片段连续出两道判断题时不会都是「真」）。
    """

    block = blocks[block_index - 1]
    matches = _num_tokens(sentence)
    if not matches or len(sentence) > _MAX_STATEMENT_LEN:
        return None

    statement, answer = sentence, True
    detail = ""
    sentences = _sentences(block.text)
    position = sentences.index(sentence) if sentence in sentences else 0
    target = _pick(matches, sentence)
    if position % 2 == 1 and target is not None:
        # 零幻觉：改动值只能取自**同一片段**原文，绝不引入外部数值
        others = _numbers_in([block.text], exclude=target.group("num"))
        new_num = _pick(others, f"{sentence}|swap")
        if new_num:
            statement = _replace_number(sentence, target, new_num)
            answer = False
            detail = (
                f"；题面把原文的「{_WS_RE.sub('', target.group('num'))}」"
                f"改成了「{new_num}」，与原文不符"
            )

    return {
        "question": f"判断下列说法是否正确：{statement}",
        "type": "judgement",
        "options": ["正确", "错误"],
        "answer": answer,
        "explanation": f"原文（{_source_label(block)}）为：「{sentence}」{detail}。",
        "evidence": _evidence_for(blocks, block_index),
    }


def _build_single(
    blocks: Sequence[Evidence], block_index: int, sentence: str
) -> dict[str, Any] | None:
    """单选题：就原文某个数值设空，正确项来自原文，干扰项取原文其它数值。"""

    block = blocks[block_index - 1]
    matches = _num_tokens(sentence)
    if not matches or len(sentence) > _MAX_STEM_LEN:
        return None
    match = _pick(matches, f"{block.chunk_id}|{sentence}")
    if match is None:
        return None

    correct = _WS_RE.sub("", match.group("num"))
    stem = f"{sentence[: match.start('num')]}____{sentence[match.end('num'):]}".strip()
    distractors = _distractor_numbers(blocks, block_index, exclude=correct)
    if len(distractors) < 2:  # 干扰项不足（原文没有别的数值可用）→ 不出这道题
        return None

    options = _shuffle_deterministic([correct, *distractors], f"{block.chunk_id}|{sentence}|single")
    return {
        "question": f"根据{_source_label(block)}，填空处应填写的数值是：{stem}",
        "type": "single",
        "options": options,
        "answer": options.index(correct),
        "explanation": f"原文（{_source_label(block)}）为：「{sentence}」。",
        "evidence": _evidence_for(blocks, block_index),
    }


def _alter_sentence(sentence: str, block: Evidence) -> str | None:
    """把句子里的某个数值换成同片段内的另一个数值（用于多选/判断题的错误说法）。"""

    matches = _num_tokens(sentence)
    if not matches:
        return None
    match = _pick(matches, f"{block.chunk_id}|{sentence}|alter")
    if match is None:
        return None
    others = _numbers_in([block.text], exclude=match.group("num"))
    new_num = _pick(others, f"{block.chunk_id}|{sentence}|alter-value")
    if not new_num:
        return None
    return _replace_number(sentence, match, new_num)


def _build_multiple(
    blocks: Sequence[Evidence], block_index: int, sentence: str
) -> dict[str, Any] | None:
    """多选题：选项 = 原句（真）+ 改动数值后的同段句子（假），正确项即原句下标。

    以传入的句子为锚（保证同一片段不会反复出同一道多选题）；
    选项直接是原文句子，长度控制在 `_MAX_MULTIPLE_OPTION_LEN` 以内。
    """

    block = blocks[block_index - 1]
    if len(sentence) > _MAX_MULTIPLE_OPTION_LEN:
        return None
    others = [
        item
        for item in _sentences(block.text)
        if item != sentence and len(item) <= _MAX_MULTIPLE_OPTION_LEN and _num_tokens(item)
    ]
    if not others:
        return None

    trues = [sentence, others[0]]
    falses = [altered for altered in (_alter_sentence(text, block) for text in trues) if altered]
    if not falses:
        return None

    entries = [(text, True) for text in trues] + [(text, False) for text in falses]
    if len(entries) < 3:
        return None
    shuffled = _shuffle_deterministic(entries, f"{block.chunk_id}|{sentence}|multiple")
    options = [text for text, _ in shuffled]
    answer = sorted(index for index, (_, is_true) in enumerate(shuffled) if is_true)
    truth_list = "\n".join(f"- {text}" for text in trues)
    return {
        "question": (
            f"根据{_source_label(block)}，下列关于该部分的说法中正确的有哪些？（多选）"
        ),
        "type": "multiple",
        "options": options,
        "answer": answer,
        "explanation": f"原文（{_source_label(block)}）为：\n{truth_list}\n其余选项的数值与原文不符。",
        "evidence": _evidence_for(blocks, block_index),
    }


def _short_points(sentence: str) -> list[str]:
    """抽出句子里的「数值 + 单位」作为简答题要点（去重、保序、最多 3 条）。"""

    points: list[str] = []
    keys: set[str] = set()
    for match in _num_tokens(sentence):
        token = _WS_RE.sub(" ", match.group(0)).strip()
        key = _number_key(match.group("num"))
        if not token or not key or key in keys:
            continue
        keys.add(key)
        points.append(token)
        if len(points) >= 3:
            break
    return points


def _build_short(
    blocks: Sequence[Evidence], block_index: int, sentence: str
) -> dict[str, Any] | None:
    """简答题：把原句里的数值全部挖空，答案即原文数值要点列表。"""

    block = blocks[block_index - 1]
    matches = _num_tokens(sentence)
    points = _short_points(sentence)
    if len(matches) < 2 or len(points) < 2 or len(sentence) > _MAX_STATEMENT_LEN:
        return None

    stem = sentence
    for match in reversed(matches):  # 从后往前替换，避免位移
        stem = f"{stem[: match.start('num')]}____{stem[match.end('num'):]}"
    return {
        "question": (
            f"根据{_source_label(block)}，请写出下面空缺处应填写的关键参数"
            f"（要点即可，可只写数值与单位）：{stem.strip()}"
        ),
        "type": "short",
        "options": [],
        "answer": points,
        "explanation": f"原文（{_source_label(block)}）为：「{sentence}」。",
        "evidence": _evidence_for(blocks, block_index),
    }


#: 题型 -> 降级出题函数
_FALLBACK_BUILDERS = {
    "single": _build_single,
    "judgement": _build_judgement,
    "multiple": _build_multiple,
    "short": _build_short,
}


def _fallback_items(
    blocks: Sequence[Evidence], *, n_items: int, types: Sequence[str]
) -> list[dict[str, Any]]:
    """确定性抽句出题：只用片段原文的句子与数值，顺序与选项都稳定可复现。

    题型分配：第 i 个可用句子优先出 `types[i % len(types)]`，该题型在这句上做不出来
    （例如缺干扰项数值）就按顺序退到下一个题型；**同一句子只出一道题**，
    避免一个句子反复出现在不同题里。
    """

    pool: list[tuple[int, str]] = []
    for index, block in enumerate(blocks, start=1):
        for sentence in _sentences(block.text):
            if _num_tokens(sentence):
                pool.append((index, sentence))
    if not pool:
        return []

    wanted = [qtype for qtype in types if qtype in _FALLBACK_BUILDERS] or list(_DEFAULT_TYPES)

    items: list[dict[str, Any]] = []
    for order, (block_index, sentence) in enumerate(pool):
        if len(items) >= n_items:
            break
        start = order % len(wanted)
        for offset in range(len(wanted)):
            qtype = wanted[(start + offset) % len(wanted)]
            item = _FALLBACK_BUILDERS[qtype](blocks, block_index, sentence)
            if item is not None:
                items.append(item)
                break
    return items


# --------------------------------------------------------------------------- #
# LLM 路径：只依据给定片段出题
# --------------------------------------------------------------------------- #

_QUIZ_SYSTEM_PROMPT = (
    "你是半导体设备维护培训的出题员。你的唯一依据是用户给出的知识片段。"
    "铁律：片段里没有的数字、周期、判定标准、结论一律不许出现在题目、选项与答案里；"
    "宁可不做题，也不许编造。只输出 JSON，不要任何解释性文字。"
)


def _render_snippets(blocks: Sequence[Evidence]) -> str:
    """把片段渲染成带 [n] 编号的清单（编号即 evidence_index 的取值）。"""

    lines: list[str] = []
    for index, block in enumerate(blocks, start=1):
        meta = block.metadata or {}
        lines.append(
            f"[{index}] 《{meta.get('doc_title', '')}》"
            f"{(' ' + str(meta.get('version'))) if meta.get('version') else ''}"
            f" 第 {meta.get('page', 0)} 页"
            f"{(' 章节 ' + str(meta.get('section'))) if meta.get('section') else ''}\n"
            f"{block.text.strip()}"
        )
    return "\n\n".join(lines)


def _render_quiz_prompt(
    blocks: Sequence[Evidence], *, payload: QuizGenerateRequest, types: Sequence[str]
) -> str:
    """出题提示词：编号片段 + 严格 JSON 输出要求（含 evidence_index）。"""

    applies = payload.device_model.strip() or "全库（不限型号）"
    topic = payload.topic.strip() or "该设备的基础维护要点"
    return (
        f"设备型号：{applies}\n"
        f"主题：{topic}\n"
        f"难度：{payload.level}\n"
        f"请出 {payload.n_items} 道题，题型限定为：{', '.join(types)}\n\n"
        "知识片段（只能依据这些片段）：\n"
        f"{_render_snippets(blocks)}\n\n"
        "输出要求：只输出一个 JSON 数组，每个元素是一道题，字段如下：\n"
        '  "question": 题干字符串\n'
        '  "type": single / multiple / judgement / short 之一\n'
        '  "options": 选项字符串数组（single/multiple 必须 4 项；judgement/short 给空数组）\n'
        '  "answer": single 为正确项下标（整数，0 开始）；multiple 为正确项下标数组；'
        "judgement 为 true/false；short 为要点字符串数组（例如 [\"4000 运行小时\", \"12 个月\"]）\n"
        '  "explanation": 一句话说明依据（可引用片段原文）\n'
        '  "evidence_index": 本题依据的片段编号（整数，1 开始，对应上面的 [n]）\n'
        "注意：\n"
        "1. 每道题必须给出 evidence_index，且只能引用上面出现过的编号；\n"
        "2. 干扰项只能用片段里出现过的其它数值，不许自造数值；\n"
        "3. short 的 answer 要点要能在片段原文中找到。"
    )


def _as_index(value: Any) -> int | None:
    """把学员/模型的答案转成选项下标（int；也容忍 "0"、"A" 这类写法）。"""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if len(text) == 1 and "A" <= text.upper() <= "Z":
        return ord(text.upper()) - ord("A")
    return None


def _as_index_set(value: Any) -> set[int]:
    """多选答案 → 下标集合（完全一致才判对，所以用集合比较）。"""

    raw: list[Any]
    if isinstance(value, (list, tuple, set)):
        raw = list(value)
    elif isinstance(value, str):
        raw = [part for part in re.split(r"[、,，;；\s]+", value) if part]
    else:
        raw = [value]
    out: set[int] = set()
    for item in raw:
        index = _as_index(item)
        if index is not None:
            out.add(index)
    return out


_TRUE_WORDS = {"true", "1", "对", "正确", "是", "t", "yes", "y", "√", "v"}
_FALSE_WORDS = {"false", "0", "错", "错误", "否", "f", "no", "n", "×", "x"}


def _as_bool(value: Any) -> bool | None:
    """判断题答案归一（bool / 1-0 / "正确-错误" / "对-错"）。"""

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if value in (0, 1):
            return bool(value)
        return None
    text = str(value).strip().lower()
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    return None


def _split_points(value: Any) -> list[str]:
    """简答要点归一：列表 / 「、；,」分隔的字符串 → 非空字符串列表。"""

    if isinstance(value, (list, tuple, set)):
        parts = [str(item) for item in value]
    else:
        text = str(value or "")
        parts = re.split(r"[、,，;；/\n]+", text) if text else []
    return [part.strip(" \t。.；;") for part in parts if part and part.strip(" \t。.；;")]


def _validate_llm_item(
    candidate: Any, blocks: Sequence[Evidence], types: Sequence[str]
) -> tuple[dict[str, Any] | None, str]:
    """校验模型产出的一道题：不合法就丢弃（返回丢弃原因，便于排查与调提示词）。"""

    if not isinstance(candidate, dict):
        return None, "元素不是对象"

    question = str(candidate.get("question") or "").strip()
    if len(question) < 5:
        return None, "题干缺失或过短"

    qtype = str(candidate.get("type") or "").strip().lower()
    if qtype not in types:
        return None, f"题型不在要求范围内：{qtype or '空'}"

    # ---- evidence_index 校验：越界/缺失一律丢弃，绝不猜是哪一段 ----
    raw_index = candidate.get("evidence_index")
    try:
        evidence_index = int(str(raw_index).strip())
    except (TypeError, ValueError):
        return None, "evidence_index 缺失或不是整数"
    if not 1 <= evidence_index <= len(blocks):
        return None, f"evidence_index 越界：{evidence_index}（片段数 {len(blocks)}）"

    block = blocks[evidence_index - 1]
    options_raw = candidate.get("options")
    options = (
        [str(option).strip() for option in options_raw if str(option).strip()]
        if isinstance(options_raw, (list, tuple))
        else []
    )
    answer_raw = candidate.get("answer")

    if qtype in ("single", "multiple"):
        if len(options) < 2:
            return None, "选项少于 2 项"
        if qtype == "single":
            answer: Any = _as_index(answer_raw)
            if answer is None or answer >= len(options):
                return None, "单选答案不是合法下标"
        else:
            indexes = _as_index_set(answer_raw)
            if not indexes or any(index >= len(options) for index in indexes):
                return None, "多选答案不是合法下标集合"
            answer = sorted(indexes)
    elif qtype == "judgement":
        flag = _as_bool(answer_raw)
        if flag is None:
            return None, "判断题答案不是 true/false"
        answer = flag
        options = options or ["正确", "错误"]
    else:  # short
        answer = _split_points(answer_raw)
        if not answer:
            return None, "简答答案没有要点"
        options = []

    # ---- 数值可溯源校验：题干/选项/简答要点里的数值必须在该片段原文中出现过 ----
    checked = [question, *options]
    if qtype == "short":
        checked.extend(str(point) for point in answer)
    untraceable = _untraceable_numbers(checked, block.text)
    if untraceable:
        return None, f"数值在片段原文中查不到（疑似编造）：{', '.join(untraceable[:3])}"

    explanation = str(candidate.get("explanation") or "").strip()
    if not explanation:
        # 模型没写解释时用**片段原文**补（不是新写内容）
        explanation = f"依据原文（{_source_label(block)}）：「{block.text.strip()[:160]}」"

    return (
        {
            "question": question,
            "type": qtype,
            "options": options,
            "answer": answer,
            "explanation": explanation,
            "evidence": _evidence_for(blocks, evidence_index),
        },
        "",
    )


def _untraceable_numbers(texts: Sequence[str], evidence_text: str) -> list[str]:
    """返回文本里「该片段原文中查不到」的数值（空列表表示全部可溯源）。

    片段侧按**裸数字**建立可溯源集合（含型号/编码里的数字，如 TMP-1600 的 1600），
    题面侧只看独立数值 token —— 模型把「TMP-1600」写成「TMP 1600」不算编造，
    但写成语料里根本不存在的「7777」会被拦下。
    """

    allowed = {_number_key(match.group(0)) for match in re.finditer(r"\d+(?:\.\d+)?", evidence_text or "")}
    missing: list[str] = []
    for text in texts:
        for match in _num_tokens(str(text)):
            num = _WS_RE.sub("", match.group("num"))
            if _number_key(num) not in allowed:
                missing.append(num)
    return list(dict.fromkeys(missing))


async def _llm_items(
    blocks: Sequence[Evidence], *, payload: QuizGenerateRequest, types: Sequence[str]
) -> list[dict[str, Any]]:
    """调模型出题并逐题校验；结构性失败抛错，由调用方降级。"""

    messages = [
        {"role": "system", "content": _QUIZ_SYSTEM_PROMPT},
        {"role": "user", "content": _render_quiz_prompt(blocks, payload=payload, types=types)},
    ]
    raw = await llm_client.chat_json(messages, temperature=0.0)
    candidates = raw.get("items") if isinstance(raw, dict) else raw
    if not isinstance(candidates, list):
        raise ValueError("模型输出不是 JSON 数组")

    items: list[dict[str, Any]] = []
    dropped: list[str] = []
    for candidate in candidates:
        if len(items) >= payload.n_items:
            break
        item, reason = _validate_llm_item(candidate, blocks, types)
        if item is None:
            dropped.append(reason)
            continue
        items.append(item)
    if dropped:
        logger.info("LLM 出题丢弃 %d 道不合规题目：%s", len(dropped), "；".join(dropped[:5]))
    return items


# --------------------------------------------------------------------------- #
# 判分（确定性代码）
# --------------------------------------------------------------------------- #


def _grade(item: dict[str, Any], got: Any) -> bool:
    """按题型判一道题（single 下标 / multiple 集合完全一致 / judgement bool / short 要点重合）。"""

    qtype = str(item.get("type") or "single")
    expected = item.get("answer")

    if qtype == "single":
        want, have = _as_index(expected), _as_index(got)
        return want is not None and want == have

    if qtype == "multiple":
        want, have = _as_index_set(expected), _as_index_set(got)
        return bool(want) and want == have

    if qtype == "judgement":
        want, have = _as_bool(expected), _as_bool(got)
        return want is not None and want == have

    if qtype == "short":
        points = _split_points(expected)
        if not points:
            return False
        text = str(got or "")
        hits = [p for p in points if _bigram_overlap(p, text) >= _SHORT_HIT_THRESHOLD]
        return len(hits) / len(points) >= _SHORT_PASS_RATIO

    return False


def _grade_all(items: Sequence[dict[str, Any]], answers: Sequence[Any]) -> list[dict[str, Any]]:
    """逐题批改，返回 detail（no/correct/expected/got/explanation/evidence）。"""

    detail: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        got = answers[index] if index < len(answers) else None
        detail.append(
            {
                "no": int(item.get("no") or index + 1),
                "question": str(item.get("question") or ""),
                "correct": _grade(item, got),
                "expected": item.get("answer"),
                "got": got,
                "explanation": str(item.get("explanation") or ""),
                "evidence": list(item.get("evidence") or []),
            }
        )
    return detail


def _serialize_items(items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """落库前重排编号（no 从 1 开始连续），并保证只含 JSON 可序列化字段。"""

    out: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        out.append(
            {
                "no": index,
                "question": str(item.get("question") or ""),
                "type": str(item.get("type") or "single"),
                "options": list(item.get("options") or []),
                "answer": item.get("answer"),
                "explanation": str(item.get("explanation") or ""),
                "evidence": [dict(citation) for citation in (item.get("evidence") or [])],
            }
        )
    return out


# --------------------------------------------------------------------------- #
# 出题
# --------------------------------------------------------------------------- #


def _wanted_types(payload: QuizGenerateRequest) -> list[str]:
    """校验并归一请求的题型（未知题型直接 400，而不是静默换成别的题型）。"""

    requested = [str(item).strip().lower() for item in (payload.question_types or [])]
    requested = [item for item in requested if item]
    if not requested:
        return list(_DEFAULT_TYPES)
    unknown = [item for item in requested if item not in _ALL_TYPES]
    if unknown:
        raise ServiceError(
            400,
            "INVALID_ARGUMENT",
            f"不支持的题型：{', '.join(unknown)}（可选 {'/'.join(_ALL_TYPES)}）",
        )
    return list(dict.fromkeys(requested))


async def _retrieve_blocks(payload: QuizGenerateRequest) -> list[Evidence]:
    """出题前的检索：按设备型号过滤，主题并入检索式。"""

    device_model = payload.device_model.strip()
    query = " ".join(
        part for part in (device_model, payload.topic.strip(), _QUERY_SUFFIX) if part
    )
    filters = {"device_model": device_model} if device_model else None
    k = min(_MAX_EVIDENCE_K, max(_MIN_EVIDENCE_K, payload.n_items + 3))
    blocks = await kb_search(query, filters, k)
    logger.info("出题检索：%r（filters=%s）→ %d 个片段", query, filters, len(blocks))
    return list(blocks)


async def generate_quiz(payload: QuizGenerateRequest, *, trace_id: str) -> dict[str, Any]:
    """按设备/主题出题（先检索，再 LLM 或确定性抽句），返回 QuizResponse 可校验的字典。

    流程与零幻觉约束：
        1. `kb_search` 取 6~8 个片段，**题目只能基于这些片段**；
        2. 有密钥时走 LLM（`llm:<model>`）：要求每题给出 evidence_index，
           越界/缺失、或题干与选项里出现片段原文查不到的数值 → **丢弃该题**；
        3. 无密钥 / 调用失败 / JSON 解析失败 / 全部被丢弃 → 确定性抽句降级
           （`extractive-fallback`，原句 + 同片段其它数值做干扰项）；
        4. 一道题都出不来 → 422 QUIZ_GENERATION_FAILED（宁可不出题，也不出无依据的题）。
    """

    _require_db()
    types = _wanted_types(payload)
    blocks = await _retrieve_blocks(payload)
    if not blocks:
        raise ServiceError(
            422,
            "QUIZ_GENERATION_FAILED",
            f"知识库中没有可用于出题的片段（设备型号：{payload.device_model or '不限'}，"
            f"主题：{payload.topic or '不限'}），请在知识库补充资料后再出题",
        )

    settings = get_settings()
    generator = "extractive-fallback"
    items: list[dict[str, Any]] = []

    if llm_client.available():
        try:
            items = await _llm_items(blocks, payload=payload, types=types)
            if items:
                generator = f"llm:{settings.deepseek_model}"
            else:
                logger.warning("LLM 出题没有产出合规题目，降级为抽句出题")
        except Exception as exc:  # noqa: BLE001 - 出题失败必须降级，不中断链路
            logger.warning("LLM 出题失败（%s），降级为抽句出题", exc)

    if not items:
        items = _fallback_items(blocks, n_items=payload.n_items, types=types)

    if not items:
        raise ServiceError(
            422,
            "QUIZ_GENERATION_FAILED",
            "检索到的片段中没有可用作题目的句子（缺少明确的数值或判定要求），"
            "为保证零幻觉，本次不出题",
        )

    rows = _serialize_items(items[: payload.n_items])
    row = db.TrainingQuiz(
        device_model=payload.device_model.strip(),
        topic=payload.topic.strip(),
        level=payload.level,
        n_items=len(rows),
        items=rows,
        generator=generator,
        created_at=_now(),
    )
    async with db.session_scope() as session:
        session.add(row)
        await session.flush()
        quiz_id = int(row.id or 0)
        created_at = row.created_at

    logger.info(
        "出题完成：quiz_id=%s 题数=%d 生成方式=%s trace_id=%s",
        quiz_id,
        len(rows),
        generator,
        trace_id,
    )
    return {
        "id": quiz_id,
        "device_model": row.device_model,
        "topic": row.topic,
        "level": row.level,
        "n_items": len(rows),
        "items": rows,
        "generator": generator,
        "created_at": _iso(created_at),
        "trace_id": trace_id,
    }


def _quiz_to_dict(row: db.TrainingQuiz, *, trace_id: str) -> dict[str, Any]:
    """TrainingQuiz 行 → QuizResponse 可校验的字典。"""

    items = [dict(item) for item in (row.items or [])]
    return {
        "id": int(row.id or 0),
        "device_model": row.device_model or "",
        "topic": row.topic or "",
        "level": row.level or "basic",
        "n_items": int(row.n_items or len(items)),
        "items": items,
        "generator": row.generator or "",
        "created_at": _iso(row.created_at),
        "trace_id": trace_id,
    }


async def get_quiz(quiz_id: int, *, trace_id: str) -> dict[str, Any] | None:
    """按 id 取试卷；不存在返回 None（路由层翻译成 404 QUIZ_NOT_FOUND）。"""

    _require_db()
    async with db.session_scope() as session:
        row = await session.get(db.TrainingQuiz, quiz_id)
        if row is None:
            return None
        return _quiz_to_dict(row, trace_id=trace_id)


# --------------------------------------------------------------------------- #
# 判分
# --------------------------------------------------------------------------- #


async def submit_attempt(
    quiz_id: int, payload: QuizSubmitRequest, *, trace_id: str
) -> dict[str, Any] | None:
    """提交作答并判分，写一条 training_attempt，返回 QuizSubmitResponse 可校验的字典。

    判分规则（全部确定性代码，不调模型）：
        - single    ：`answer` 是选项下标（int），完全相等才判对；
        - multiple  ：`answer` 是下标列表，学员答案必须**完全一致**才判对；
        - judgement ：`answer` 是 bool（容忍「正确/错误」「对/错」「true/false」）；
        - short     ：`answer` 是要点列表，逐要点与学员文本做字符二元组重合度，
                      重合 ≥0.5 记命中，命中率 ≥0.6 判该题正确。

    `score = 正确题数 / 总题数 * 100`（保留 1 位小数），`passed = score >= PASS_LINE`。

    **本方法不自动发证**：`certification_id` 恒为 None。发证必须显式调用
    `issue_certification` —— 因为发证要指定等级（L1/L2/L3）与有效期（`valid_days`），
    属于授权决策，不能由「提交答卷」这一步替用户默认掉。

    找不到 quiz（或 DB 不可用）时：不存在返回 None（路由层 404）。
    """

    _require_db()
    async with db.session_scope() as session:
        quiz = await session.get(db.TrainingQuiz, quiz_id)
        if quiz is None:
            return None

        items = [dict(item) for item in (quiz.items or [])]
        if not items:
            raise ServiceError(422, "QUIZ_EMPTY", f"试卷（id={quiz_id}）没有题目，无法判分")

        detail = _grade_all(items, list(payload.answers or []))
        n_correct = sum(1 for entry in detail if entry["correct"])
        score = round(n_correct / len(items) * 100, 1)
        passed = score >= PASS_LINE

        attempt = db.TrainingAttempt(
            quiz_id=quiz_id,
            trainee=payload.trainee,
            answers=list(payload.answers or []),
            score=score,
            passed=passed,
            detail=detail,
            duration_s=float(payload.duration_s or 0.0),
            created_at=_now(),
        )
        session.add(attempt)
        await session.flush()
        attempt_id = int(attempt.id or 0)

    logger.info(
        "判分完成：quiz_id=%s attempt_id=%s trainee=%s score=%s passed=%s trace_id=%s",
        quiz_id,
        attempt_id,
        payload.trainee,
        score,
        passed,
        trace_id,
    )
    return {
        "attempt_id": attempt_id,
        "quiz_id": quiz_id,
        "trainee": payload.trainee,
        "score": score,
        "passed": passed,
        "pass_line": PASS_LINE,
        "detail": detail,
        # 发证需要显式调用 issue_certification（等级与有效期由调用方指定）
        "certification_id": None,
        "trace_id": trace_id,
    }


# --------------------------------------------------------------------------- #
# 认证
# --------------------------------------------------------------------------- #


def _cert_to_dict(row: db.Certification, *, trace_id: str, now: datetime | None = None) -> dict[str, Any]:
    """Certification 行 → CertificationResponse 可校验的字典（含距到期天数）。"""

    return {
        "id": int(row.id or 0),
        "trainee": row.trainee or "",
        "device_model": row.device_model or "",
        "level": row.level or "L1",
        "attempt_id": int(row.attempt_id or 0),
        "quiz_id": int(row.quiz_id or 0),
        "score": float(row.score or 0.0),
        "issuer": row.issuer or "",
        "issued_at": _iso(row.issued_at),
        "expires_at": _iso(row.expires_at),
        "status": row.status or "valid",
        "days_to_expiry": _days_to_expiry(row.expires_at, now=now),
        "note": row.note or "",
        "trace_id": trace_id,
    }


def _days_to_expiry(expires_at: datetime | None, *, now: datetime | None = None) -> int | None:
    """距到期天数（负数=已过期；不足一天按「离到期还有 1 天」/「已过期 1 天」计）。"""

    if expires_at is None:
        return None
    moment = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)
    seconds = (moment - (now or _now())).total_seconds()
    days = seconds / 86400.0
    return int(math.floor(days) if seconds < 0 else math.ceil(days))


_CERT_NOTE = "本证书按内部授权口径签发，不代表设备原厂认证；到期前请复训并重新考核。"


async def issue_certification(
    payload: CertificationRequest, *, trace_id: str
) -> dict[str, Any]:
    """发证：必须基于**一次通过的考核**，并记录有效期用于到期提醒。

    规则：
        - `attempt_id` 必须存在且 `passed=True`，否则
          `ServiceError(400, "ATTEMPT_NOT_PASSED", "认证必须基于一次通过的考核")`；
        - `expires_at = issued_at + valid_days` 天，`status="valid"`；
        - **系统不代表原厂发证**：`issuer` 默认「内部授权」，note 固定标注该口径；
        - 分数取该次考核的 `score`，`quiz_id` 取该次考核对应试卷。
    """

    _require_db()
    async with db.session_scope() as session:
        attempt = await session.get(db.TrainingAttempt, payload.attempt_id)
        if attempt is None:
            raise ServiceError(
                404,
                "ATTEMPT_NOT_FOUND",
                f"考核记录不存在：{payload.attempt_id}",
            )
        if not attempt.passed:
            raise ServiceError(
                400,
                "ATTEMPT_NOT_PASSED",
                "认证必须基于一次通过的考核",
                {"attempt_id": payload.attempt_id, "score": float(attempt.score or 0.0)},
            )

        if attempt.trainee and payload.trainee and attempt.trainee != payload.trainee:
            logger.warning(
                "发证的 trainee（%s）与考核记录的 trainee（%s）不一致：attempt_id=%s",
                payload.trainee,
                attempt.trainee,
                payload.attempt_id,
            )

        issued_at = _now()
        expires_at = issued_at + timedelta(days=int(payload.valid_days))
        note = payload.note.strip()
        note = f"{note} {_CERT_NOTE}".strip() if note else _CERT_NOTE

        row = db.Certification(
            trainee=payload.trainee,
            device_model=payload.device_model,
            level=payload.level or "L1",
            attempt_id=int(attempt.id or 0),
            quiz_id=int(attempt.quiz_id or 0),
            score=float(attempt.score or 0.0),
            issuer=(payload.issuer or "内部授权"),
            issued_at=issued_at,
            expires_at=expires_at,
            status="valid",
            note=note,
            created_at=issued_at,
        )
        session.add(row)
        await session.flush()
        result = _cert_to_dict(row, trace_id=trace_id, now=issued_at)

    logger.info(
        "发证完成：cert_id=%s trainee=%s device=%s level=%s 到期=%s trace_id=%s",
        result["id"],
        result["trainee"],
        result["device_model"],
        result["level"],
        result["expires_at"],
        trace_id,
    )
    return result


async def list_certifications(
    *,
    trainee: str | None,
    device_model: str | None,
    status: str | None,
    limit: int,
    offset: int,
    trace_id: str,
) -> dict[str, Any]:
    """认证记录分页查询（trainee / device_model / status 可选过滤，按签发时间倒序）。"""

    _require_db()
    conditions = []
    if trainee:
        conditions.append(db.Certification.trainee == trainee)
    if device_model:
        conditions.append(db.Certification.device_model == device_model)
    if status:
        conditions.append(db.Certification.status == status)

    async with db.session_scope() as session:
        total = int(
            (
                await session.execute(
                    select(func.count()).select_from(db.Certification).where(*conditions)
                )
            ).scalar_one()
        )
        rows = list(
            (
                await session.execute(
                    select(db.Certification)
                    .where(*conditions)
                    .order_by(db.Certification.issued_at.desc(), db.Certification.id.desc())
                    .limit(max(1, int(limit)))
                    .offset(max(0, int(offset)))
                )
            ).scalars()
        )
        now = _now()
        items = [_cert_to_dict(row, trace_id=trace_id, now=now) for row in rows]

    return {"items": items, "total": total, "trace_id": trace_id}


async def list_expiring(*, days: int, trace_id: str) -> dict[str, Any]:
    """到期提醒：返回 `expires_at` 落在 `[now-30天, now+days]` 区间内的认证。

    - 含**已过期**的（`days_to_expiry` 为负数），便于催复训；
    - 已吊销（`status="revoked"`）的不提醒（证书已失效，不需要续期）；
    - 按 `expires_at` 升序（最紧急的排前面）；单次最多返回 `_EXPIRING_LIMIT` 条。
    """

    _require_db()
    now = _now()
    horizon = int(max(0, days))
    window_start = now - timedelta(days=_EXPIRING_LOOKBACK_DAYS)
    window_end = now + timedelta(days=horizon)

    async with db.session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(db.Certification)
                    .where(
                        db.Certification.expires_at.is_not(None),
                        db.Certification.expires_at >= window_start,
                        db.Certification.expires_at <= window_end,
                        db.Certification.status != "revoked",
                    )
                    .order_by(db.Certification.expires_at.asc(), db.Certification.id.asc())
                    .limit(_EXPIRING_LIMIT)
                )
            ).scalars()
        )
        items = [_cert_to_dict(row, trace_id=trace_id, now=now) for row in rows]

    return {"items": items, "total": len(items), "trace_id": trace_id}


__all__ = [
    "PASS_LINE",
    "generate_quiz",
    "get_quiz",
    "issue_certification",
    "list_certifications",
    "list_expiring",
    "submit_attempt",
]
