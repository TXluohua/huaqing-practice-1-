#!/usr/bin/env python
"""黄金集自检：防止 must_cite 写错、answer_points 不可命中（收尾任务 T5）。

用法
    .venv/bin/python scripts/check_golden.py
    .venv/bin/python scripts/check_golden.py --stats     # 只看构成比例

为什么需要这个脚本
    黄金集是**评测的标尺**。`must_cite` 写成 `某手册#9.9`（知识库里根本没有这一节）时，
    评测不会报错 —— 它只会安静地把 Top-5 命中率算成 0，然后让人误以为「检索退化了」。
    反过来，`answer_points` 写得比原文还离谱，要点命中率也会莫名其妙掉下来。
    这两种错误都必须**在跑评测之前**暴露，而不是在报告里以一个可疑的数字出现。

检查项
    1. 每行合法 JSON，字段完整（正样本要 answer_points，负样本要 expect=NOT_COVERED）；
    2. id 唯一；
    3. `must_cite` 形如 `文档名#章节号`，且该章节号**真实存在于 data/raw/<文档名>.md**；
    4. `answer_points` 的每个要点，在它所引用的那个章节正文里能命中（二元组重叠 ≥ 0.5）——
       命不中的多半是写错了节号，或者把别的文档的内容记串了。

第 4 条是最有价值的一条：它把「要点命中率」这个指标的可达性，在写题的时候就锁死了。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "data" / "eval" / "golden_qa.jsonl"
RAW_DIR = ROOT / "data" / "raw"
PUNCT = " \t\n，。、；：？！“”‘’（）()[]【】,.;:?!—－-_/\\"

#: 评分口径与 scripts/eval.py 保持一致（这里独立实现，避免跨改 RAG 侧文件）
BIGRAM_HIT = 0.5


def normalize(text: str) -> str:
    out = (text or "").lower()
    for ch in PUNCT:
        out = out.replace(ch, "")
    return out


def bigrams(text: str) -> set[str]:
    return {text[i : i + 2] for i in range(len(text) - 1)} if len(text) > 1 else {text}


def point_hit(point: str, haystack: str) -> bool:
    p, h = normalize(point), normalize(haystack)
    if not p:
        return False
    if p in h:
        return True
    pb, hb = bigrams(p), bigrams(h)
    return bool(pb) and len(pb & hb) / len(pb) >= BIGRAM_HIT


def load_sections() -> dict[str, dict[str, str]]:
    """{文档名: {章节号: 正文}}。

    文档名取 frontmatter 的 `doc_title`，**不是文件名** —— 黄金集里写的是
    「刻蚀设备维护手册」，而磁盘上是 `etch_maintenance_manual.md`。
    用文件名去比对会把 18 条正样本全部报成「文档不存在」，这个坑踩过一次。
    """

    docs: dict[str, dict[str, str]] = {}
    for path in sorted(RAW_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        title = path.stem
        body_lines = text.splitlines()
        # 解析 YAML frontmatter（只认 --- 包起来的头部）
        if body_lines and body_lines[0].strip() == "---":
            for idx in range(1, len(body_lines)):
                if body_lines[idx].strip() == "---":
                    for meta in body_lines[1:idx]:
                        key, _, value = meta.partition(":")
                        if key.strip() == "doc_title" and value.strip():
                            title = value.strip().strip("\"'")
                    body_lines = body_lines[idx + 1 :]
                    break

        sections: dict[str, str] = {}
        current: str | None = None
        buf: list[str] = []
        for line in body_lines:
            if line.startswith("## "):
                if current is not None:
                    sections[current] = "\n".join(buf).strip()
                head = line[3:].strip()
                # "5.2 真空度报警 E-2041 处理" -> "5.2"
                current = head.split()[0] if head else ""
                buf = [head]
            elif current is not None:
                buf.append(line)
        if current is not None:
            sections[current] = "\n".join(buf).strip()
        docs[title] = sections
    return docs


def main() -> int:
    parser = argparse.ArgumentParser(description="黄金集自检")
    parser.add_argument("--stats", action="store_true", help="只打印构成比例")
    parser.add_argument("--file", type=Path, default=GOLDEN, help="黄金集路径")
    args = parser.parse_args()

    if not args.file.exists():
        print(f"黄金集不存在：{args.file}")
        return 1

    docs = load_sections()
    known_docs = set(docs)
    lines = [ln for ln in args.file.read_text(encoding="utf-8").splitlines() if ln.strip()]

    errors: list[str] = []
    seen_ids: set[str] = set()
    categories: Counter[str] = Counter()
    items: list[dict] = []

    for lineno, line in enumerate(lines, 1):
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"第 {lineno} 行不是合法 JSON：{exc}")
            continue
        items.append(item)

        gid = str(item.get("id") or "")
        if not gid:
            errors.append(f"第 {lineno} 行缺少 id")
        elif gid in seen_ids:
            errors.append(f"第 {lineno} 行 id 重复：{gid}")
        seen_ids.add(gid)

        if not str(item.get("question") or "").strip():
            errors.append(f"{gid}：question 为空")

        category = str(item.get("category") or "未分类")
        categories[category] += 1
        if item.get("negative"):
            categories["负样本"] += 1
            if item.get("expect") != "NOT_COVERED":
                errors.append(f"{gid}：负样本的 expect 必须是 NOT_COVERED")
            if not item.get("note"):
                errors.append(f"{gid}：负样本缺少 note（说明「凭什么算无依据」）")
            continue

        points = item.get("answer_points") or []
        if not points:
            errors.append(f"{gid}：正样本缺少 answer_points")

        cites = item.get("must_cite") or []
        if not cites:
            errors.append(f"{gid}：正样本缺少 must_cite")

        # 先把 must_cite 解析成实际正文；任一条无效即整体无效
        bodies: list[tuple[str, str]] = []
        cite_ok = True
        for cite in cites:
            if "#" not in str(cite):
                errors.append(f"{gid}：must_cite 缺少 '#'：{cite!r}")
                cite_ok = False
                continue
            doc, _, section = str(cite).partition("#")
            if doc not in known_docs:
                errors.append(
                    f"{gid}：文档不存在：{doc!r}（data/raw 下只有 {sorted(known_docs)}）"
                )
                cite_ok = False
                continue
            if section not in docs[doc]:
                errors.append(
                    f"{gid}：章节不存在：{cite!r}（{doc} 只有 {sorted(docs[doc])}）"
                )
                cite_ok = False
                continue
            bodies.append((str(cite), docs[doc][section]))

        # 要点命中的口径是「**至少命中一条**被引章节」——一条答案本来就可以由多节支撑
        # （如 q009：掉速判据在手册 3.5、到货周期在备件目录 3.3）。
        # 早先要求「命中所有被引章节」，把这种正常的多节答案全判成了错误。
        if cite_ok:
            for point in points:
                if not any(point_hit(point, body) for _, body in bodies):
                    errors.append(
                        f"{gid}：要点在所有被引章节中都命不中 → {str(point)[:40]}…"
                        f"（被引：{'、'.join(c for c, _ in bodies)}）"
                    )

    n_neg = sum(1 for i in items if i.get("negative"))
    n_pos = len(items) - n_neg

    print(f"黄金集：{args.file}")
    print(f"  共 {len(items)} 条（正 {n_pos} / 负 {n_neg}）")
    dist = Counter(str(i.get("category") or "未分类") for i in items)
    print("  分类构成：" + "、".join(f"{k} {v}" for k, v in dist.most_common()))
    print(f"  负样本占比：{n_neg / len(items):.0%}" if items else "")

    if args.stats:
        return 0

    if errors:
        print(f"\n发现 {len(errors)} 个问题：")
        for err in errors:
            print(f"  ✗ {err}")
        return 1

    print("\n✓ 全部通过：章节引用真实存在，且每条要点都能在引用章节中命中。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
