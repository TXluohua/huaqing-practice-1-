"""pytest 全局夹具。

测试必须**离线可复现**。本机 `.env` 里已配置 `DEEPSEEK_API_KEY` /
`DASHSCOPE_API_KEY`，若不屏蔽，跑整图的用例（`test_factory_ainvoke_and_stream`）
和抽句式降级用例（`test_generate_fallback_attaches_citation_to_every_sentence`）
会因为「有密钥」而改走云端 LLM 分支 —— 既真的发起网络请求、花配额，
又让断言随网络与账号状态漂移（实测：配密钥后前者静默打真实接口，后者直接失败）。

需要验证「已配置密钥」行为的用例，在自己的用例内显式打开再还原，例如：

    settings = get_settings()
    saved = settings.deepseek_api_key
    settings.deepseek_api_key = "sk-test"
    try:
        ...
    finally:
        settings.deepseek_api_key = saved
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from backend.setting import get_settings  # noqa: E402

#: 夹具默认清空的云端密钥字段（向量化与精排走本地模型，不受影响）
_CLOUD_KEYS = ("deepseek_api_key", "dashscope_api_key")


@pytest.fixture(autouse=True)
def _no_cloud_keys() -> Iterator[None]:
    """默认清空云端密钥：用例走本地降级路径，不产生任何外部调用。"""

    settings = get_settings()
    saved = {name: getattr(settings, name) for name in _CLOUD_KEYS}
    for name in _CLOUD_KEYS:
        setattr(settings, name, "")
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(settings, name, value)
