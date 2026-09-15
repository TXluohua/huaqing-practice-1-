#!/usr/bin/env python
"""一键入库：解析 → 抽图 → 清洗 → 切片 → 向量化 → 写入向量库。

用法（项目约定：所有命令走 .venv/bin/python）
    .venv/bin/python scripts/ingest.py                  # 入库 data/raw 下全部文档
    .venv/bin/python scripts/ingest.py --rebuild        # 先清空索引再入库
    .venv/bin/python scripts/ingest.py data/raw/a.md    # 只入库指定文件
    .venv/bin/python scripts/ingest.py --pdf x.pdf --title 手册 --version V1.0 --category 设备维护

输出：每个文档的页数 / 切片数 / 元数据完整率，以及索引落点与耗时。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.rag.ingest import (  # noqa: E402
    Chunk,
    default_raw_dir,
    ingest_path,
    iter_raw_documents,
    validate_metadata,
)
from backend.rag.store import get_store, reset_store  # noqa: E402
from backend.setting import get_settings  # noqa: E402


@dataclass
class IngestReport:
    """一次入库的汇总（脚本与 kb_service 共用同一份统计口径）。"""

    n_documents: int = 0
    n_chunks: int = 0
    n_images: int = 0
    completeness: float = 0.0
    documents: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0


async def ingest_all(
    paths: list[Path],
    *,
    rebuild: bool = False,
    pdf_meta: dict[str, str] | None = None,
    chunk_size: int | None = None,
    chunk_overlap: int | None = None,
    require_complete_metadata: bool = True,
) -> IngestReport:
    """把指定文档入库；rebuild=True 时先清空索引（scripts/build_index.py 复用）。"""

    started = time.perf_counter()
    store = await get_store()
    if rebuild:
        await store.reset()

    report = IngestReport()
    all_chunks: list[Chunk] = []
    for path in paths:
        overrides = {}
        if path.suffix.lower() == ".pdf" and pdf_meta:
            overrides = {k: v for k, v in pdf_meta.items() if v}
        parsed, chunks = ingest_path(
            path,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            require_complete_metadata=require_complete_metadata,
            **overrides,
        )
        written = await store.add_chunks(chunks)
        all_chunks.extend(chunks)
        report.n_documents += 1
        report.n_chunks += written
        report.n_images += len(parsed.images)
        report.warnings.extend(parsed.warnings)
        ratio, missing = validate_metadata(chunks)
        report.documents.append(
            {
                "path": str(path),
                "doc_title": parsed.meta.doc_title,
                "version": parsed.meta.version,
                "category": parsed.meta.category,
                "device_model": parsed.meta.device_model,
                "pages": parsed.n_pages,
                "chunks": len(chunks),
                "images": len(parsed.images),
                "completeness": round(ratio, 4),
                "missing": missing,
            }
        )
    ratio, _ = validate_metadata(all_chunks)
    report.completeness = round(ratio, 4)
    report.elapsed_s = round(time.perf_counter() - started, 2)
    return report


def print_report(report: IngestReport, stats: dict) -> None:
    """人类可读的入库报告。"""

    print("=" * 78)
    print("入库报告")
    print("=" * 78)
    for doc in report.documents:
        print(
            f"  {doc['doc_title']:<22} {doc['version']:<6} {doc['category']:<6} "
            f"{doc['device_model']:<10} {doc['pages']:>2}页 {doc['chunks']:>3}片 "
            f"完整率 {doc['completeness']:.0%}"
        )
        if doc["missing"]:
            print(f"      ⚠ 元数据缺失：{', '.join(doc['missing'])}")
    print("-" * 78)
    print(f"  文档 {report.n_documents} 个 / 切片 {report.n_chunks} 个 / 抽图 {report.n_images} 张")
    print(f"  元数据完整率（page + version）：{report.completeness:.1%}")
    print(f"  向量库：backend={stats['backend']} provider={stats['provider']} 维度={stats['dim']}")
    print(f"  索引总量：{stats['n_chunks']} 片 / 覆盖 {stats['n_documents']} 个文档版本")
    print(
        f"  平均切片 {stats['avg_chars']} 字 / {stats['avg_tokens']} token，"
        f"最大 {stats['max_tokens']} token"
    )
    print(f"  耗时 {report.elapsed_s}s（验收标准：建索引 ≤ 5 分钟）")
    if report.warnings:
        print("-" * 78)
        print("  告警：")
        for warning in dict.fromkeys(report.warnings):
            print(f"    - {warning}")
    print("=" * 78)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="知识库一键入库（解析→切片→向量化）")
    parser.add_argument("paths", nargs="*", type=Path, help="待入库文件；缺省为 data/raw 下全部")
    parser.add_argument("--rebuild", action="store_true", help="先清空索引再入库")
    parser.add_argument("--raw-dir", type=Path, default=None, help="语料目录（缺省 data/raw）")
    parser.add_argument("--pdf", type=Path, default=None, help="PDF 文件（需配 --title/--version）")
    parser.add_argument("--title", default=None, help="PDF 文档标题")
    parser.add_argument("--version", default=None, help="PDF 文档版本（必填，元数据完整性要求）")
    parser.add_argument("--category", default=None, help="PDF 文档分类：设备维护 / 工艺 / 标准")
    parser.add_argument("--device-model", default=None, help="PDF 设备型号")
    parser.add_argument("--chunk-size", type=int, default=None, help="切片字符数（缺省取 setting）")
    parser.add_argument("--chunk-overlap", type=int, default=None, help="切片重叠字符数")
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="允许元数据不完整（仅排障用；默认不完整即失败）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths: list[Path] = list(args.paths)
    if args.pdf:
        paths.append(args.pdf)
    if not paths:
        paths = list(iter_raw_documents(args.raw_dir or default_raw_dir()))
    if not paths:
        print(f"未找到可入库的文档（目录：{args.raw_dir or default_raw_dir()}）")
        return 1
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        print(f"文件不存在：{', '.join(missing)}")
        return 1

    settings = get_settings()
    settings.ensure_dirs()
    pdf_meta = {
        "doc_title": args.title,
        "version": args.version,
        "category": args.category,
        "device_model": args.device_model,
    }

    async def run() -> int:
        reset_store()  # 避免复用上一次进程内残留的索引句柄
        report = await ingest_all(
            paths,
            rebuild=args.rebuild,
            pdf_meta=pdf_meta,
            chunk_size=args.chunk_size,
            chunk_overlap=args.chunk_overlap,
            require_complete_metadata=not args.allow_incomplete,
        )
        store = await get_store()
        print_report(report, await store.stats())
        return 0

    try:
        return asyncio.run(run())
    except ValueError as exc:
        print(f"入库失败：{exc}")
        return 2
    except Exception as exc:  # noqa: BLE001 - 脚本入口给出清晰错误而不是堆栈
        print(f"入库异常：{type(exc).__name__}: {exc}")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
