"""切片与元数据测试（开发文档 8.2 单元测试：切片元数据完整性、清洗规则）。

运行：
    .venv/bin/python -m pytest tests/test_rag/test_ingest.py -q
    .venv/bin/python tests/test_rag/test_ingest.py     # 无 pytest 时的兜底入口
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.rag.ingest import (  # noqa: E402
    chunk_document,
    clean_text,
    default_raw_dir,
    estimate_tokens,
    ingest_path,
    iter_raw_documents,
    parse_markdown,
    split_sentences,
    strip_repeated_lines,
    validate_metadata,
)

SAMPLE = """---
doc_title: 测试手册
version: V9.9
category: 设备维护
device_model: Etcher-A
---

<!-- page: 1 -->

## 3.4 真空系统异常排查

第一页正文第一句。第一页正文第二句。

<!-- page: 3 -->

## 3.5 真空泵维护

第三页正文，讲干泵与罗茨泵的维护要点。
"""


def _write(tmp: Path, name: str, text: str) -> Path:
    path = tmp / name
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# 解析与切片
# --------------------------------------------------------------------------- #
def test_parse_markdown_keeps_page_and_section() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        parsed = parse_markdown(_write(Path(tmp), "a.md", SAMPLE))
        assert parsed.meta.doc_title == "测试手册"
        assert parsed.meta.version == "V9.9"
        assert parsed.meta.category == "设备维护"
        assert parsed.meta.device_model == "Etcher-A"
        assert parsed.n_pages == 3, "页码取最大值：3.5 节位于第 3 页"
        assert [b.section for b in parsed.blocks] == ["3.4", "3.5"]
        assert [b.page for b in parsed.blocks] == [1, 3]


def test_chunks_have_complete_metadata() -> None:
    """D1 验收标准：切片元数据完整率 100%（page + version 齐全）。"""

    with tempfile.TemporaryDirectory() as tmp:
        _parsed, chunks = ingest_path(_write(Path(tmp), "a.md", SAMPLE))
        ratio, missing = validate_metadata(chunks)
        assert ratio == 1.0, f"元数据不完整：{missing}"
        assert all(chunk.page > 0 and chunk.version for chunk in chunks)
        assert all(chunk.chunk_id.startswith("c_") for chunk in chunks)


def test_chunk_id_is_stable() -> None:
    """同一文档重复入库必须产生相同 chunk_id（幂等，避免重复切片）。"""

    with tempfile.TemporaryDirectory() as tmp:
        path = _write(Path(tmp), "a.md", SAMPLE)
        assert [c.chunk_id for c in ingest_path(path)[1]] == [
            c.chunk_id for c in ingest_path(path)[1]
        ]


def test_missing_version_is_rejected() -> None:
    """缺 version 时不得入库（引用无法标注版本 = 数据缺陷）。"""

    with tempfile.TemporaryDirectory() as tmp:
        path = _write(Path(tmp), "b.md", "<!-- page: 1 -->\n\n## 1.1 标题\n\n正文内容足够长。\n")
        try:
            ingest_path(path)
        except ValueError as exc:
            assert "元数据完整率" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("缺少 version 时应当拒绝入库")
        # 显式允许时才放行（排障用），完整率仍应低于 1
        _parsed, chunks = ingest_path(path, require_complete_metadata=False)
        assert validate_metadata(chunks)[0] < 1.0


def test_unsupported_suffix_raises() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        try:
            ingest_path(_write(Path(tmp), "c.docx", "x"))
        except ValueError as exc:
            assert "不支持" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("docx 未接入时应明确报错，而不是静默返回空切片")


def test_chunk_size_and_overlap_respected() -> None:
    long_text = "段落内容。" * 200
    body = (
        "---\ndoc_title: 长文\nversion: V1.0\ncategory: 工艺\ndevice_model: 通用\n---\n\n"
        f"<!-- page: 1 -->\n\n## 2.1 长章节\n\n{long_text}\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        parsed = parse_markdown(_write(Path(tmp), "d.md", body))
        chunks = chunk_document(parsed, chunk_size=300, chunk_overlap=50)
        assert len(chunks) > 1, "长章节必须被切成多块"
        assert all(c.n_chars <= 420 for c in chunks), "单块不应显著超过 chunk_size"
        assert all(c.section == "2.1" for c in chunks), "切片不得跨章节"


# --------------------------------------------------------------------------- #
# 清洗
# --------------------------------------------------------------------------- #
def test_clean_text_removes_page_numbers_and_joins_hyphens() -> None:
    cleaned = clean_text("第一行  \n\n\n  12  \n第二 line-\nbreak 结束")
    assert "\n\n\n" not in cleaned
    assert "12" not in cleaned.splitlines(), "纯页码行应被删除"
    assert "linebreak" in cleaned, "连字符断行应合并"


def test_strip_repeated_header_lines() -> None:
    """跨页重复的页眉页脚必须被识别并删除（否则污染检索与引用）。"""

    pages = [["公司内部资料", f"第 {i} 页内容", "页脚"] for i in range(1, 6)]
    result = strip_repeated_lines(pages)
    assert all("公司内部资料" not in line for page in result for line in page)
    assert any("第 3 页内容" in line for page in result for line in page)


def test_split_sentences_and_token_estimate() -> None:
    assert len(split_sentences("第一句。第二句；第三句？")) == 3
    assert estimate_tokens("真空度") >= 3
    assert estimate_tokens("") == 0


def test_repository_corpus_is_fully_metadata_complete() -> None:
    """仓库自带语料必须全部可入库（避免到 CI/演示时才暴露格式问题）。"""

    paths = list(iter_raw_documents(default_raw_dir()))
    if not paths:  # pragma: no cover - 语料未就绪时跳过
        print("（data/raw 为空，跳过语料校验）")
        return
    for path in paths:
        _parsed, chunks = ingest_path(path)
        ratio, missing = validate_metadata(chunks)
        assert ratio == 1.0, f"{path.name} 元数据不完整：{missing}"


if __name__ == "__main__":  # pragma: no cover - 无 pytest 时的兜底运行入口
    import traceback

    cases = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failures = 0
    for name, case in cases:
        try:
            case()
        except Exception:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}")
            traceback.print_exc()
        else:
            print(f"PASS {name}")
    print(f"\n{len(cases) - failures}/{len(cases)} passed")
    raise SystemExit(1 if failures else 0)
