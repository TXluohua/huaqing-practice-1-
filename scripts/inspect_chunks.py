#!/usr/bin/env python
"""查看切片（调分块参数时用；开发文档 6.2）。

用法
    .venv/bin/python scripts/inspect_chunks.py --stats            # 只打印索引统计
    .venv/bin/python scripts/inspect_chunks.py --doc 刻蚀          # 按文档名过滤
    .venv/bin/python scripts/inspect_chunks.py --q 真空度 --limit 5 # 按内容模糊搜索
    .venv/bin/python scripts/inspect_chunks.py --page 3           # 按页码过滤
    .venv/bin/python scripts/inspect_chunks.py --id c_1a2b3c4d    # 查看单个切片全文

调分块参数的典型流程：改 `setting.chunk_size` / `chunk_overlap` → 重建索引 →
用本脚本抽查切片边界是否切断步骤列表或参数表。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.rag.store import get_store, reset_store  # noqa: E402
from backend.setting import get_settings  # noqa: E402


def _print_chunk(chunk, *, full: bool = False) -> None:
    meta = (
        f"《{chunk.doc_title}》{chunk.version} P.{chunk.page} "
        f"章节 {chunk.section or '-'} {chunk.heading or ''}"
    ).strip()
    print(f"  {chunk.chunk_id}  {meta}")
    print(f"    {chunk.device_model or '-'} / {chunk.category or '-'} / {chunk.n_chars} 字 / {chunk.n_tokens} token")
    text = chunk.text if full else chunk.text[:180].replace("\n", " ")
    suffix = "" if full or len(chunk.text) <= 180 else " …"
    print(f"    {text}{suffix}")
    print()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="查看知识库切片")
    parser.add_argument("--doc", default=None, help="按文档名模糊过滤")
    parser.add_argument("--q", default=None, help="按切片内容模糊搜索")
    parser.add_argument("--page", type=int, default=None, help="按页码过滤")
    parser.add_argument("--section", default=None, help="按章节号过滤，如 3.4")
    parser.add_argument("--id", default=None, help="查看指定 chunk_id 的全文（含相邻块）")
    parser.add_argument("--limit", type=int, default=10, help="最多显示条数")
    parser.add_argument("--stats", action="store_true", help="只打印索引统计")
    parser.add_argument("--rebuild", action="store_true", help="先清空索引（配合 --q 前重建用）")
    return parser


async def run(args: argparse.Namespace) -> int:
    if args.rebuild:
        reset_store()
    store = await get_store()
    stats = await store.stats()

    print("=" * 78)
    print("索引统计")
    print("=" * 78)
    print(f"  后端 {stats['backend']} / 向量化 {stats['provider']}（{stats['model']}，{stats['dim']} 维）")
    print(f"  切片 {stats['n_chunks']} 个 / 文档版本 {stats['n_documents']} 个")
    print(f"  文档：{', '.join(stats['documents']) or '（空）'}")
    print(f"  平均 {stats['avg_chars']} 字 / {stats['avg_tokens']} token，最大 {stats['max_tokens']} token")
    print(f"  页码集合：{stats['pages']}")
    print("=" * 78)
    if args.stats:
        return 0

    if args.id:
        chunks = await store.neighbors(args.id, count=1)
        if not chunks:
            print(f"未找到切片：{args.id}")
            return 1
        print(f"切片 {args.id} 及其相邻块：\n")
        for chunk in chunks:
            _print_chunk(chunk, full=True)
        return 0

    chunks = await store.all_chunks()
    if args.doc:
        chunks = [c for c in chunks if args.doc in c.doc_title]
    if args.page is not None:
        chunks = [c for c in chunks if c.page == args.page]
    if args.section:
        chunks = [c for c in chunks if c.section == args.section]
    if args.q:
        needle = args.q.strip()
        chunks = [c for c in chunks if needle in c.text or needle in c.heading]

    print(f"命中 {len(chunks)} 个切片，显示前 {min(len(chunks), args.limit)} 个：\n")
    for chunk in chunks[: args.limit]:
        _print_chunk(chunk)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    get_settings().ensure_dirs()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
