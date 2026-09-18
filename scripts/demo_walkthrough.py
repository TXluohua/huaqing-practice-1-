#!/usr/bin/env python
"""三个演示场景走查（收尾任务 T7）。

用法
    make dev                                            # 另开终端起后端
    .venv/bin/python scripts/demo_walkthrough.py
    .venv/bin/python scripts/demo_walkthrough.py --base-url http://127.0.0.1:8010

为什么写成脚本而不是手动点一遍
    D8 交付要求「每场景记录 SSE 事件序列、耗时、状态、引用数」。手点只能留下截图，
    出错时无法复盘是哪个事件缺了、哪一步变慢。脚本把每个场景的**事件序列**原样落盘，
    评审时能直接看到 `meta → token* → citations → done` 有没有按契约走。

四个场景
    A 抢修     报报警码，拿带引用的处置步骤
    B 新人学习  mode=training，拿分层讲解
    C 参数+备件 参数查询（含数值）与备件台账查询（含库存/到货/替代件）
    D 拒答      问不存在的型号，验证 fail-closed

场景 D 是**必须过**的一条：拒答是特性不是故障，走查里少了它，等于没有验证底线。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Scenario:
    key: str
    title: str
    question: str
    mode: str = "qa"
    #: 期望的状态：OK 或 NOT_COVERED
    expect: str = "OK"
    #: 该场景至少要命中这些关键词之一（人工判读用，脚本只提示不判死）
    look_for: tuple[str, ...] = ()
    device_model: str | None = None


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        key="A",
        title="抢修：报警码 → 带引用的处置步骤",
        question="刻蚀机报真空度报警 E-2041，应该怎么处理？",
        look_for=("O-ring", "干泵", "罗茨泵", "1.0 Pa"),
    ),
    Scenario(
        key="B",
        title="新人学习：培训模式分层讲解",
        question="新手想弄懂刻蚀机的真空系统：它由哪些泵组成、各自作用是什么、日常怎么维护？",
        mode="training",
        look_for=("干泵", "罗茨泵", "分子泵"),
    ),
    Scenario(
        key="C1",
        title="参数查询：拿数值 + 引用",
        question="CVD-200 的 LPCVD 氮化硅沉积温度窗口是多少？超出窗口会怎样？",
        look_for=("750", "800"),
    ),
    Scenario(
        key="C2",
        title="备件查询：拿库存/到货/替代件依据",
        question="腔体门 O-ring 还有库存吗？有没有替代件、多久到货？",
        look_for=("FFKM", "库存", "到货", "SP-ETA-0101"),
    ),
    Scenario(
        key="D",
        title="拒答：不存在的型号必须 fail-closed",
        question="XH-900 型号腔体清洗周期是多少？",
        expect="NOT_COVERED",
    ),
)


@dataclass
class Outcome:
    scenario: Scenario
    status: str | None = None
    latency_s: float = 0.0
    first_token_s: float | None = None
    n_citations: int = 0
    citations: list[dict] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    answer: str = ""
    confidence: float | None = None
    uncertain: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.error is None and self.status == self.scenario.expect


async def run_scenario(client: httpx.AsyncClient, sc: Scenario, timeout_s: float) -> Outcome:
    out = Outcome(scenario=sc)
    payload: dict = {"question": sc.question, "mode": sc.mode}
    if sc.device_model:
        payload["device_model"] = sc.device_model

    started = time.perf_counter()
    try:
        async with client.stream(
            "POST",
            "/api/chat/stream",
            json=payload,
            headers={"Accept": "text/event-stream", "X-User-Id": "demo-walkthrough"},
            timeout=timeout_s,
        ) as resp:
            if resp.status_code != 200:
                out.error = f"HTTP {resp.status_code}"
                return out
            event_name: str | None = None
            async for raw in resp.aiter_lines():
                if raw.startswith("event:"):
                    event_name = raw.split(":", 1)[1].strip()
                    continue
                if not raw.startswith("data:"):
                    continue
                body = raw.split(":", 1)[1].strip()
                if event_name:
                    out.events.append(event_name)
                if event_name == "token" and out.first_token_s is None:
                    out.first_token_s = time.perf_counter() - started
                    try:
                        out.answer += json.loads(body).get("delta", "")
                    except json.JSONDecodeError:
                        pass
                elif event_name == "citations":
                    try:
                        out.citations = json.loads(body).get("items", [])
                        out.n_citations = len(out.citations)
                    except json.JSONDecodeError:
                        pass
                elif event_name == "done":
                    try:
                        data = json.loads(body)
                        out.status = data.get("status")
                        out.confidence = data.get("confidence")
                        out.uncertain = data.get("uncertain") or []
                    except json.JSONDecodeError:
                        out.status = "PARSE_ERROR"
                    break
                elif event_name == "error":
                    out.error = body[:200]
                    break
                event_name = None
    except (httpx.TimeoutException, httpx.HTTPError) as exc:
        out.error = f"{type(exc).__name__}: {exc}"
    finally:
        out.latency_s = time.perf_counter() - started
    return out


def summarize(events: list[str]) -> str:
    """把事件序列压缩成 meta → token×12 → citations → done 这样的可读形式。"""

    if not events:
        return "（无事件）"
    parts: list[str] = []
    for name in events:
        if parts and parts[-1][0] == name:
            parts[-1][1] += 1
        else:
            parts.append([name, 1])
    return " → ".join(f"{n}×{c}" if c > 1 else n for n, c in parts)


async def main() -> int:
    parser = argparse.ArgumentParser(description="三个演示场景走查")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    print(f"目标：{args.base_url}\n")
    results: list[Outcome] = []
    async with httpx.AsyncClient(base_url=args.base_url) as client:
        print("预热中（模型加载，不计入场景耗时）…")
        await run_scenario(client, SCENARIOS[0], args.timeout)

        for sc in SCENARIOS:
            print(f"\n{'=' * 74}\n场景 {sc.key}：{sc.title}\n{'=' * 74}")
            print(f"问：{sc.question}   （mode={sc.mode}）")
            out = await run_scenario(client, sc, args.timeout)
            results.append(out)

            flag = "✓" if out.passed else "✗"
            print(f"{flag} status={out.status}  耗时 {out.latency_s:.2f}s  引用 {out.n_citations} 条")
            print(f"  事件序列：{summarize(out.events)}")
            if out.error:
                print(f"  错误：{out.error}")
            if out.answer:
                print(f"  答案：{out.answer[:220].replace(chr(10), ' / ')}")
            if out.uncertain:
                print(f"  提示：{out.uncertain[0][:110]}")
            if out.citations:
                heads = [
                    f"{c.get('doc_title') or c.get('doc')}#{c.get('section')}"
                    for c in out.citations[:4]
                ]
                print(f"  引用：{'、'.join(h for h in heads if h)}")

            if sc.look_for and out.answer:
                hit = [k for k in sc.look_for if k in out.answer]
                print(f"  关键词命中：{hit or '（无，需人工判读）'}")

    n_pass = sum(1 for r in results if r.passed)
    print(f"\n{'=' * 74}")
    print(f"走查结果：{n_pass}/{len(results)} 个场景符合预期")
    for r in results:
        if not r.passed:
            print(f"  ✗ 场景 {r.scenario.key}：期望 {r.scenario.expect}，实际 {r.status or r.error}")

    out_path = args.out or ROOT / "data" / "eval" / f"demo_{datetime.now():%Y%m%d_%H%M%S}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "base_url": args.base_url,
                "n_pass": n_pass,
                "n_total": len(results),
                "scenarios": [
                    {
                        "key": r.scenario.key,
                        "title": r.scenario.title,
                        "question": r.scenario.question,
                        "mode": r.scenario.mode,
                        "expect": r.scenario.expect,
                        "passed": r.passed,
                        "status": r.status,
                        "latency_s": round(r.latency_s, 2),
                        "first_token_s": round(r.first_token_s, 2) if r.first_token_s else None,
                        "n_citations": r.n_citations,
                        "event_sequence": summarize(r.events),
                        "answer": r.answer,
                        "confidence": r.confidence,
                        "uncertain": r.uncertain,
                        "citations": [
                            {
                                "doc": c.get("doc_title") or c.get("doc"),
                                "section": c.get("section"),
                                "page": c.get("page"),
                                "source_type": c.get("source_type"),
                            }
                            for c in r.citations
                        ],
                        "error": r.error,
                    }
                    for r in results
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n走查记录已写入：{out_path}")
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
