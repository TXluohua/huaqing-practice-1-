"""文档解析 + 抽图 + 清洗 + 切片（开发文档 6.2 / 4.3）。

职责边界
--------
本模块只负责把「原始文档」变成「带完整元数据的切片」，
不做向量化（store.py）、不做检索（retriever.py）。

元数据完整率 100%（D1 验收标准）
--------------------------------
每个切片必须带 `page` 与 `version`：
- `version` 来自 front-matter（md）或调用方传入（pdf），缺失即失败；
- `page` 来自 `<!-- page: N -->` 标记（md）或物理页码（pdf），缺失时回退到
  「最近一次已知页码」，若始终未知则记为 0 并由 validate_metadata() 判为不完整。

支持的输入
----------
- `.md` / `.markdown` / `.txt`：YAML front-matter + `<!-- page: N -->` 页码标记
  + `## 3.4 标题` 章节标记（见 data/raw 下的示例语料）；
- `.pdf`：按物理页解析（pypdf），标题从行首编号推断，页码即物理页码；
  文档级元数据（标题/版本/分类/型号）由调用方传入 —— 与接口文档 §4.7 的
  上传参数（doc_title / version / category）一致。

未接入：`.docx` / `.pptx` / `.xlsx`（开发文档 5.3 的 MarkItDown 路线），
调用即抛出明确的不支持错误，而不是静默返回空切片。
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from ..setting import PROJECT_ROOT, get_settings

logger = logging.getLogger(__name__)

#: 支持的文档后缀（含大小写不敏感处理）
SUPPORTED_SUFFIXES: frozenset[str] = frozenset({".md", ".markdown", ".txt", ".pdf"})

_PAGE_MARK_RE = re.compile(r"<!--\s*page\s*:\s*(\d+)\s*-->")
_HEADING_RE = re.compile(r"^(#{1,6})\s*(?:(\d+(?:\.\d+)*)\s*)?(.*?)\s*$")
#: PDF 中形如「3.4 真空系统异常排查」或「第 3 章 真空系统」的行，视为标题
_PDF_HEADING_RE = re.compile(r"^(?:第\s*\d+\s*章|\d+(?:\.\d+){0,3}\s+\S.{0,40})$")
_IMAGE_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<src>[^)\s]+)")
_PAGE_NUMBER_ONLY_RE = re.compile(r"^[\s\-—–]*\d{1,4}[\s\-—–]*$")
_MULTI_BLANK_RE = re.compile(r"\n{3,}")
_MULTI_SPACE_RE = re.compile(r"[ \t\u3000]{2,}")
_HYPHEN_BREAK_RE = re.compile(r"([A-Za-z])-\n([a-z])")
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: 结论句切分（citation 的覆盖率按句统计，与 verify 共用一份定义）
SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?；;])\s*")


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class DocMeta:
    """文档级元数据。`version` 与 `category` 会随切片一起入库。"""

    doc_title: str
    version: str
    category: str
    device_model: str
    source_path: str
    doc_id: str = ""

    def __post_init__(self) -> None:
        if not self.doc_id:
            self.doc_id = make_doc_id(self.doc_title, self.version, self.source_path)


@dataclass(slots=True)
class TextBlock:
    """解析后的中间块：一段同页同章节的正文。"""

    text: str
    page: int
    section: str = ""
    heading: str = ""


@dataclass(slots=True)
class ImageAsset:
    """抽出的图片（开发文档 5.6 的 kb_image）。"""

    rel_path: str
    alt: str = ""
    page: int = 0
    section: str = ""
    extracted_text: str = ""


@dataclass(slots=True)
class ParsedDocument:
    """解析结果：元数据 + 文本块 + 图片 + 告警。"""

    meta: DocMeta
    blocks: list[TextBlock] = field(default_factory=list)
    images: list[ImageAsset] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    n_pages: int = 0

    @property
    def text(self) -> str:
        return "\n\n".join(b.text for b in self.blocks)


@dataclass(slots=True)
class Chunk:
    """切片（入库与检索的最小单位）。

    `chunk_id` 决定去重与引用溯源；`page` / `version` 是引用卡片上
    「P.118」与「V3.2」的来源，缺失即视为数据缺陷。
    """

    chunk_id: str
    doc_id: str
    doc_title: str
    version: str
    category: str
    device_model: str
    page: int
    section: str
    heading: str
    index: int
    text: str
    n_chars: int
    n_tokens: int
    source_path: str

    @property
    def search_text(self) -> str:
        """供 BM25 使用的检索文本：章节号 + 标题 + 正文。

        把章节号并进去是刻意的 —— 用户会直接问「5.2 节写了什么」，
        而 `5.2` 这类标记在正文里通常并不出现。
        """

        prefix = " ".join(part for part in (self.section, self.heading) if part)
        return f"{prefix} {self.text}".strip()

    def to_payload(self) -> dict[str, Any]:
        """转成可 JSON 序列化的入库记录（store.py 与清单文件共用）。"""

        return {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "doc_title": self.doc_title,
            "version": self.version,
            "category": self.category,
            "device_model": self.device_model,
            "page": self.page,
            "section": self.section,
            "heading": self.heading,
            "index": self.index,
            "text": self.text,
            "n_chars": self.n_chars,
            "n_tokens": self.n_tokens,
            "source_path": self.source_path,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Chunk":
        return cls(**payload)


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #


def make_doc_id(doc_title: str, version: str, source_path: str = "") -> str:
    """文档 ID：标题 + 版本 + 路径的稳定哈希（同一文档重传视为同一 doc_id）。"""

    raw = f"{doc_title}|{version}|{source_path}".encode("utf-8")
    return "doc_" + hashlib.sha1(raw).hexdigest()[:12]


def make_chunk_id(doc_id: str, page: int, section: str, index: int, text: str) -> str:
    """切片 ID：内容相关且稳定（重跑入库不会产生重复切片）。"""

    raw = f"{doc_id}|{page}|{section}|{index}|{text[:80]}".encode("utf-8")
    return "c_" + hashlib.sha1(raw).hexdigest()[:12]


def estimate_tokens(text: str) -> int:
    """token 数估算（上下文预算用）。

    中文按 1 字 ≈ 1 token、英文按 1 词 ≈ 1.3 token 估算。
    偏保守（宁可少放），因为真实分词器对中文常见字多为 1 token 左右。
    """

    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    ascii_words = len(_ASCII_WORD_RE.findall(text))
    return int(cjk + ascii_words * 1.3) + 1


def clean_text(text: str) -> str:
    """清洗：控制字符、连字符断行、多余空白、纯页码行。"""

    text = _CONTROL_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _HYPHEN_BREAK_RE.sub(r"\1\2", text)
    lines = []
    for line in text.split("\n"):
        line = line.rstrip()
        if _PAGE_NUMBER_ONLY_RE.match(line):
            continue
        line = _MULTI_SPACE_RE.sub(" ", line)
        lines.append(line)
    text = "\n".join(lines)
    text = _MULTI_BLANK_RE.sub("\n\n", text)
    return text.strip()


def strip_repeated_lines(
    pages: Sequence[Sequence[str]], threshold: float = 0.5
) -> list[list[str]]:
    """删除跨页重复的页眉/页脚（出现页数占比 >= threshold 的行）。"""

    if len(pages) < 2:
        return [list(p) for p in pages]
    counts: dict[str, int] = {}
    for page_lines in pages:
        for line in {ln.strip() for ln in page_lines if ln.strip()}:
            counts[line] = counts.get(line, 0) + 1
    repeated = {
        ln for ln, c in counts.items() if c / len(pages) >= threshold and len(ln) <= 60
    }
    if repeated:
        logger.debug("清洗：删除 %d 条跨页重复行", len(repeated))
    return [[ln for ln in page_lines if ln.strip() not in repeated] for page_lines in pages]


def split_sentences(text: str) -> list[str]:
    """按中文/英文句末标点切句（verify 的引用覆盖率按句统计）。"""

    return [p.strip() for p in SENTENCE_SPLIT_RE.split(text) if p and p.strip()]


def _read_text(path: Path) -> str:
    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="ignore")


def _load_front_matter(raw: str) -> tuple[dict[str, Any], str]:
    """解析开头 `---` 包裹的 front-matter，返回 (元数据, 剩余正文)。

    优先用 PyYAML；不可用时退化为 `key: value` 的极简解析（本项目语料只用到
    四个标量字段，够用且不引入硬依赖）。
    """

    text = raw.lstrip("\ufeff")
    if not text.startswith("---"):
        return {}, raw
    end = text.find("\n---", 3)
    if end == -1:
        return {}, raw
    block = text[3:end].strip("\n")
    body = text[end + 4 :].lstrip("\n")
    try:
        import yaml

        loaded = yaml.safe_load(block) or {}
        if isinstance(loaded, dict):
            return {str(k): v for k, v in loaded.items()}, body
    except Exception:  # noqa: BLE001 - 退化解析，不因缺依赖而失败
        logger.debug("PyYAML 不可用，使用退化 front-matter 解析")
    meta: dict[str, Any] = {}
    for line in block.split("\n"):
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip().strip("'\"")
    return meta, body


# --------------------------------------------------------------------------- #
# 解析：Markdown / 纯文本
# --------------------------------------------------------------------------- #


def parse_markdown(path: Path, meta_overrides: dict[str, Any] | None = None) -> ParsedDocument:
    """解析 md/txt：front-matter + 页码标记 + 章节标题。"""

    raw = _read_text(path)
    front, body = _load_front_matter(raw)
    overrides = {k: v for k, v in (meta_overrides or {}).items() if v not in (None, "")}
    merged = {**front, **overrides}

    warnings: list[str] = []
    version = str(merged.get("version") or "")
    category = str(merged.get("category") or "")
    if not version:
        warnings.append(f"{path.name}：缺少 version，引用将无法标注文档版本")
    if not category:
        warnings.append(f"{path.name}：缺少 category，元数据过滤会失效")
    meta = DocMeta(
        doc_title=str(merged.get("doc_title") or path.stem),
        version=version,
        category=category,
        device_model=str(merged.get("device_model") or ""),
        source_path=str(path),
    )

    # ---- 逐行扫描：记录每行所属页码 / 章节，并收集图片引用 ----
    pages: list[list[str]] = [[]]
    page_of_line: list[int] = []
    section_of_line: list[str] = []
    heading_of_line: list[str] = []
    page_no, section, heading = 0, "", ""
    has_page_mark = False
    images: list[ImageAsset] = []

    for line in body.split("\n"):
        mark = _PAGE_MARK_RE.search(line)
        if mark and line.strip().startswith("<!--"):
            page_no = int(mark.group(1))
            has_page_mark = True
            pages.append([])
            continue
        if line.lstrip().startswith("#"):
            head = _HEADING_RE.match(line)
            if head and head.group(1) in ("##", "###", "####"):
                section = (head.group(2) or section).strip() or section
                heading = (head.group(3) or "").strip() or heading
                continue
        img = _IMAGE_RE.search(line)
        if img:
            images.append(
                ImageAsset(
                    rel_path=img.group("src"), alt=img.group("alt"), page=page_no, section=section
                )
            )
        pages[-1].append(line)
        page_of_line.append(page_no)
        section_of_line.append(section)
        heading_of_line.append(heading)

    if not has_page_mark:
        warnings.append(
            f"{path.name}：没有 `<!-- page: N -->` 页码标记，切片 page 只能取 0（引用落不到页码）"
        )

    # ---- 删除跨页重复行（页眉页脚），再按「同页同章节」聚合文本块 ----
    pages = strip_repeated_lines(pages)
    blocks: list[TextBlock] = []
    cursor = 0
    for page_lines in pages:
        buffer: list[str] = []
        block_page = page_of_line[cursor] if cursor < len(page_of_line) else 0
        block_section = section_of_line[cursor] if cursor < len(section_of_line) else ""
        block_heading = heading_of_line[cursor] if cursor < len(heading_of_line) else ""

        for line in page_lines:
            if cursor < len(page_of_line):
                line_page = page_of_line[cursor]
                line_section = section_of_line[cursor]
                line_heading = heading_of_line[cursor]
            else:  # pragma: no cover - 页码标记与行数不一致时的兜底
                line_page, line_section, line_heading = block_page, block_section, block_heading
            if (line_page, line_section) != (block_page, block_section):
                text = clean_text("\n".join(buffer))
                if text:
                    blocks.append(
                        TextBlock(
                            text=text,
                            page=block_page,
                            section=block_section,
                            heading=block_heading,
                        )
                    )
                buffer.clear()
                block_page, block_section, block_heading = line_page, line_section, line_heading
            buffer.append(line)
            cursor += 1
        text = clean_text("\n".join(buffer))
        if text:
            blocks.append(
                TextBlock(text=text, page=block_page, section=block_section, heading=block_heading)
            )

    n_pages = max((b.page for b in blocks), default=0)
    return ParsedDocument(
        meta=meta, blocks=blocks, images=images, warnings=warnings, n_pages=n_pages
    )


# --------------------------------------------------------------------------- #
# 解析：PDF
# --------------------------------------------------------------------------- #


def parse_pdf(path: Path, meta_overrides: dict[str, Any] | None = None) -> ParsedDocument:
    """解析 PDF：物理页 → 文本块，行首编号 → 章节；顺带抽出内嵌图片。

    文档级元数据（doc_title / version / category / device_model）必须由调用方传入 ——
    PDF 正文里没有可靠的版本字段，而「版本」是引用可信度的关键（P5 痛点）。
    """

    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - 依赖缺失时给出明确指引
        raise RuntimeError(
            "解析 PDF 需要 pypdf：请执行 `.venv/bin/python -m pip install pypdf`"
        ) from exc

    overrides = {k: v for k, v in (meta_overrides or {}).items() if v not in (None, "")}
    warnings: list[str] = []
    version = str(overrides.get("version") or "")
    if not version:
        warnings.append(f"{path.name}：未提供 version，引用将无法标注文档版本")
    meta = DocMeta(
        doc_title=str(overrides.get("doc_title") or path.stem),
        version=version,
        category=str(overrides.get("category") or ""),
        device_model=str(overrides.get("device_model") or ""),
        source_path=str(path),
    )

    reader = PdfReader(str(path))
    raw_pages: list[list[str]] = []
    for page in reader.pages:
        text = page.extract_text() or ""
        raw_pages.append(text.replace("\r\n", "\n").split("\n"))
    if not any(any(ln.strip() for ln in p) for p in raw_pages):
        warnings.append(f"{path.name}：未提取到文本层（可能是扫描件，见风险 R1）")

    cleaned_pages = strip_repeated_lines(raw_pages)

    blocks: list[TextBlock] = []
    images: list[ImageAsset] = []
    section, heading = "", ""
    for page_index, lines in enumerate(cleaned_pages, start=1):
        buffer: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped and _PDF_HEADING_RE.match(stripped) and len(stripped) <= 60:
                text = clean_text("\n".join(buffer))
                if text:
                    blocks.append(
                        TextBlock(text=text, page=page_index, section=section, heading=heading)
                    )
                buffer.clear()
                head = stripped.split(" ", 1)[0]
                section = head if re.match(r"^\d+(?:\.\d+)*$", head) else (head or section)
                heading = stripped[len(head) :].strip() or heading
                continue
            buffer.append(line)
        text = clean_text("\n".join(buffer))
        if text:
            blocks.append(TextBlock(text=text, page=page_index, section=section, heading=heading))

        # ---- 抽图（best-effort：失败只记告警，不影响文本入库）----
        try:
            for img_index, image in enumerate(reader.pages[page_index - 1].images):
                name = f"{meta.doc_id}_p{page_index}_{img_index}_{Path(image.name).name}"
                target = get_settings().image_dir / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(image.data)
                images.append(
                    ImageAsset(rel_path=name, alt=image.name, page=page_index, section=section)
                )
        except Exception as exc:  # noqa: BLE001 - 抽图失败不得中断入库
            warnings.append(f"{path.name} P.{page_index}：抽图失败（{exc}）")

    return ParsedDocument(
        meta=meta, blocks=blocks, images=images, warnings=warnings, n_pages=len(cleaned_pages)
    )


def parse_document(
    path: str | Path,
    *,
    doc_title: str | None = None,
    version: str | None = None,
    category: str | None = None,
    device_model: str | None = None,
) -> ParsedDocument:
    """按后缀分派解析器。显式传入的元数据优先于文档内自带值。"""

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"文档不存在：{path}")
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValueError(
            f"不支持的文档类型 {suffix!r}（当前支持：{', '.join(sorted(SUPPORTED_SUFFIXES))}）；"
            "docx/pptx/xlsx 需先接入 MarkItDown（开发文档 5.3）"
        )
    overrides = {
        "doc_title": doc_title,
        "version": version,
        "category": category,
        "device_model": device_model,
    }
    if suffix == ".pdf":
        return parse_pdf(path, overrides)
    return parse_markdown(path, overrides)


# --------------------------------------------------------------------------- #
# 切片
# --------------------------------------------------------------------------- #


def _split_paragraphs(text: str) -> list[str]:
    """先按空行切段；超长段落再按行聚合，避免单块远超 chunk_size。"""

    out: list[str] = []
    for para in (p.strip() for p in re.split(r"\n\s*\n", text)):
        if not para:
            continue
        if len(para) <= 1200:
            out.append(para)
            continue
        line_buffer: list[str] = []
        for line in para.split("\n"):
            line_buffer.append(line)
            if sum(len(x) for x in line_buffer) >= 600:
                out.append("\n".join(line_buffer))
                line_buffer = []
        if line_buffer:
            out.append("\n".join(line_buffer))
    return out


def _window_long_paragraph(para: str, chunk_size: int) -> list[str]:
    """把超长段落切成 <= chunk_size 的窗口，优先在句末标点处断开。

    必要性：单个段落本身就可能超过 chunk_size（长表格、连续步骤）。
    若不在段落内部再切，一个切片可以远超 chunk_size —— 既撑爆上下文预算，
    也会让引用粒度粗糙到无法定位。
    """

    if len(para) <= chunk_size:
        return [para]
    sentences = [s for s in re.split(r"(?<=[。！？!?；;])", para) if s]
    if len(sentences) <= 1:  # 没有任何句末标点：退化为硬切
        return [para[i : i + chunk_size] for i in range(0, len(para), chunk_size)]
    windows: list[str] = []
    buffer: list[str] = []
    size = 0
    for sentence in sentences:
        if size + len(sentence) > chunk_size and buffer:
            windows.append("".join(buffer))
            buffer, size = [], 0
        if len(sentence) > chunk_size:  # 单句仍超长：硬切
            if buffer:
                windows.append("".join(buffer))
                buffer, size = [], 0
            windows.extend(
                sentence[i : i + chunk_size] for i in range(0, len(sentence), chunk_size)
            )
            continue
        buffer.append(sentence)
        size += len(sentence)
    if buffer:
        windows.append("".join(buffer))
    return windows


def chunk_document(
    parsed: ParsedDocument,
    *,
    chunk_size: int | None = None,
    chunk_overlap: int | None = None,
) -> list[Chunk]:
    """把解析结果切成入库切片。

    策略：先按「同页同章节」分块切段落，再贪心累积到 chunk_size；
    重叠取上一块尾部 chunk_overlap 个字符，且**只在段落边界重叠**，不切断句子。
    """

    settings = get_settings()
    chunk_size = chunk_size or settings.chunk_size
    chunk_overlap = chunk_overlap or settings.chunk_overlap
    if chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap 必须小于 chunk_size")

    chunks: list[Chunk] = []
    index = 0
    for block in parsed.blocks:
        paragraphs: list[str] = []
        for para in _split_paragraphs(block.text):
            paragraphs.extend(_window_long_paragraph(para, chunk_size))
        buffer: list[str] = []
        size = 0

        def emit() -> None:
            nonlocal index, buffer, size
            text = "\n\n".join(buffer).strip()
            if not text:
                buffer, size = [], 0
                return
            chunks.append(
                Chunk(
                    chunk_id=make_chunk_id(
                        parsed.meta.doc_id, block.page, block.section, index, text
                    ),
                    doc_id=parsed.meta.doc_id,
                    doc_title=parsed.meta.doc_title,
                    version=parsed.meta.version,
                    category=parsed.meta.category,
                    device_model=parsed.meta.device_model,
                    page=block.page,
                    section=block.section,
                    heading=block.heading,
                    index=index,
                    text=text,
                    n_chars=len(text),
                    n_tokens=estimate_tokens(text),
                    source_path=parsed.meta.source_path,
                )
            )
            index += 1
            if chunk_overlap <= 0:
                buffer, size = [], 0
                return
            tail = text[-chunk_overlap:]
            cut = tail.find("\n\n")
            tail = tail[cut + 2 :] if cut != -1 else tail
            buffer = [tail] if tail.strip() else []
            size = len(tail)

        for para in paragraphs:
            if size + len(para) > chunk_size and buffer:
                emit()
            buffer.append(para)
            size += len(para) + 2
        emit()

    return chunks


# --------------------------------------------------------------------------- #
# 校验与入口
# --------------------------------------------------------------------------- #


def validate_metadata(chunks: Sequence[Chunk]) -> tuple[float, list[str]]:
    """返回 (元数据完整率, 缺失说明)。完整 = page > 0 且 version 非空。"""

    if not chunks:
        return 0.0, ["没有切片"]
    ok = 0
    missing: list[str] = []
    for chunk in chunks:
        problems = []
        if not chunk.page or chunk.page <= 0:
            problems.append("page")
        if not chunk.version.strip():
            problems.append("version")
        if problems:
            if len(missing) < 5:
                missing.append(f"{chunk.chunk_id}({','.join(problems)})")
        else:
            ok += 1
    return ok / len(chunks), missing


def ingest_path(
    path: str | Path,
    *,
    doc_title: str | None = None,
    version: str | None = None,
    category: str | None = None,
    device_model: str | None = None,
    chunk_size: int | None = None,
    chunk_overlap: int | None = None,
    require_complete_metadata: bool = True,
) -> tuple[ParsedDocument, list[Chunk]]:
    """解析 + 切片一体入口（scripts/ingest.py 与 kb_service 共用）。

    `require_complete_metadata=True` 时，元数据完整率不足 100% 直接抛错 ——
    D1 验收标准要求 page + version 齐全，宁可入库失败也不要产生无法引用的切片。
    """

    parsed = parse_document(
        path,
        doc_title=doc_title,
        version=version,
        category=category,
        device_model=device_model,
    )
    chunks = chunk_document(parsed, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    ratio, missing = validate_metadata(chunks)
    if ratio < 1.0:
        parsed.warnings.append(
            f"元数据完整率 {ratio:.0%}，缺失示例：{', '.join(missing)}"
        )
    if require_complete_metadata and ratio < 1.0:
        raise ValueError(
            f"{Path(path).name} 元数据完整率 {ratio:.0%} < 100%：{', '.join(missing)}；"
            "请补 version（front-matter 或上传参数）与页码标记 `<!-- page: N -->`"
        )
    return parsed, chunks


def default_raw_dir() -> Path:
    """默认语料目录：<项目根>/data/raw（setting.data_dir 指向 backend/data，故用 PROJECT_ROOT）。"""

    return PROJECT_ROOT / "data" / "raw"


def iter_raw_documents(raw_dir: str | Path | None = None) -> Iterator[Path]:
    """遍历 data/raw 下所有受支持的文档（跳过隐藏文件）。"""

    base = Path(raw_dir) if raw_dir else default_raw_dir()
    if not base.exists():
        return
    for path in sorted(base.rglob("*")):
        if (
            path.is_file()
            and path.suffix.lower() in SUPPORTED_SUFFIXES
            and not path.name.startswith(".")
        ):
            yield path


def ingest_paths(
    paths: Iterable[str | Path], **kwargs: Any
) -> tuple[list[Chunk], list[ParsedDocument]]:
    """批量入库（返回全部切片与解析结果，便于脚本汇总）。"""

    all_chunks: list[Chunk] = []
    docs: list[ParsedDocument] = []
    for path in paths:
        parsed, chunks = ingest_path(path, **kwargs)
        docs.append(parsed)
        all_chunks.extend(chunks)
    return all_chunks, docs


__all__ = [
    "SUPPORTED_SUFFIXES",
    "Chunk",
    "DocMeta",
    "ImageAsset",
    "ParsedDocument",
    "TextBlock",
    "chunk_document",
    "clean_text",
    "default_raw_dir",
    "estimate_tokens",
    "ingest_path",
    "ingest_paths",
    "iter_raw_documents",
    "make_chunk_id",
    "make_doc_id",
    "parse_document",
    "parse_markdown",
    "parse_pdf",
    "split_sentences",
    "strip_repeated_lines",
    "validate_metadata",
]
