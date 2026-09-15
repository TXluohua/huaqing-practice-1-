#!/usr/bin/env python
"""重建索引（FR-08：一键重建索引，验收标准 ≤ 5 分钟）。

用法
    .venv/bin/python scripts/build_index.py              # 清空并重建（默认 data/raw）
    .venv/bin/python scripts/build_index.py --keep       # 增量入库（不先清空）
    .venv/bin/python scripts/build_index.py --provider hashing   # 换向量化提供方重建

设计要点
--------
- 重建 = 清空 + 重新解析全部语料，因此结果与运行顺序无关（幂等）；
- 索引清单里会写入 provider/维度，换提供方后不重建会导致维度不匹配报错，
  本脚本就是那条报错信息所指的操作。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
for path in (str(ROOT), str(SCRIPTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

from backend.rag.ingest import default_raw_dir, iter_raw_documents  # noqa: E402
from backend.rag.store import get_store, reset_store  # noqa: E402
from backend.setting import get_settings  # noqa: E402
from ingest import ingest_all, print_report  # noqa: E402  （同目录脚本，走 sys.path）

#: 一键重建的目标上限（开发文档 FR-08）
REBUILD_BUDGET_S = 300.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="一键重建向量索引")
    parser.add_argument("--raw-dir", type=Path, default=None, help="语料目录（缺省 data/raw）")
    parser.add_argument("--keep", action="store_true", help="增量入库，不先清空索引")
    parser.add_argument(
        "--provider",
        default=None,
        choices=["auto", "local", "ollama", "dashscope", "hashing"],
        help="临时覆盖向量化提供方（等价于设置 EMBEDDING_PROVIDER）",
    )
    parser.add_argument("--chunk-size", type=int, default=None)
    parser.add_argument("--chunk-overlap", type=int, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    settings.ensure_dirs()
    if args.provider:
        settings.embedding_provider = args.provider

    paths = list(iter_raw_documents(args.raw_dir or default_raw_dir()))
    if not paths:
        print(f"未找到待入库文档（目录：{args.raw_dir or default_raw_dir()}）")
        return 1

    started = time.perf_counter()

    async def run() -> int:
        reset_store()
        report = await ingest_all(
            paths,
            rebuild=not args.keep,
            chunk_size=args.chunk_size,
            chunk_overlap=args.chunk_overlap,
        )
        store = await get_store()
        print_report(report, await store.stats())
        manifest = store.manifest()
        print(f"索引清单：{store.root / 'manifest.json'}")
        print(
            f"  provider={manifest.get('provider')} dim={manifest.get('dim')} "
            f"chunks={manifest.get('n_chunks')} updated_at={manifest.get('updated_at')}"
        )
        return 0

    code = asyncio.run(run())
    elapsed = time.perf_counter() - started
    verdict = "达标" if elapsed <= REBUILD_BUDGET_S else "超时（需排查）"
    print(f"重建总耗时 {elapsed:.2f}s（验收：≤ {REBUILD_BUDGET_S:.0f}s）→ {verdict}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
