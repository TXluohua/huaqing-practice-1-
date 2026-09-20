"""生成与校验节点：generate / verify（开发文档 4.3）。

产出字段
--------
    generate -> answer / answer_structured
    verify   -> citations / confidence / confidence_label / status / uncertain
                （拒答时同时改写 answer 为拒答话术，绝不给出无依据的操作步骤）

生成路径（两条，都不允许编造）
------------------------------
1. **LLM 路径**：配置 DEEPSEEK_API_KEY 时调用 deepseek-chat（temperature=0，
   只允许用上下文、逐句挂引用）；
2. **抽句式降级路径**：没有密钥 / 调用失败时，从上下文块里抽取原句、逐句挂引用
   组成答案。它是「不会编造」的保守实现，**不是 LLM 输出**，会在
   `answer_structured["generator"]` 里如实标注 `extractive-fallback`。

无论哪条路径，答案都要过 verify 的四道关（引用真实性 / 覆盖率 / 置信度 / 拒答），
verify 是**确定性代码**，不可绕过（开发文档 6.3）。
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Sequence

from ...rag.citation import (
    authority_score,
    enforce_citations,
    strip_misleading_refusal_prefix,
    build_not_covered_answer,
    build_uncertain_notes,
    check_citations,
    compute_confidence,
    confidence_label,
    should_reject,
)
from ...rag.retriever import evidence_to_citations
from ...tools.kb_tools import kb_corpus_text
from ...setting import get_settings
from ..state import AgentState, Evidence
from . import NodeName, register_node
from .retrieval import looks_like_followup, normalized_top_score, sufficiency_threshold

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"

#: 降级答案最多抽取多少句（按与问题的关键词重合度排序后取前 N 句）
_MAX_EXTRACT_SENTENCES = 6
_SENTENCE_RE = re.compile(r"(?<=[。！？!?；;])\s*")


def _elapsed(started: float, name: str) -> dict[str, float]:
    return {name: round((time.perf_counter() - started) * 1000, 2)}


def load_prompt(name: str) -> str:
    """读取 prompts/<name>.md；缺失时返回空串（不因提示词缺失而崩溃）。"""

    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():  # pragma: no cover - 提示词缺失兜底
        logger.warning("提示词文件不存在：%s", path)
        return ""
    return path.read_text(encoding="utf-8")


def render_prompt(template: str, **values: str) -> str:
    """极简占位符渲染：替换 {{key}}（不用 str.format，避免正文里的花括号出错）。"""

    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    return rendered


# --------------------------------------------------------------------------- #
# 生成
# --------------------------------------------------------------------------- #


def _sentences(text: str) -> list[str]:
    """切句并规范化：折叠换行/多空格，去掉行首的列表符号。

    必须折叠换行：否则「列表引导句 + 第一项」会被拼成一条跨行句子，
    引用标记只能挂在末尾标点之后，覆盖率统计就会把它算成无引用结论句。
    """

    out: list[str] = []
    for raw in _SENTENCE_RE.split(text):
        cleaned = re.sub(r"\s+", " ", raw).strip()
        cleaned = re.sub(r"^[-*•·]+\s*", "", cleaned)
        cleaned = re.sub(r"^\d+[.、)]\s*", "", cleaned)
        if len(cleaned) >= 8:
            out.append(cleaned)
    return out


def _relevance(sentence: str, question_tokens: set[str]) -> float:
    """句子与问题的关键词重合度（降级路径用来挑最相关的原句）。"""

    if not question_tokens:
        return 0.0
    tokens = {t for t in _SENTENCE_RE.sub(" ", sentence).replace(" ", "") if t}
    hits = len(question_tokens & tokens)
    return hits / len(question_tokens)


def _extractive_answer(
    blocks: Sequence[Evidence],
    *,
    mode: str,
    question: str = "",
    focus_query: str | None = None,
    max_sentences: int = 6,
) -> tuple[str, dict[str, Any]]:
    """抽句式降级答案：只用上下文原句，逐句挂 [n]。

    设计要点（决定「引用覆盖率 100%」能不能成立）：
    1. 每句单独成行，且**引用标记写在句末标点之前**（`…… [n]。`），
       这样按标点切句时引用与结论句必然在同一句里；
    2. 只挑与问题关键词重合度最高的若干句，避免把 8 个上下文块全倒出来；
    3. 文末统一给「依据：」清单 —— 该清单被 verify 认定为非结论句（不需要引用）。

    因为句子全部来自检索到的原文、编号由代码分配，本路径天然不编造。
    """

    # 打分用「检索意图」而不是字面问题：追问（"你刚才说的第一步是什么？"）的字面
    # 词在知识库里一个都不存在，用它打分只能抽到无关句子；用补全后的检索式打分，
    # 才能真正落到上一轮讨论的那一节。
    question_tokens = {t for t in re.sub(r"\s+", "", focus_query or question)}
    candidates: list[tuple[float, int, str, str]] = []
    for index, block in enumerate(blocks, start=1):
        meta = block.metadata or {}
        source = (
            f"[{index}] 《{meta.get('doc_title', '')}》"
            f"{(' ' + str(meta.get('version'))) if meta.get('version') else ''} "
            f"第 {meta.get('page', 0)} 页"
            f"{(' 章节 ' + str(meta.get('section'))) if meta.get('section') else ''}"
        )
        for position, sentence in enumerate(_sentences(block.text)):
            score = _relevance(sentence, question_tokens) - 0.01 * index - 0.005 * position
            candidates.append((score, index, sentence, source))
    candidates.sort(key=lambda item: -item[0])
    picked = candidates[:max_sentences]

    lines: list[str] = []
    if mode == "training":
        # 结构行只写粗体标题：citation 会把「整行粗体」识别为标题而非结论句
        lines.append("**基础理解**")
    for _, index, sentence, _source in picked:
        body = sentence.rstrip("。！？!?；;，,")
        lines.append(f"- {body} [{index}]。")
    if mode == "training":
        lines.append("")
        lines.append("**操作要点**")
        lines.append("以上内容摘自知识库原文，现场作业请以设备实际型号与版本为准。")

    # 文末依据清单（非结论句，verify 不计入覆盖率分母）
    lines.append("")
    lines.append("依据：")
    seen_sources: list[str] = []
    for _, index, _sentence, source in picked:
        if source not in seen_sources:
            seen_sources.append(source)
    lines.extend(f"- {source}" for source in sorted(seen_sources))

    answer = "\n".join(lines).strip()
    structured = {
        "generator": "extractive-fallback",
        "mode": mode,
        "n_blocks": len(blocks),
        "n_sentences": len(picked),
        "note": "未配置 DEEPSEEK_API_KEY 或调用失败，本次答案由上下文原句抽取组成（非 LLM 生成）",
    }
    return answer, structured


async def _call_llm(
    *,
    question: str,
    context: str,
    mode: str,
    history: Sequence[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    """调用 DeepSeek（OpenAI 兼容接口），返回 (答案文本, 结构化元信息)。"""

    import httpx

    settings = get_settings()
    system_prompt = load_prompt("system").strip()
    generate_prompt = render_prompt(
        load_prompt("generate"),
        context=context,
        question=question,
        mode=mode,
    ).strip()

    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    for turn in list(history)[-6:]:
        role = str(turn.get("role", "user"))
        content = str(turn.get("content", ""))
        if content and role in ("user", "assistant"):
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": generate_prompt or question})

    url = f"{settings.deepseek_base_url or 'https://api.deepseek.com'}/chat/completions"
    payload = {
        "model": settings.deepseek_model,
        "messages": messages,
        "temperature": 0,
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {settings.deepseek_api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=settings.answer_timeout_s) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
    answer = str(data["choices"][0]["message"]["content"]).strip()
    structured = {
        "generator": f"llm:{settings.deepseek_model}",
        "mode": mode,
        "usage": data.get("usage", {}),
    }
    return answer, structured


async def generate(state: AgentState) -> dict[str, Any]:
    """上下文 + 问题 → 结构化答案（只用上下文，逐句挂引用）。"""

    started = time.perf_counter()
    settings = get_settings()
    question = state.get("question") or ""
    context = state.get("context") or ""
    blocks = list(state.get("context_blocks") or [])
    mode = str(state.get("answer_mode") or "qa")
    focus = " ".join(str(q) for q in (state.get("queries") or []) if str(q).strip()).strip()
    errors: list[str] = []

    if not blocks:
        # 没有证据就不生成任何内容：交给 verify 明确拒答
        return {
            "answer": "",
            "answer_structured": {
                "generator": "none",
                "mode": mode,
                "reason": "上下文为空（检索无结果或证据不足）",
            },
            "timings": _elapsed(started, "generate"),
            "errors": ["无上下文，跳过生成"],
        }

    answer = ""
    structured: dict[str, Any] = {}
    if settings.deepseek_api_key:
        try:
            answer, structured = await _call_llm(
                question=question,
                context=context,
                mode=mode,
                history=state.get("history") or [],
            )
        except Exception as exc:  # noqa: BLE001 - 调用失败必须降级，不中断链路
            logger.warning("LLM 调用失败（%s），降级为抽句式答案", exc)
            errors.append(f"LLM 调用失败已降级为抽句式答案：{exc}")
            answer, structured = _extractive_answer(
                blocks, mode=mode, question=question, focus_query=focus
            )
    else:
        answer, structured = _extractive_answer(
            blocks, mode=mode, question=question, focus_query=focus
        )
        errors.append("未配置 DEEPSEEK_API_KEY，本次为抽句式降级答案（非 LLM 生成）")

    # 统一做「引用归属」：已有引用的句子不动，缺引用的按文本重合度归属到上下文块，
    # 归属不上的句子直接省略。这样「引用覆盖率 100%」由代码保证，而不是靠 LLM 自觉 ——
    # 实测（配了 DEEPSEEK_API_KEY 后）LLM 只挂到 78%~94%，原先被硬闸门整条拒答（缺陷 #10）。
    answer, enforce_stats = enforce_citations(
        answer, blocks, threshold=settings.citation_attribution_threshold
    )
    structured["citation_enforcement"] = enforce_stats
    if enforce_stats["dropped"]:
        errors.append(
            f"引用归属：{enforce_stats['dropped']} 句结论因无法归属到上下文块被省略"
        )

    out: dict[str, Any] = {
        "answer": answer,
        "answer_structured": structured,
        "timings": _elapsed(started, "generate"),
    }
    if errors:
        out["errors"] = errors
    return out


# --------------------------------------------------------------------------- #
# 校验（确定性代码，禁止绕过）
# --------------------------------------------------------------------------- #


async def verify(state: AgentState) -> dict[str, Any]:
    """引用校验 + 置信度 + 拒答判定（开发文档 4.3 / 5.2.3 四道关）。"""

    started = time.perf_counter()
    settings = get_settings()
    answer = str(state.get("answer") or "")
    blocks = list(state.get("context_blocks") or [])

    # 兜底不变量：**任何路径**产出的答案，在进入校验前都要过一遍引用归属。
    # 45 条集上实测出现过 coverage 0.80 / 0.889 / 0.909 的正样本（其中两条因此被误拒），
    # 而 generate 里那一次归属本该保证 100% —— 与其继续追是哪条路径漏了，
    # 不如把「发出去的每条结论都有依据」变成校验前的硬前置（幂等，重复执行无副作用）。
    # LLM 偶发「首行声明未覆盖、后面照常给答案」：放行前把这行自相矛盾的开头剥掉
    answer, stripped_header = strip_misleading_refusal_prefix(answer)
    if stripped_header:
        logger.debug("已剥掉答案开头误加的「未覆盖」声明")

    enforcement: dict[str, Any] = {}
    if answer.strip() and blocks:
        answer, enforcement = enforce_citations(
            answer, blocks, threshold=settings.citation_attribution_threshold
        )

    citations = evidence_to_citations(blocks)
    check = check_citations(answer, blocks, citations)
    authority = authority_score(blocks)
    normalized = normalized_top_score(state)
    confidence = compute_confidence(
        normalized_top=normalized, coverage=check.coverage, authority=authority
    )
    label = confidence_label(confidence, settings=settings)
    # 追问场景下 queries 已由 rewrite 用上文补全，锚点校验用补全后的意图文本
    resolved_query = " ".join(
        str(q) for q in (state.get("queries") or []) if str(q).strip()
    ).strip()
    decision = should_reject(
        answer=answer,
        blocks=blocks,
        check=check,
        normalized_top=normalized,
        confidence=confidence,
        settings=settings,
        question=str(state.get("question") or ""),
        anchor_query=resolved_query or None,
        is_followup=looks_like_followup(
            str(state.get("question") or ""), state.get("history") or []
        ),
        corpus_text=await kb_corpus_text(),
        score_threshold=sufficiency_threshold(state),
    )

    if decision.reject:
        # 拒答必须同时是「低置信度」：否则会出现「状态=未覆盖、置信度=0.86」这种
        # 自相矛盾的展示（前端气泡上尤其刺眼）。上限压到中档线以下。
        confidence = min(confidence, round(settings.confidence_medium - 0.01, 4))
        label = confidence_label(confidence, settings=settings)

    notes = build_uncertain_notes(decision, check)
    previous = (state.get("answer_structured") or {}).get("citation_enforcement") or {}
    if not enforcement:
        enforcement = previous
    if enforcement.get("dropped"):
        notes.append(
            f"有 {enforcement['dropped']} 句结论因未在材料中找到依据被省略"
            f"（引用归属阈值 {enforcement.get('threshold')}）"
        )
    out: dict[str, Any] = {
        "confidence": confidence,
        "confidence_label": label,
        "status": decision.status,
        "uncertain": notes,
        "timings": _elapsed(started, "verify"),
    }
    # 无论放行还是拒答，都回写最终答案：verify 里可能又做过一次引用归属，
    # 若不回写，流出去的就是「校验过的答案」和「发出去的答案」不一致
    out["answer"] = answer
    if decision.reject:
        # 拒答：清空引用、改写答案，绝不给出无依据的操作步骤
        out["citations"] = []
        out["answer"] = build_not_covered_answer(decision)
    else:
        out["citations"] = citations

    structured = dict(state.get("answer_structured") or {})
    structured["verification"] = {
        "coverage": round(check.coverage, 4),
        "total_claims": check.total_claims,
        "cited_claims": check.cited_claims,
        "fake_citation_ids": check.fake_ids,
        "unused_block_ids": check.unused_ids,
        "normalized_top_score": round(normalized, 4),
        "authority": round(authority, 4),
        "rejected": decision.reject,
        "reject_reasons": decision.reasons,
    }
    out["answer_structured"] = structured
    return out


register_node(NodeName.GENERATE, generate)
register_node(NodeName.VERIFY, verify)

__all__ = ["generate", "load_prompt", "render_prompt", "verify"]
