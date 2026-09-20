"""共享 LLM 客户端（新业务 service 层复用）。

为什么单独一个模块：三块新业务（维护计划、备件采购、考核认证）都要调文本模型，
而原有调用点 `agents/nodes/generation.py::_call_llm` 是节点私有实现。
这里抽一个薄的共享客户端，避免每个 service 各写一份 httpx 调用。

约定（与既有链路一致）：
- **没有配置 `DEEPSEEK_API_KEY` 时不报错、不阻塞**：`chat()` 抛 `LLMUnavailable`，
  调用方自行降级（例如考核题退化为"从原文抽句生成填空题"），这与项目"降级不中断"一致；
- temperature 默认 0（可复现优先）；
- 只在 service 层使用，节点仍走 `generation.py` 的既有实现（不改契约）。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Sequence

import httpx

from ..setting import get_settings

logger = logging.getLogger(__name__)

#: 从 <code>```json ... ```</code> 里抠出 JSON（模型常带围栏）
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class LLMUnavailable(RuntimeError):
    """未配置文本模型，或调用失败且调用方要求必须成功。"""


def available() -> bool:
    """是否配置了文本模型密钥。"""

    return bool(get_settings().deepseek_api_key)


async def chat(
    messages: Sequence[dict[str, str]],
    *,
    temperature: float = 0.0,
    timeout_s: float | None = None,
) -> str:
    """调用 DeepSeek（OpenAI 兼容接口），返回纯文本内容。"""

    settings = get_settings()
    if not settings.deepseek_api_key:
        raise LLMUnavailable("未配置 DEEPSEEK_API_KEY")

    url = f"{settings.deepseek_base_url or 'https://api.deepseek.com'}/chat/completions"
    payload = {
        "model": settings.deepseek_model,
        "messages": list(messages),
        "temperature": temperature,
    }
    headers = {
        "Authorization": f"Bearer {settings.deepseek_api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=timeout_s or settings.answer_timeout_s) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
    return str(data["choices"][0]["message"]["content"]).strip()


def parse_json(text: str) -> Any:
    """从模型输出里解析 JSON（容忍 ```json 围栏与前后废话）。"""

    raw = (text or "").strip()
    fence = _FENCE_RE.search(raw)
    if fence:
        raw = fence.group(1).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # 退一步：截取第一个 [ 或 { 到最后一个配对括号
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = raw.find(opener), raw.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError(f"模型输出不是合法 JSON：{raw[:120]}")


async def chat_json(
    messages: Sequence[dict[str, str]],
    *,
    temperature: float = 0.0,
    timeout_s: float | None = None,
) -> Any:
    """调用模型并解析 JSON；解析失败抛 ValueError（由调用方决定是否降级）。"""

    return parse_json(await chat(messages, temperature=temperature, timeout_s=timeout_s))


__all__ = ["LLMUnavailable", "available", "chat", "chat_json", "parse_json"]
