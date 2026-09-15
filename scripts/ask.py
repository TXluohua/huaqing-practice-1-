#!/usr/bin/env python3
"""命令行单问（开发文档 6.2 的 scripts/ask.py）—— 验证 LangGraph 链路是否打通。

用法：
    python3 scripts/ask.py "刻蚀机真空度异常怎么排查？"
    python3 scripts/ask.py "..." --session demo-1 --image-id img_001
    python3 scripts/ask.py "..." --stream          # 走流式链路
    python3 scripts/ask.py "..." --json            # 打印完整状态

骨架阶段说明：
    图中尚无节点，因此 answer 为空字符串、status 为 NOT_COVERED（fail-closed 初始值）。
    本脚本的价值在于证明 compile -> invoke -> 检查点 -> 返回 这条链路已经打通：
    节点接入后，同一条命令即可直接看到真实答案。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.agents.agent_factory import (  # noqa: E402
    ainvoke,
    astream,
    close_agent,
    graph_summary,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="知识库问答命令行调试（LangGraph 链路自检）")
    parser.add_argument("question", help="用户问题")
    parser.add_argument("--session", default=None, help="会话 ID（缺省则新建）")
    parser.add_argument("--image-id", action="append", default=None, help="图片 ID，可重复")
    parser.add_argument("--stream", action="store_true", help="走流式链路（astream）")
    parser.add_argument("--mode", default="values", help="流模式，默认 values")
    parser.add_argument("--json", action="store_true", help="输出完整状态 JSON")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出 DEBUG 日志")
    return parser


async def run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.stream:
        # 流式链路：先跑，再取最后一份状态；同时证明 SSE 的取数通道可用
        chunks = 0
        result: dict = {}
        async for chunk in astream(
            args.question,
            session_id=args.session,
            image_ids=args.image_id,
            stream_mode=args.mode,
        ):
            chunks += 1
            if isinstance(chunk, dict):
                result = chunk
        print(f"流式链路：收到 {chunks} 个 chunk（stream_mode={args.mode}）")
    else:
        result = await ainvoke(
            args.question,
            session_id=args.session,
            image_ids=args.image_id,
        )

    summary = graph_summary()
    print("-" * 68)
    print(f"图名称      : {summary['name']}")
    print(f"检查点后端  : {summary['checkpointer']}（落盘={summary['persistent']}）")
    print(f"已接入节点  : {summary['nodes'] or '无（骨架阶段，START -> END 直连）'}")
    print(f"trace_id    : {result.get('trace_id')}")
    print(f"session_id  : {result.get('session_id')}")
    print(f"thread_id   : {result.get('thread_id')}")
    print(f"问题        : {result.get('question')}")
    print(f"状态        : {result.get('status')}  置信度={result.get('confidence')}")
    print(f"答案        : {result.get('answer') or '（空 —— 尚未接入 generate 节点）'}")
    print(f"引用数      : {len(result.get('citations') or [])}")
    print("-" * 68)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    await close_agent()
    return 0


def main() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
