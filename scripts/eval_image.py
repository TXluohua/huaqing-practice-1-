#!/usr/bin/env python
"""图片类黄金集评测（收尾任务 T3）。

用法
    .venv/bin/python scripts/eval_image.py                 # 跑 data/eval/golden_image.jsonl
    .venv/bin/python scripts/eval_image.py --ids img003    # 只跑某几条
    .venv/bin/python scripts/eval_image.py --dry-run       # 只校验 fixture 是否齐备

为什么单独一个脚本，而不是并进 scripts/eval.py
    `scripts/eval.py` 是 RAG 侧文件（分工规范里明确不跨改）。图片类评测的驱动方式也不同：
    文字类直接把 question 丢进图，图片类要先**把 fixture 装进 upload_dir 换一个 image_id**，
    再让链路按 image_id 去取图。两者混在一个脚本里会让「文字类 P95」被多模态 API 的
    抖动污染 —— 那正是要分开看的指标。

关于「装图片」
    生产路径是 `POST /api/upload/image` 落盘为 {upload_dir}/{image_id}.{ext}。
    评测脚本不开服务，所以直接把 fixture 复制成同样的命名，走完全相同的
    `_resolve_image_path` 解析逻辑。复制而非软链，是为了不受 fixture 目录被清理的影响。

img003 的判定口径
    它是负样本，期望「识别不出来」。判定不是看它有没有输出文字，而是两条同时成立：
      (1) 链路 status == NOT_COVERED（fail-closed，没有硬答）；
      (2) errors 里出现「请用文字描述现象」的提示。
    只要模型报出了任何具体文字/数值（哪怕碰巧对），本条即判不通过 ——
    因为「猜对了」和「读出来了」在工程上不可区分，而这条样例守的是「不猜」。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.agents.graph import compile_graph  # noqa: E402
from backend.agents.state import make_initial_state  # noqa: E402
from backend.setting import get_settings  # noqa: E402

GOLDEN = ROOT / "data" / "eval" / "golden_image.jsonl"
FIXTURE_DIR = ROOT / "data" / "eval" / "fixtures"


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def stage_fixture(item: dict[str, Any], upload_dir: Path) -> tuple[str | None, str | None]:
    """把样例图片复制进 upload_dir，返回 (image_id, 错误)。"""

    rel = str(item.get("image") or "")
    src = FIXTURE_DIR / Path(rel).name
    if not src.is_file():
        return None, f"fixture 缺失：{src}"
    upload_dir.mkdir(parents=True, exist_ok=True)
    # image_id 用 golden_ 前缀，避免和真实上传的文件撞名
    image_id = f"golden_{item.get('id')}"
    dst = upload_dir / f"{image_id}{src.suffix}"
    if not dst.is_file() or dst.stat().st_mtime < src.stat().st_mtime:
        shutil.copyfile(src, dst)
    return image_id, None


def flatten(value: Any) -> str:
    """把 extracted / raw_text 拍平成一个可搜索的字符串。"""

    if isinstance(value, dict):
        return " ".join(flatten(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return " ".join(flatten(v) for v in value)
    return str(value or "")


def check_expected(expect: dict[str, Any], haystack: str) -> list[str]:
    """逐项校验 expect_extract，返回未命中的描述列表。"""

    missing: list[str] = []
    for key, want in (expect or {}).items():
        if key == "unrecognizable":  # 负样本，单独判
            continue
        targets = want if isinstance(want, list) else [want]
        for target in targets:
            if isinstance(target, dict):  # 如 {"gas": "SiH4", "value": 350}
                ok = all(str(v) in haystack for v in target.values())
                desc = " / ".join(str(v) for v in target.values())
            else:
                ok = str(target) in haystack
                desc = str(target)
            if not ok:
                missing.append(f"{key}={desc}")
    return missing


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    settings.ensure_dirs()

    if not GOLDEN.exists():
        print(f"图片黄金集不存在：{GOLDEN}")
        return 1
    items = load_jsonl(GOLDEN)
    if args.ids:
        wanted = {x.strip() for x in args.ids.split(",") if x.strip()}
        items = [it for it in items if str(it.get("id")) in wanted]
    if not items:
        print("没有可评测的样例")
        return 1

    # 先检查 fixture，缺图就别浪费时间加载模型
    staged: list[tuple[dict[str, Any], str]] = []
    for item in items:
        image_id, err = stage_fixture(item, Path(settings.upload_dir))
        if err:
            print(f"  [跳过] {item.get('id')}：{err}")
            continue
        staged.append((item, image_id))
    if not staged:
        print("\n没有可用样例。先生成 fixture：")
        print("  .venv/bin/python scripts/make_image_fixtures.py")
        return 1
    if args.dry_run:
        print(f"\nfixture 齐备，共 {len(staged)} 条可评测：")
        for item, image_id in staged:
            print(f"  {item.get('id'):8s} {item.get('image')} -> image_id={image_id}")
        return 0

    graph = compile_graph()
    print(f"开始图片类评测：{len(staged)} 条")

    results: list[dict[str, Any]] = []
    for item, image_id in staged:
        question = str(item.get("question", ""))
        negative = bool(item.get("negative"))
        state = make_initial_state(question, answer_mode="qa", image_ids=[image_id])
        started = time.perf_counter()
        try:
            out = await graph.ainvoke(state)
            error: str | None = None
        except Exception as exc:  # noqa: BLE001 - 单条失败不中断整轮
            out, error = {}, f"{type(exc).__name__}: {exc}"
        latency = time.perf_counter() - started

        extraction = out.get("image_result")
        raw_text = getattr(extraction, "raw_text", "") or ""
        extracted = getattr(extraction, "extracted", {}) or {}
        confidence = float(getattr(extraction, "confidence", 0.0) or 0.0)
        haystack = flatten(extracted) + " " + raw_text
        status = out.get("status")
        errors = [str(e) for e in (out.get("errors") or [])]
        answer = str(out.get("answer") or "")
        prompt_hint = any("请用文字描述现象" in e for e in errors)

        if error:
            passed, reason = False, f"链路异常：{error}"
        elif negative:
            # 负样本：既要 fail-closed，又不能猜出内容
            missing = check_expected(item.get("expect_extract") or {}, haystack)
            guessed = bool(haystack.strip())
            if status != "NOT_COVERED":
                passed, reason = False, f"未拒答（status={status}），负样本必须 fail-closed"
            elif guessed:
                passed, reason = False, f"对不可识别图片给出了内容：{haystack[:80]}"
            elif not prompt_hint:
                passed, reason = False, "拒答但未提示「请用文字描述现象」"
            else:
                passed, reason = True, "正确拒答 + 提示改用文字"
            del missing
        else:
            missing = check_expected(item.get("expect_extract") or {}, haystack)
            if missing:
                passed, reason = False, f"未抽取到：{'、'.join(missing)}"
            elif status != "OK":
                passed, reason = False, f"抽取成功但链路未作答（status={status}）"
            elif not answer.strip():
                passed, reason = False, "链路无答案正文"
            else:
                passed, reason = True, "抽取与作答均正常"

        results.append(
            {
                "id": item.get("id"),
                "question": question,
                "negative": negative,
                "image_id": image_id,
                "status": status,
                "passed": passed,
                "reason": reason,
                "latency_s": round(latency, 2),
                "image_type": getattr(extraction, "image_type", None),
                "confidence": round(confidence, 3),
                "extracted": extracted,
                "raw_text": raw_text[:500],
                "errors": errors,
                "answer_head": answer[:300],
                "n_citations": len(out.get("citations") or []),
            }
        )
        flag = "✓" if passed else "✗"
        print(f"  {flag} {item.get('id'):8s} {status or '-':12s} {latency:5.2f}s  {reason}")

    n_pass = sum(1 for r in results if r["passed"])
    print("\n" + "=" * 78)
    print(f"图片类评测：{n_pass}/{len(results)} 通过")
    for r in results:
        if not r["passed"]:
            print(f"  ✗ {r['id']}：{r['reason']}")

    out_path = args.out or ROOT / "data" / "eval" / f"report_image_{datetime.now():%Y%m%d_%H%M%S}.json"
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "golden": str(GOLDEN),
        "fixture_dir": str(FIXTURE_DIR),
        "summary": {"n_items": len(results), "n_passed": n_pass, "all_passed": n_pass == len(results)},
        "items": results,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已写入：{out_path}")
    return 0 if n_pass == len(results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="图片类黄金集评测")
    parser.add_argument("--ids", default=None, help="只跑指定 id，逗号分隔")
    parser.add_argument("--out", type=Path, default=None, help="报告 JSON 输出路径")
    parser.add_argument("--dry-run", action="store_true", help="只校验 fixture，不跑模型")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
