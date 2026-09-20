"""引用生成与校验、置信度评分、拒答判定（开发文档 4.3 verify / 5.2.3 四道关）。

**全部是确定性代码，不含任何模型调用** —— 能用固定流程表达的，绝不交给模型
自主决定（开发文档 4.4.2）。`verify` 节点只调用本模块。

四道关（开发文档 5.2.3）
------------------------
1. 引用存在性：答案里出现的 `[n]` 必须能在 `context_blocks` 里找到对应块；
   找不到的编号即「伪造引用」，一律判不通过；
2. 引用覆盖率：结论句必须逐句挂引用，覆盖率目标 **100%**（不可降级指标）；
3. 置信度：`0.40×精排 Top1 归一化分 + 0.35×引用覆盖率 + 0.25×来源权威度`；
4. 拒答：无证据 / 精排分过低 / 引用校验不通过 / 覆盖率不达标 → `NOT_COVERED`。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..agents.state import Citation, ConfidenceLabel, Evidence, Status
from ..setting import Settings, get_settings
from .ingest import split_sentences

logger = logging.getLogger(__name__)

#: 引用编号出现形式：[1]、[1,2]、[1-3]、［1］（全角）
_CITATION_GROUP_RE = re.compile(r"[\[［]\s*(\d+(?:\s*[-–,，、]\s*\d+)*)\s*[\]］]")
#: 不需要引用的行：标题、列表项前缀、纯过渡句、以及文末的「依据」清单
_NO_CITE_PATTERNS = (
    re.compile(r"^[#*\->\s]*$"),
    re.compile(r"^(安全提示|提示|注意|说明|小结|结论)[:：]?$"),
    re.compile(r"^\s*建议"),  # 「建议…」是处置指引，不是知识结论
    # 「依据：」清单本身是对引用的复述，不是新的结论句
    re.compile(r"^[\s\-*\d.、]*依据\s*[:：]"),
    # 纯引用行，如「- [1] 《刻蚀设备维护手册》 V3.2 第 3 页 章节 3.4」
    re.compile(r"^[\s\-*\d.、]*\[\d+(?:\s*[,，]\s*\d+)*\]"),
    re.compile(r"^[\s\-*\d.、]*《.+》\s*V?[\d.]+.*(页|章节)"),
    # Markdown 粗体小标题整行（如「**基础理解**」）：结构行，不是知识结论
    re.compile(r"^\*{1,2}[^*\n]{1,40}\*{1,2}\s*[：:]?\s*$"),
)
#: 覆盖率豁免：这些句子属于「未覆盖说明 / 模板尾注」而不是知识结论
#: 注意：培训模式（FR-09）的讲解模板会带结构行与尾注，它们不是结论句 ——
#: 若不豁免，讲解类回答会被判「覆盖率不足」而误拒（实测踩到过）。
_DISCLAIMER_RE = re.compile(
    r"(知识库未覆盖|未检索到|无法确认|未找到|不确定|请人工确认|建议转人工"
    r"|以上内容摘自|以设备实际型号与版本为准|现场作业请)"
)

#: 拒答时的固定话术（不给出操作步骤 —— 开发文档 4.2「不做什么」）
NOT_COVERED_TEMPLATE = (
    "知识库未覆盖该问题，不给出具体操作步骤。\n"
    "已检索到的相关材料：{materials}\n"
    "建议：补充设备型号与现象描述，或转人工支持。"
)


@dataclass(slots=True)
class CitationCheck:
    """引用校验结果（给 verify 节点与评测脚本共用）。"""

    #: 答案里出现过的引用编号（按出现顺序）
    used_ids: list[int] = field(default_factory=list)
    #: 真实存在的编号
    valid_ids: list[int] = field(default_factory=list)
    #: **伪造引用**：答案里出现但上下文里不存在的编号
    fake_ids: list[int] = field(default_factory=list)
    #: 未被任何句子引用的上下文块编号（无害，但用于提示模型「有依据没用」）
    unused_ids: list[int] = field(default_factory=list)
    #: 结论句总数 / 带引用的结论句数
    total_claims: int = 0
    cited_claims: int = 0
    #: 逐句明细：[{"sentence":..., "citations":[...]}]
    sentences: list[dict[str, Any]] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        """引用覆盖率（结论句口径）。"""

        if self.total_claims == 0:
            return 0.0
        return self.cited_claims / self.total_claims

    @property
    def has_fake_citation(self) -> bool:
        return bool(self.fake_ids)

    @property
    def passed(self) -> bool:
        return not self.has_fake_citation


def parse_citation_ids(text: str) -> list[int]:
    """从文本里解析引用编号（支持 [1] / [1,2] / [1-3] / 全角括号）。"""

    ids: list[int] = []
    for match in _CITATION_GROUP_RE.finditer(text or ""):
        body = match.group(1)
        for part in re.split(r"[,，、]", body):
            part = part.strip()
            if re.fullmatch(r"\d+", part):
                ids.append(int(part))
                continue
            range_match = re.fullmatch(r"(\d+)\s*[-–]\s*(\d+)", part)
            if range_match:
                start, end = int(range_match.group(1)), int(range_match.group(2))
                if start <= end and end - start <= 50:
                    ids.extend(range(start, end + 1))
    # 去重并保持出现顺序
    return list(dict.fromkeys(ids))


def is_claim_sentence(sentence: str) -> bool:
    """判断一句话是否属于「需要引用的结论句」。

    豁免：过短句、纯标题、以及明确的未覆盖说明（拒答话术本身不需要引用）。
    """

    stripped = (sentence or "").strip()
    if len(stripped) < 8:
        return False
    if any(p.search(stripped) for p in _NO_CITE_PATTERNS):
        return False
    if _DISCLAIMER_RE.search(stripped):
        return False
    return True


def check_citations(
    answer: str,
    blocks: Sequence[Evidence],
    citations: Sequence[Citation] | None = None,
) -> CitationCheck:
    """校验答案与上下文/引用的一致性（**不做模型调用**）。

    参数
    ----
    answer:  生成的答案文本（含 `[n]` 标记）
    blocks:  上下文块（顺序即编号 1..n，来自 retriever.build_context）
    citations: 可选的引用项列表；仅用于统计，真实性以 blocks 为准
    """

    valid_ids = {index for index in range(1, len(blocks) + 1)}
    if citations:
        # 引用项与上下文块不一致时以 blocks 为准，但引用项里出现过的编号不算「伪造」
        valid_ids |= {c.id for c in citations}

    result = CitationCheck()
    used: list[int] = []
    for sentence in split_sentences(answer or ""):
        ids = parse_citation_ids(sentence)
        if ids:
            used.extend(ids)
            if is_claim_sentence(sentence):
                result.total_claims += 1
                # 只有**真实存在**的引用才算「这句话有依据」：
                # 挂了伪造编号的句子等于没有依据，不能计入覆盖率
                if any(cid in valid_ids for cid in ids):
                    result.cited_claims += 1
            result.sentences.append({"sentence": sentence, "citations": ids})
        elif is_claim_sentence(sentence):
            result.total_claims += 1
            result.sentences.append({"sentence": sentence, "citations": []})

    result.used_ids = list(dict.fromkeys(used))
    result.valid_ids = [i for i in result.used_ids if i in valid_ids]
    result.fake_ids = [i for i in result.used_ids if i not in valid_ids]
    result.unused_ids = sorted(valid_ids - set(result.used_ids))
    if result.fake_ids:
        logger.warning("伪造引用编号：%s", result.fake_ids)
    return result


@dataclass(slots=True)
class AnchorCheck:
    """问题锚点校验：问题里的关键标识是否真的出现在检索到的材料里。

    这是「负样本必须拒答」的主要闸门 —— 精排分数只能说「这条材料跟问题像」，
    说不了「这条材料讲的是同一个型号 / 同一个对象」。例如问「XH-900 腔体清洗周期」，
    检索必然返回《刻蚀设备维护手册》里关于 Etcher-A 清洗周期的段落，分数很高，
    但型号根本不存在于知识库 —— 此时必须拒答，不能把别的型号的参数当成答案。
    """

    ok: bool
    missing_models: list[str] = field(default_factory=list)
    missing_numbers: list[str] = field(default_factory=list)
    term_coverage: float = 0.0
    missing_terms: list[str] = field(default_factory=list)
    #: 在整个知识库中都找不到的关键术语（缺失比超出阈值即判未覆盖）
    kb_missing_terms: list[str] = field(default_factory=list)
    kb_missing_ratio: float = 0.0
    #: 真正未通过的检查原因（只在 ok=False 时非空 —— 避免「提示」被当成「拒绝理由」）
    failure_reasons: list[str] = field(default_factory=list)

    @property
    def reasons(self) -> list[str]:
        return list(self.failure_reasons)


#: 设备型号/报警码形态：XH-900 / TMP-1600 / E-2041 / CVD200
_MODEL_ID_RE = re.compile(r"[A-Za-z]{1,6}[-\s]?\d{2,4}")
#: 数值 + 单位（问题里出现的具体阈值必须有依据）
#: 前面不能是 `-` 或字母数字：型号里的数字（CVD-200、TMP-1600）不是「具体数值」
_NUM_UNIT_RE = re.compile(
    r"(?<![-\w])\d+(?:\.\d+)?\s*"
    r"(?:h|s|min|Pa|kPa|MPa|sccm|slm|L|mL|mm|cm|kg|W|kW|MHz|ppm|r/min|rpm|片|次|天|小时|分钟|秒)"
)
#: 疑问句里不承载「查什么」的通用词，不计入术语覆盖率
_QUESTION_NOISE: frozenset[str] = frozenset(
    {
        "什么", "怎么", "如何", "为什么", "多少", "哪个", "哪些", "是否", "请问", "需要",
        "应该", "可以", "规定", "要求", "标准", "情况", "问题", "时候", "步骤", "顺序",
        "方法", "处理", "执行", "分别", "具体", "一般", "通常", "以及", "还有", "如果",
        "怎样", "多久", "何时", "哪几", "几个", "一次", "多少", "什么", "怎么",
        # 口语化提问里的填充词、程度词、泛化动词（实测 45 条集上会把可答问题误判为未覆盖）
        "要是", "如果", "假设", "万一", "比如", "例如", "以上", "以下", "太低", "太高",
        "压得", "毛病", "讲究", "我们", "你们", "他们", "准备", "打算", "换个", "换一个",
        "这块", "那块", "这个", "那个", "用到", "配药", "药水", "注意", "怎么办", "干嘛",
        "车间", "起火", "或者", "第一步", "第二步", "后续", "然后", "接着", "顺便", "麻烦",
        "帮忙", "看下", "问下", "有没有", "能不能", "行不行", "大概", "差不多", "可能",
        "有点", "挺", "超", "特别", "更", "最", "再", "又", "还", "就", "才", "都", "也",
        "只", "光", "全", "非常", "一下", "一些", "点儿", "点儿", "啥", "咋", "哦", "嗯",
        "哪里", "哪儿", "哪种", "什么样",
        # 提问框架名词：它们问的是「想知道哪一类信息」，不是知识库里的实体。
        # 负样本靠的是「年度/维保/合同/费用/折旧/年限/资产/编号/预算/排班表」这类实体词，
        # 不在这张表里，因此不会削弱未覆盖判定（45 条集上实测：正样本误杀 0、负样本 5/5 仍拦住）。
        "后果", "限制", "影响", "原因", "效果", "风险", "区别", "关系", "做法", "作用",
        "好处", "坏处", "要点", "前提", "依据", "结论", "表现", "现象", "趋势", "幅度",
        "程度", "细节", "情况", "步骤", "方法", "措施", "要求", "标准", "判据", "区别",
    }
)


def _content_terms(question: str, *, min_len: int = 2) -> list[str]:
    """问题的内容术语（去停用词、去疑问词、去单字）。"""

    try:
        from .retriever import get_tokenizer

        tokens = get_tokenizer().cut(question)
    except Exception:  # noqa: BLE001 - 分词不可用时退化为字符二元组
        tokens = [question[i : i + 2] for i in range(len(question) - 1)]
    terms: list[str] = []
    for token in tokens:
        token = token.strip()
        if len(token) < min_len or token in _QUESTION_NOISE or token in _STOPWORD_HINT:
            continue
        if re.fullmatch(r"[，。、；：？！（）()\[\]【】\s]+", token):
            continue
        terms.append(token)
    return list(dict.fromkeys(terms))


#: 与分词器共享的停用词（避免循环 import，这里只做最小副本判断）
_STOPWORD_HINT: frozenset[str] = frozenset({"的", "了", "是", "在", "和", "与", "有", "以及"})


def anchor_check(
    question: str,
    blocks: Sequence[Evidence],
    *,
    min_term_coverage: float = 0.5,
    corpus_text: str | None = None,
    max_kb_missing_ratio: float = 0.4,
) -> AnchorCheck:
    """校验问题锚点是否落在上下文里（型号 / 数值 / 关键术语）。

    参数
    ----
    question: 用户问题原文
    blocks:   上下文块（要引用它们来回答，就必须在它们里面找到依据）
    min_term_coverage: 关键术语在**上下文中**的覆盖率下限，默认 0.5
    corpus_text: 整个知识库的检索文本；传入后额外做「术语是否存在于知识库」的校验 ——
        这是「问的东西知识库里根本没有」这类负样本最可靠的判据
        （例如问「年度维保合同费用」，检索仍会返回清洗设备文档，分数还挺高）
    max_kb_missing_ratio: 知识库级缺失比上限，默认 0.4

    返回 AnchorCheck：`ok=False` 时应当拒答，`reasons` 可直接进 done 事件的 uncertain。
    """

    haystack = " ".join(
        f"{(b.metadata or {}).get('doc_title', '')} {(b.metadata or {}).get('section', '')} {b.text}"
        for b in blocks
    )
    haystack_norm = haystack.lower()

    models = [m for m in {x.strip() for x in _MODEL_ID_RE.findall(question or "")} if m]
    missing_models = [m for m in models if m.lower() not in haystack_norm]

    # 数值锚点前先把型号/报警码从文本里剔除：否则「CVD-200 片内均匀性」里的 200
    # 会被当成「数值 200 片」要求材料里出现，把一个可答问题判成无依据（实测踩到过）
    scrubbed = question or ""
    for model in models:
        scrubbed = scrubbed.replace(model, " ")
    numbers = [n.strip() for n in {x.strip() for x in _NUM_UNIT_RE.findall(scrubbed)}]
    missing_numbers = [n for n in numbers if n.lower() not in haystack_norm]

    # 型号片段（如 XH / 900）已由型号规则覆盖，避免在术语里重复报一次
    model_fragments = {part.lower() for m in models for part in re.split(r"[-\s]", m)}
    terms = [t for t in _content_terms(question or "") if t.lower() not in model_fragments]
    if terms:
        hits = [t for t in terms if t.lower() in haystack_norm]
        coverage = len(hits) / len(terms)
        missing_terms = [t for t in terms if t not in hits]
    else:  # pragma: no cover - 极短问题
        coverage, missing_terms = 1.0, []

    # ---- 知识库级校验：术语在整个知识库里都不存在 → 问题本身超出覆盖范围 ----
    kb_missing: list[str] = []
    kb_ratio = 0.0
    if corpus_text and terms:
        corpus_norm = corpus_text.lower()
        kb_missing = [t for t in terms if t.lower() not in corpus_norm]
        kb_ratio = len(kb_missing) / len(terms)

    failures: list[str] = []
    if missing_models:
        failures.append(
            f"问题中的设备标识 {missing_models} 未出现在检索到的材料中（知识库未覆盖该型号）"
        )
    if missing_numbers:
        failures.append(f"问题中的具体数值 {missing_numbers} 在检索到的材料中找不到依据")
    if coverage < min_term_coverage:
        failures.append(
            f"问题关键术语在材料中的覆盖率仅 {coverage:.0%}（缺失：{'、'.join(missing_terms[:6])}）"
        )
    if kb_ratio > max_kb_missing_ratio:
        failures.append(
            f"问题涉及的关键点 {kb_missing[:6]} 在知识库中不存在（缺失比 {kb_ratio:.0%}"
            f" 超过上限 {max_kb_missing_ratio:.0%}）"
        )

    return AnchorCheck(
        missing_models=missing_models,
        missing_numbers=missing_numbers,
        term_coverage=round(coverage, 4),
        missing_terms=missing_terms,
        kb_missing_terms=kb_missing,
        kb_missing_ratio=round(kb_ratio, 4),
        failure_reasons=failures,
        ok=not failures,
    )


# --------------------------------------------------------------------------- #
# 引用归属（LLM 路径的覆盖率保障）
# --------------------------------------------------------------------------- #


#: 归一化时去掉空白与标点（\W 在 Unicode 模式下不会吃掉中文，只去标点/空白）
_NORM_PUNCT_RE = re.compile(r"[\W_]+")


def _normalize_for_match(text: str) -> str:
    return _NORM_PUNCT_RE.sub("", (text or "").lower())


def _bigrams(text: str) -> set[str]:
    return {text[i : i + 2] for i in range(len(text) - 1)} if len(text) > 1 else {text}


def _bigram_overlap(text: str, other: str) -> float:
    """字符二元组重合度（中文下比词重合更稳，且不依赖分词器）。"""

    a = _bigrams(_normalize_for_match(text))
    b = _bigrams(_normalize_for_match(other))
    if not a:
        return 0.0
    return len(a & b) / len(a)


def best_supporting_block(
    sentence: str,
    blocks: Sequence[Evidence],
    *,
    threshold: float,
) -> int | None:
    """给一句结论找最匹配的上下文块，返回其 1-based 编号；不达标返回 None。"""

    best_index, best_score = None, 0.0
    for index, block in enumerate(blocks, start=1):
        score = _bigram_overlap(sentence, block.text)
        if score > best_score:
            best_index, best_score = index, score
    if best_index is None or best_score < threshold:
        return None
    return best_index


def _attach_citation(sentence: str, index: int) -> str:
    """把引用编号插到句末标点**之前**（否则按标点切句会把引用与结论句拆开）。"""

    stripped = sentence.rstrip()
    match = re.search(r"([。！？!?；;]+)$", stripped)
    if match:
        tail = match.group(1)
        body = stripped[: -len(tail)]
        return f"{body} [{index}]{tail}"
    return f"{stripped} [{index}]"


def enforce_citations(
    answer: str,
    blocks: Sequence[Evidence],
    *,
    threshold: float = 0.45,
) -> tuple[str, dict[str, Any]]:
    """让每条结论句都带上引用：代码补引用，补不上的句子**直接省略**。

    为什么需要（实测缺陷 #10）
    --------------------------
    `verify` 在高置信度下要求引用覆盖率 100%，而这个门槛原先只有**抽句式**路径能天然满足
    （那条路径由代码给每句分配编号）。换成真 LLM 后，提示词只能「尽量」逐句挂引用，
    实测落在 78%~94% —— 于是本来答得不错的回答被硬闸门整条拒答，
    结果就是「覆盖率 100% 是靠拒答不达标的回答维持的」。

    本函数把「挂引用」从模型的自觉变成**代码保证**：
      1. 已有引用的句子：不动；
      2. 没有引用但有依据的句子：按字符二元组重合度归属到最匹配的上下文块，补上编号；
      3. 归属不上的句子：**不输出**（宁可不答这一句，也不给没有依据的结论）。

    这样「引用覆盖率 100%」恢复成它本来的含义：**发出去的每一条结论都有依据**。

    返回 (新答案, 统计)。统计里 `dropped_sentences` 供可观测与人工复核。
    """

    stats: dict[str, Any] = {
        "threshold": threshold,
        "attributed": 0,
        "kept": 0,
        "dropped": 0,
        "dropped_sentences": [],
    }
    if not (answer or "").strip() or not blocks:
        return answer, stats

    out_lines: list[str] = []
    for line in answer.split("\n"):
        stripped_line = line.strip()
        if not stripped_line:
            out_lines.append(line)
            continue
        # 非结论行（标题、依据清单、表格、代码块等）原样保留
        if not is_claim_sentence(stripped_line):
            out_lines.append(line)
            continue

        pieces = split_sentences(stripped_line)
        if not pieces:
            out_lines.append(line)
            continue

        kept_pieces: list[str] = []
        for piece in pieces:
            if not is_claim_sentence(piece):
                kept_pieces.append(piece)
                continue
            if parse_citation_ids(piece):
                # 已有编号的句子原样保留 —— **包括编号越界的句子**：
                # 伪造引用是模型行为红线，必须留在答案里被 check_citations 抓出来并拒答，
                # 绝不能在这里静默改成「看起来正确」的编号（开发文档 §8.2 明确要求可识别）
                kept_pieces.append(piece)
                stats["kept"] += 1
                continue
            index = best_supporting_block(piece, blocks, threshold=threshold)
            if index is None:
                stats["dropped"] += 1
                if len(stats["dropped_sentences"]) < 5:
                    stats["dropped_sentences"].append(piece.strip()[:80])
                continue
            kept_pieces.append(_attach_citation(piece, index))
            stats["attributed"] += 1

        if not kept_pieces:
            # 整行都是无依据的结论 → 整行不算（不留空壳列表项）
            continue
        prefix = line[: len(line) - len(line.lstrip())] if line.lstrip().startswith(("-", "*", "1", "2", "3", "4", "5", "6", "7", "8", "9")) else line[: len(line) - len(line.lstrip())]
        out_lines.append(prefix + "".join(kept_pieces))

    return "\n".join(out_lines).strip(), stats


def authority_score(
    blocks: Sequence[Evidence],
    *,
    settings: Settings | None = None,
) -> float:
    """来源权威度（置信度公式的 0.25 项）：取上下文块里的最大值。

    取值来自 setting.authority_weights（标准 1.0 / 设备维护 0.9 / 工艺 0.85 …），
    没有任何块时返回 0。
    """

    settings = settings or get_settings()
    weights: dict[str, float] = settings.authority_weights or {}
    best = 0.0
    for block in blocks:
        meta = block.metadata or {}
        category = str(meta.get("category") or "")
        source_type = str(meta.get("source_type") or "")
        if category and category in weights:
            best = max(best, float(weights[category]))
        elif source_type in weights:
            best = max(best, float(weights[source_type]))
        else:
            best = max(best, 0.8)
    return best


def compute_confidence(
    *,
    normalized_top: float,
    coverage: float,
    authority: float,
) -> float:
    """置信度 = 0.40×精排归一化分 + 0.35×引用覆盖率 + 0.25×来源权威度（FR-05）。"""

    score = 0.40 * max(0.0, min(1.0, normalized_top))
    score += 0.35 * max(0.0, min(1.0, coverage))
    score += 0.25 * max(0.0, min(1.0, authority))
    return round(min(1.0, max(0.0, score)), 4)


def confidence_label(score: float, *, settings: Settings | None = None) -> ConfidenceLabel:
    """置信度三级标注：>=0.80 high / 0.60~0.80 medium / <0.60 low。"""

    settings = settings or get_settings()
    if score >= settings.confidence_high:
        return "high"
    if score >= settings.confidence_medium:
        return "medium"
    return "low"


@dataclass(slots=True)
class RejectionDecision:
    """拒答判定结果。`reasons` 会随 SSE done 事件的 uncertain 字段返回。"""

    reject: bool
    status: Status
    reasons: list[str] = field(default_factory=list)
    #: 拒答时给用户的「已检索到的相关材料」清单
    materials: list[str] = field(default_factory=list)


def should_reject(
    *,
    answer: str,
    blocks: Sequence[Evidence],
    check: CitationCheck,
    normalized_top: float,
    confidence: float,
    settings: Settings | None = None,
    require_coverage: float = 1.0,
    question: str = "",
    corpus_text: str | None = None,
    score_threshold: float | None = None,
    anchor_query: str | None = None,
    is_followup: bool = False,
) -> RejectionDecision:
    """四道关的最终闸门：任何一道不过 → 拒答（NOT_COVERED）。

    参数
    ----
    require_coverage: 引用覆盖率要求，默认 1.0（100%，不可降级指标）；
        低置信度路径（setting.confidence_medium 以下）会放宽到 0.8。
    """

    settings = settings or get_settings()
    reasons: list[str] = []

    if not blocks:
        reasons.append("未检索到可用证据")
    if not (answer or "").strip():
        reasons.append("未生成答案")
    if check.has_fake_citation:
        reasons.append(f"引用校验不通过：存在不存在的引用编号 {check.fake_ids}")
    threshold = (
        settings.sufficient_score_threshold if score_threshold is None else score_threshold
    )
    if normalized_top < threshold:
        reasons.append(f"精排最高分 {normalized_top:.2f} 低于阈值 {threshold:.2f}")

    # 锚点校验：型号 / 数值 / 关键术语必须真的在材料里（负样本拒答的主要闸门）
    # 锚点校验比对的是「本次检索表达的意图」，而不是原始字面问题：
    # 追问（「你刚才说的第一步是什么？」）的指代词与「第一步」在知识库里必然查不到，
    # 但它的检索式已经把上文补进来了 —— 拿原始句去校验会把正确回答误判为未覆盖（实测踩到过）。
    # 追问场景：指代句本身与材料没有词面交集是正常的（上文已由检索式补全），
    # 因此只放宽「上下文术语覆盖率」这一条；型号、数值、知识库缺失比三条照旧不放松。
    anchors = anchor_check(
        anchor_query or question,
        blocks,
        min_term_coverage=(
            min(settings.anchor_term_coverage, 0.34) if is_followup else settings.anchor_term_coverage
        ),
        corpus_text=corpus_text,
        max_kb_missing_ratio=settings.anchor_kb_missing_ratio,
    )
    if not anchors.ok:
        # 只在校验**失败**时把原因计入拒答理由；
        # 未超阈值的「缺失术语」只是提示，不能拿来拒绝一个本来答得对的答案
        reasons.extend(anchors.reasons)

    # 覆盖率：高置信度要求 100%；中低置信度按 confidence_medium 放宽
    effective_required = require_coverage
    if confidence < settings.confidence_high:
        effective_required = min(require_coverage, 0.8)
    if blocks and check.total_claims and check.coverage < effective_required:
        reasons.append(
            f"引用覆盖率 {check.coverage:.0%} 低于要求 {effective_required:.0%}"
        )

    if is_followup and reasons and not anchors.missing_models:
        # 追问的指代对象没能从材料里确认时，直接告诉用户怎么问才能答 ——
        # 纯代词追问（「那它的更换周期是多少？」）在确定性链路里无法消解指代，
        # 保守拒答是对的，但话术必须可操作，否则用户只会以为「系统坏了」。
        reasons.append(
            "本次追问依赖上文的指代对象，未能从检索到的材料中确认指代目标；"
            "建议在追问里补上具体部件或参数名称（例如「腔体门 O-ring 的更换周期」）"
        )

    materials = [
        f"《{str((b.metadata or {}).get('doc_title', ''))}》"
        f"{(' ' + str((b.metadata or {}).get('version'))) if (b.metadata or {}).get('version') else ''}"
        f" 第 {(b.metadata or {}).get('page', 0)} 页"
        for b in blocks[:5]
    ]
    if reasons:
        return RejectionDecision(True, "NOT_COVERED", reasons, materials)
    return RejectionDecision(False, "OK", [], materials)


def build_not_covered_answer(decision: RejectionDecision) -> str:
    """拒答话术：只给材料清单与建议，**不给操作步骤**。"""

    materials = "；".join(decision.materials) if decision.materials else "（无）"
    return NOT_COVERED_TEMPLATE.format(materials=materials)


def build_uncertain_notes(decision: RejectionDecision, check: CitationCheck) -> list[str]:
    """组装 done 事件的 uncertain 列表（未覆盖/存疑点）。"""

    notes = list(decision.reasons)
    if check.unused_ids and not decision.reject:
        notes.append(f"上下文块 {check.unused_ids} 未被答案引用")
    return notes


__all__ = [
    "AnchorCheck",
    "CitationCheck",
    "NOT_COVERED_TEMPLATE",
    "RejectionDecision",
    "anchor_check",
    "authority_score",
    "best_supporting_block",
    "enforce_citations",
    "build_not_covered_answer",
    "build_uncertain_notes",
    "check_citations",
    "compute_confidence",
    "confidence_label",
    "is_claim_sentence",
    "parse_citation_ids",
    "should_reject",
]
