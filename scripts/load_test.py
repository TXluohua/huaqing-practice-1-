#!/usr/bin/env python
"""20 并发压测（收尾任务 T10 / NFR-02）。

用法
    make dev                                   # 另开一个终端起后端
    .venv/bin/python scripts/load_test.py                        # 默认 20 并发
    .venv/bin/python scripts/load_test.py -n 20 --rounds 3       # 跑 3 轮取更稳的结论
    .venv/bin/python scripts/load_test.py --base-url http://127.0.0.1:8010

为什么并发下必须单独测
    单条延迟（scripts/eval.py 的 P95 ≈ 5s）说明不了并发能力：精排是 **CPU 密集**的
    Cross-Encoder，每次约 4.9s，且模型推理在进程内是**串行**的。20 个请求同时到达时，
    真正的瓶颈不是网络也不是 LLM，而是排队的精排 —— 所以并发 P95 会明显高于单条 P95，
    这不是 bug，是必须如实写进交付报告的容量结论。

测什么
    打 `POST /api/chat/stream`（SSE），逐条记录：
      成功（收到 done 事件）/ 失败（HTTP 非 200 / 链路 error 事件 / 超时）
      首 Token 延迟（第一个 token 事件到达的时刻）
      总耗时、status、引用数
    最后给出成功率、首 Token P50/P95、总耗时 P50/P95、错误率。

关于「首 Token」
    本实现**先过 verify 再出字**（见 接口文档 §8 缺口②），所以首 Token ≈ 整条链路耗时，
    首 Token 与总耗时的差值天然很小。这是设计取舍，不是流式失效。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]

#: 默认压测问题：混合文档类与备件类，避免全是同一条缓存/同一条路径
DEFAULT_QUESTIONS: tuple[str, ...] = (
    "刻蚀机腔体真空度异常波动，应该按什么顺序排查？",
    "刻蚀机报真空度报警 E-2041，触发条件是什么，应该怎么处理？",
    "Etcher-A 腔体门 O-ring 的规格和更换周期是什么？",
    "CVD-200 的 LPCVD 氮化硅沉积温度窗口是多少？",
    "进入设备腔体维护前，上锁挂牌 LOTO 必须按什么顺序执行？",
)


@dataclass
class RequestResult:
    index: int
    ok: bool
    question: str
    first_token_s: float | None = None
    total_s: float = 0.0
    status: str | None = None
    n_citations: int = 0
    http_code: int | None = None
    error: str | None = None
    events: list[str] = field(default_factory=list)


async def one_request(
    client: httpx.AsyncClient,
    index: int,
    question: str,
    timeout_s: float,
) -> RequestResult:
    """打一条 /api/chat/stream，解析 SSE 直到 done / error。"""

    result = RequestResult(index=index, ok=False, question=question)
    started = time.perf_counter()
    payload = {"question": question, "mode": "qa"}
    try:
        async with client.stream(
            "POST",
            "/api/chat/stream",
            json=payload,
            headers={"Accept": "text/event-stream", "X-User-Id": f"loadtest-{index}"},
            timeout=timeout_s,
        ) as resp:
            result.http_code = resp.status_code
            if resp.status_code != 200:
                result.error = f"HTTP {resp.status_code}"
                return result

            event_name: str | None = None
            async for raw in resp.aiter_lines():
                if raw.startswith("event:"):
                    event_name = raw.split(":", 1)[1].strip()
                    continue
                if not raw.startswith("data:"):
                    continue
                if event_name:
                    result.events.append(event_name)
                body = raw.split(":", 1)[1].strip()
                if event_name == "token" and result.first_token_s is None:
                    result.first_token_s = time.perf_counter() - started
                elif event_name == "done":
                    try:
                        data = json.loads(body)
                        result.status = data.get("status")
                        result.n_citations = len(data.get("citations") or [])
                    except json.JSONDecodeError:
                        result.status = "PARSE_ERROR"
                    result.ok = result.status not in (None, "ERROR")
                    break
                elif event_name == "error":
                    result.error = body[:200]
                    break
                event_name = None
    except (httpx.TimeoutException, httpx.HTTPError) as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        result.total_s = time.perf_counter() - started
    return result


def pct(values: list[float], p: float) -> float:
    """线性插值分位数；样本不足时退回最大值。"""

    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    k = (len(ordered) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


async def run_round(
    base_url: str, concurrency: int, timeout_s: float, questions: tuple[str, ...]
) -> tuple[list[RequestResult], float]:
    async with httpx.AsyncClient(base_url=base_url) as client:
        # 预热：首个请求要付模型加载与索引懒加载的钱，不预热会把「启动 20s」
        # 算成「并发时第一条 20s」，结论就废了
        print("预热中（模型加载 + 索引懒加载，不计入统计）…")
        warm = await one_request(client, -1, questions[0], timeout_s)
        if not warm.ok:
            print(f"  预热失败：{warm.error or warm.status}（后续结论不可信）")

        print(f"并发 {concurrency} 条开始…")
        started = time.perf_counter()
        tasks = [
            one_request(client, i, questions[i % len(questions)], timeout_s)
            for i in range(concurrency)
        ]
        results = await asyncio.gather(*tasks)
        wall = time.perf_counter() - started
    return list(results), wall


def report(results: list[RequestResult], wall: float, concurrency: int) -> dict:
    ok = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]
    firsts = [r.first_token_s for r in ok if r.first_token_s is not None]
    totals = [r.total_s for r in ok]

    print("\n" + "=" * 74)
    print(f"并发压测结果（并发数 {concurrency}）")
    print("=" * 74)
    print(f"  成功 {len(ok)} / {len(results)}    失败 {len(failed)}    错误率 {len(failed) / len(results):.1%}")
    print(f"  墙钟总耗时 {wall:.2f}s（并发串行化程度看这里：越接近单条耗时说明并行越有效）")
    if totals:
        print(f"  总耗时   P50 {statistics.median(totals):.2f}s   P95 {pct(totals, 0.95):.2f}s   max {max(totals):.2f}s")
    if firsts:
        print(f"  首 Token P50 {statistics.median(firsts):.2f}s   P95 {pct(firsts, 0.95):.2f}s   max {max(firsts):.2f}s")
        print("  （本实现「先过 verify 再出字」，首 Token ≈ 整条链路耗时，两者接近属预期）")

    statuses: dict[str, int] = {}
    for r in ok:
        statuses[str(r.status)] = statuses.get(str(r.status), 0) + 1
    if statuses:
        print(f"  status 分布：{statuses}")

    if failed:
        print("\n  失败明细：")
        for r in failed[:10]:
            print(f"    #{r.index} {r.error or '未知'}（HTTP {r.http_code}，耗时 {r.total_s:.2f}s）")

    # 容量结论：用 NFR 的分路径目标当标尺，而不是拍一个"60s 就算慢"的整数。
    # 纯文档目标 ≤8s、带外部源 ≤15s（见收尾清单 §5 指标看板），并发下用户感知的是
    # 端到端等待，所以拿 15s（较宽的那档）当"可接受"的分界线。
    p95 = pct(totals, 0.95) if totals else 0.0
    err = len(failed) / max(len(results), 1)
    if err > 0.05:
        verdict = f"不能支撑（错误率 {err:.0%} > 5%）"
    elif p95 > 60:
        verdict = f"不能支撑（P95 {p95:.0f}s，用户可感知为卡死；见收尾清单 T11）"
    elif p95 > 15:
        verdict = f"勉强支撑（P95 {p95:.0f}s，超出 NFR 的 15s 上限）"
    else:
        verdict = f"可以支撑（P95 {p95:.0f}s，在 NFR 上限内）"
    print(f"\n  结论：{concurrency} 并发下{verdict}。")

    return {
        "concurrency": concurrency,
        "n_ok": len(ok),
        "n_failed": len(failed),
        "error_rate": round(len(failed) / len(results), 4),
        "wall_s": round(wall, 3),
        "total_p50_s": round(statistics.median(totals), 3) if totals else None,
        "total_p95_s": round(pct(totals, 0.95), 3) if totals else None,
        "first_token_p50_s": round(statistics.median(firsts), 3) if firsts else None,
        "first_token_p95_s": round(pct(firsts, 0.95), 3) if firsts else None,
        "status_counts": statuses,
        "verdict": verdict,
        "failures": [
            {"index": r.index, "error": r.error, "http_code": r.http_code, "total_s": round(r.total_s, 2)}
            for r in failed
        ],
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="20 并发压测（NFR-02）")
    parser.add_argument("-n", "--concurrency", type=int, default=20, help="并发数，默认 20")
    parser.add_argument("--rounds", type=int, default=1, help="轮数，多轮取 P95 最大值")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="后端地址")
    parser.add_argument("--timeout", type=float, default=300.0, help="单请求超时（秒）")
    parser.add_argument("--out", type=Path, default=None, help="结果 JSON 输出路径")
    args = parser.parse_args()

    print(f"目标：{args.base_url}/api/chat/stream    并发 {args.concurrency} × {args.rounds} 轮")
    rounds: list[dict] = []
    for i in range(args.rounds):
        if args.rounds > 1:
            print(f"\n----- 第 {i + 1} / {args.rounds} 轮 -----")
        results, wall = await run_round(
            args.base_url, args.concurrency, args.timeout, DEFAULT_QUESTIONS
        )
        rounds.append(report(results, wall, args.concurrency))

    # 多轮时用 P95 最差的一轮作为结论（容量评估看最坏情况）
    worst = max(rounds, key=lambda r: r.get("total_p95_s") or 0)
    if args.rounds > 1:
        print("\n" + "=" * 74)
        print(f"多轮汇总：最差一轮 P95 {worst.get('total_p95_s')}s，错误率 {worst['error_rate']:.1%}")

    out_path = args.out or ROOT / "data" / "eval" / f"loadtest_{datetime.now():%Y%m%d_%H%M%S}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "base_url": args.base_url,
                "rounds": rounds,
                "worst_round": worst,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n结果已写入：{out_path}")
    return 0 if worst["n_failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
