"""向量化与向量库：**向量库唯一出口**（开发文档 6.1「向量库访问收敛」）。

换向量库只改本文件 —— 上层（retriever / nodes / services）只通过 VectorStore
的接口访问，不直接 import chromadb。

三级降级（离线可用是硬要求）
----------------------------
embedding 提供方按顺序探测，第一个可用者胜出：

    1. local      sentence-transformers + 本地缓存模型（默认 BAAI/bge-large-zh-v1.5）
    2. ollama     http://127.0.0.1:11434（开发文档 5.3 的 bge-m3 路线）
    3. dashscope  阿里云 text-embedding-v3（需 DASHSCOPE_API_KEY）
    4. hashing    内置确定性哈希向量（零依赖兜底，仅保证链路可跑、可测试）

向量库后端同理：

    chroma  持久化目录（开发文档 5.3）
    simple  内置 numpy + JSONL 实现（chromadb 未安装时自动启用）

**建库与查询必须共用同一实例**：索引清单（manifest.json）里记录 provider 名称、
维度与模型签名，任何不匹配都会抛 EmbeddingMismatchError，
而不是静默地拿两种语义空间算余弦相似度（那会把命中率毁掉且很难发现）。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import json
import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol, Sequence, runtime_checkable

import numpy as np

from ..setting import Settings, get_settings
from .ingest import Chunk, estimate_tokens

logger = logging.getLogger(__name__)

MANIFEST_NAME = "manifest.json"
SIMPLE_INDEX_NAME = "chunks.jsonl"
SIMPLE_VECTORS_NAME = "vectors.npy"

_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9_]+")


def configure_hf_env(
    settings: Settings | None = None,
    *,
    offline: bool | None = None,
) -> None:
    """配置 HuggingFace 运行环境：**模型目录落在项目内** + 离线/端点开关。

    必须在任何 HuggingFace 相关 import 之前调用，原因有二：

    1. **可部署**：HF_HOME/HF_HUB_CACHE 指向 `setting.model_dir`
       （默认 `backend/data/models`），模型不写用户家目录，
       Docker 构建或换机时只需同步这一个目录；
    2. **防坑**：huggingface_hub 联网失败时，sentence-transformers 的
       CrossEncoder 会退化成「按 config 新建未训练模型」而不是报错 ——
       那种模型打分毫无区分度却看着像能用（本项目已实测踩到）。
       因此离线标志必须在加载前设好。
    """

    settings = settings or get_settings()
    model_dir = Path(settings.model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_HOME", str(model_dir))
    os.environ.setdefault("HF_HUB_CACHE", str(model_dir / "hub"))
    if settings.hf_endpoint:
        os.environ.setdefault("HF_ENDPOINT", settings.hf_endpoint)
    if offline is None:
        offline = settings.embedding_offline
    if offline:
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
    else:  # 显式允许下载（仅 scripts/prepare_model.py --download）
        os.environ["HF_HUB_OFFLINE"] = "0"
        os.environ["TRANSFORMERS_OFFLINE"] = "0"


def apply_hf_offline_env(offline: bool = True) -> None:
    """兼容旧调用点：等价于 configure_hf_env(offline=offline)。"""

    configure_hf_env(offline=offline)


def resolve_model_ref(name_or_path: str, model_dir: Path | str) -> str:
    """把「模型名或路径」解析成 sentence-transformers 能直接加载的引用。

    解析顺序（本地部署友好，不依赖用户家目录缓存）：
        1. 传进来的本身就是存在的路径 → 原样使用；
        2. `<model_dir>/<模型名末段>` 是一个含权重的目录 → 用该绝对路径
           （例如 ModelScope / 手工拷贝到 backend/data/models/bge-reranker-v2-m3）；
        3. 否则返回原始仓库名，交给 HuggingFace 缓存解析
           （HF_HOME 已被指向 model_dir，因此仍然落在项目内）。

    这样「把模型目录放进项目」和「用 HF 缓存」两种部署方式都能工作，
    配置项 `EMBEDDING_MODEL` / `RERANK_MODEL` 既可以是仓库名，也可以是路径。
    """

    raw = (name_or_path or "").strip()
    if not raw:
        return raw
    candidate = Path(raw).expanduser()
    if candidate.exists():
        return str(candidate if candidate.is_absolute() else candidate.resolve())
    local = Path(model_dir) / Path(raw).name
    if local.is_dir():
        has_weight = any(
            (local / item).exists()
            for item in (
                "model.safetensors",
                "pytorch_model.bin",
                "model.onnx",
                "openvino_model.bin",
            )
        ) or any(local.glob("*.safetensors"))
        if has_weight:
            return str(local.resolve())
    return raw


class EmbeddingUnavailable(RuntimeError):
    """所有 embedding 提供方都不可用（正常环境下 hashing 兜底不会触发）。"""


class EmbeddingMismatchError(RuntimeError):
    """索引与当前 embedding 实例不匹配（provider / 维度 / 签名不同）。"""


# --------------------------------------------------------------------------- #
# embedding 提供方
# --------------------------------------------------------------------------- #


@runtime_checkable
class Embedder(Protocol):
    """embedding 提供方契约：全部异步、返回 L2 归一化后的 float32 矩阵。"""

    name: str
    model: str
    dim: int

    @property
    def signature(self) -> str:  # pragma: no cover - 协议声明
        ...

    async def embed(self, texts: Sequence[str]) -> np.ndarray:  # pragma: no cover
        ...


class HashingEmbedder:
    """确定性哈希向量（零依赖兜底）。

    用「中文字 + 中文二元组 + 英文小写词」做特征，blake2b 稳定哈希到固定维度，
    再做 L2 归一化。它不是语义模型，检索质量明显弱于 bge，
    但**完全离线、完全确定**：保证没有模型、没有网络时链路仍可跑通与测试。
    """

    name = "hashing"
    model = "char-ngram-hashing"

    def __init__(self, dim: int = 512) -> None:
        self.dim = dim

    @property
    def signature(self) -> str:
        return f"hashing:{self.dim}"

    @staticmethod
    def _features(text: str) -> list[str]:
        text = text.lower()
        feats: list[str] = []
        cjk = _CJK_RE.findall(text)
        feats.extend(cjk)
        feats.extend(a + b for a, b in zip(cjk, cjk[1:]))
        feats.extend(_ASCII_WORD_RE.findall(text))
        return feats

    async def embed(self, texts: Sequence[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            counts: dict[str, int] = {}
            for feat in self._features(text):
                counts[feat] = counts.get(feat, 0) + 1
            for feat, freq in counts.items():
                digest = hashlib.blake2b(feat.encode("utf-8"), digest_size=8).digest()
                value = int.from_bytes(digest, "big")
                bucket = value % self.dim
                sign = 1.0 if (value >> 63) & 1 else -1.0
                out[row, bucket] += sign * (1.0 + math.log(freq))
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return (out / norms).astype(np.float32)


class SentenceTransformerEmbedder:
    """本地 sentence-transformers 模型（默认 bge-large-zh-v1.5，1024 维）。

    同步 encode 放到线程池执行，避免阻塞事件循环（开发文档 6.4：全链路异步）。
    `offline=True` 时设置 HF_HUB_OFFLINE，只用本地缓存、不发起下载。
    """

    name = "local"

    def __init__(self, model: str, *, offline: bool = True, model_dir: Path | str | None = None) -> None:
        settings = get_settings()
        self.model = resolve_model_ref(model, model_dir or settings.model_dir)
        self.offline = offline
        self._model: Any = None
        self._dim: int | None = None
        self._lock = asyncio.Lock()

    async def _ensure_model(self) -> Any:
        async with self._lock:
            if self._model is not None:
                return self._model
            apply_hf_offline_env(self.offline)

            def load() -> Any:
                from sentence_transformers import SentenceTransformer

                return SentenceTransformer(self.model)

            self._model = await asyncio.to_thread(load)
            self._dim = int(self._model.get_sentence_embedding_dimension())
            logger.info("embedding 就绪：local/%s（%d 维）", self.model, self._dim)
            return self._model

    @property
    def dim(self) -> int:
        if self._dim is None:
            raise EmbeddingUnavailable("本地模型尚未加载，维度未知")
        return self._dim

    @property
    def signature(self) -> str:
        return f"local:{self.model}"

    async def embed(self, texts: Sequence[str]) -> np.ndarray:
        model = await self._ensure_model()

        def run() -> np.ndarray:
            vectors = model.encode(
                list(texts),
                normalize_embeddings=True,
                batch_size=16,
                show_progress_bar=False,
            )
            return np.asarray(vectors, dtype=np.float32)

        return await asyncio.to_thread(run)


class OllamaEmbedder:
    """Ollama 本地服务（开发文档 5.3 的 bge-m3 路线）。"""

    name = "ollama"

    def __init__(self, base_url: str, model: str, *, timeout_s: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self._dim: int | None = None
        self._lock = asyncio.Lock()

    @property
    def dim(self) -> int:
        if self._dim is None:
            raise EmbeddingUnavailable("Ollama 维度未知（尚未探测）")
        return self._dim

    @property
    def signature(self) -> str:
        return f"ollama:{self.model}"

    async def ping(self) -> bool:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"{self.base_url}/api/tags")
                return resp.status_code == 200
        except Exception:  # noqa: BLE001 - 未启动即视为不可用
            return False

    async def embed(self, texts: Sequence[str]) -> np.ndarray:
        import httpx

        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            resp = await client.post(
                f"{self.base_url}/api/embed",
                json={"model": self.model, "input": list(texts)},
            )
            resp.raise_for_status()
            payload = resp.json()
        vectors = np.asarray(payload["embeddings"], dtype=np.float32)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        vectors = vectors / norms
        async with self._lock:
            self._dim = int(vectors.shape[1])
        return vectors.astype(np.float32)


class DashScopeEmbedder:
    """阿里云 DashScope 向量化接口（需 DASHSCOPE_API_KEY）。"""

    name = "dashscope"
    _URL = (
        "https://dashscope.aliyuncs.com/api/v1/services/embeddings/"
        "text-embedding/text-embedding"
    )

    def __init__(self, api_key: str, model: str, *, timeout_s: float = 20.0) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s
        self._dim: int | None = None

    @property
    def dim(self) -> int:
        if self._dim is None:
            raise EmbeddingUnavailable("DashScope 维度未知（尚未调用）")
        return self._dim

    @property
    def signature(self) -> str:
        return f"dashscope:{self.model}"

    async def embed(self, texts: Sequence[str]) -> np.ndarray:
        import httpx

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = {"model": self.model, "input": {"texts": list(texts)}}
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            resp = await client.post(self._URL, headers=headers, json=body)
            resp.raise_for_status()
            payload = resp.json()
        items = payload["output"]["embeddings"]
        vectors = np.asarray([item["embedding"] for item in items], dtype=np.float32)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        vectors = vectors / norms
        self._dim = int(vectors.shape[1])
        return vectors.astype(np.float32)


async def create_embedder(settings: Settings | None = None) -> Embedder:
    """按配置创建 embedding 提供方。

    **默认 local：向量化在本地 HF 模型上完成，不调用任何外部 API。**

    - `local`：直接加载本地模型；模型缺失时**报错**并给出准备指引，
      绝不静默降级（静默降级会让检索质量悄悄变差，且看日志才发现）；
    - `auto`：local → ollama → hashing 的降级链，**不含云端接口**；
    - `ollama`：本地 Ollama 服务（同机进程，仍属本地部署）；
    - `dashscope`：仅显式配置时使用，属云端调用并会打日志提醒；
    - `hashing`：零依赖确定性兜底（仅测试/应急，质量显著下降）。
    """

    settings = settings or get_settings()
    configure_hf_env(settings)
    choice = (settings.embedding_provider or "local").lower()

    if choice == "hashing":
        logger.warning("embedding_provider=hashing：使用确定性哈希兜底，检索质量低于语义模型")
        return HashingEmbedder()

    if choice == "ollama":
        return OllamaEmbedder(settings.ollama_base_url, settings.ollama_embedding_model)

    if choice == "dashscope":  # pragma: no cover - 仅显式配置时
        if not settings.dashscope_api_key:
            raise EmbeddingUnavailable("embedding_provider=dashscope 但未配置 DASHSCOPE_API_KEY")
        logger.warning(
            "embedding_provider=dashscope：向量化走阿里云接口（云端调用）；"
            "如需纯本地请改为 local 并运行 scripts/prepare_model.py"
        )
        return DashScopeEmbedder(settings.dashscope_api_key, settings.dashscope_embedding_model)

    local = SentenceTransformerEmbedder(settings.embedding_model, offline=settings.embedding_offline)
    if choice == "local":
        try:
            await local._ensure_model()
        except Exception as exc:  # noqa: BLE001 - 给出可执行的修复指引
            raise EmbeddingUnavailable(
                f"本地向量模型 {settings.embedding_model} 无法加载：{exc}\n"
                f"模型目录：{settings.model_dir}\n"
                "修复：`.venv/bin/python scripts/prepare_model.py --check` 查看状态，"
                "用 `--copy-from-cache`（从本机 HF 缓存复制）或 `--download`（联网下载）准备模型；"
                "若要临时降级可设 EMBEDDING_PROVIDER=auto"
            ) from exc
        return local

    # ---- auto：本地模型 → ollama → hashing（不调用云端接口）----
    try:  # pragma: no cover - 依赖本机模型
        await local._ensure_model()
        return local
    except Exception as exc:  # noqa: BLE001 - 逐级降级
        logger.warning("本地 embedding 不可用（%s），尝试下一级", exc)

    ollama = OllamaEmbedder(settings.ollama_base_url, settings.ollama_embedding_model)
    if await ollama.ping():
        logger.warning("改用本地 Ollama（%s）做向量化", settings.ollama_embedding_model)
        return ollama

    logger.warning(
        "本地语义模型与 Ollama 都不可用，降级为 hashing 兜底（检索质量下降，仅保证链路可用）"
    )
    return HashingEmbedder()


# --------------------------------------------------------------------------- #
# 向量库后端
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class SearchHit:
    """向量检索命中（chunk 与余弦相似度）。"""

    chunk: Chunk
    score: float


class _SimpleBackend:
    """内置向量库：chunks.jsonl + vectors.npy，零外部依赖。"""

    name = "simple"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._chunks: list[Chunk] = []
        self._vectors: np.ndarray | None = None

    # ---- 持久化 ----
    @property
    def _chunk_path(self) -> Path:
        return self.root / SIMPLE_INDEX_NAME

    @property
    def _vector_path(self) -> Path:
        return self.root / SIMPLE_VECTORS_NAME

    def load(self) -> None:
        if self._chunk_path.exists():
            self._chunks = [
                Chunk.from_payload(json.loads(line))
                for line in self._chunk_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        if self._vector_path.exists() and self._chunks:
            self._vectors = np.load(self._vector_path).astype(np.float32)
            if self._vectors.shape[0] != len(self._chunks):  # pragma: no cover - 数据损坏兜底
                logger.warning("向量数与切片数不一致，重置向量索引")
                self._vectors = None
        if self._vectors is None:
            self._vectors = (
                np.zeros((0, 0), dtype=np.float32) if not self._chunks else None
            )

    def save(self) -> None:
        with self._chunk_path.open("w", encoding="utf-8") as fh:
            for chunk in self._chunks:
                fh.write(json.dumps(chunk.to_payload(), ensure_ascii=False) + "\n")
        if self._vectors is not None:
            np.save(self._vector_path, self._vectors)

    # ---- 读写 ----
    def count(self) -> int:
        return len(self._chunks)

    def chunks(self) -> list[Chunk]:
        return list(self._chunks)

    def add(self, chunks: Sequence[Chunk], vectors: np.ndarray) -> None:
        if len(chunks) != vectors.shape[0]:
            raise ValueError("切片数与向量数不一致")
        existing = {c.chunk_id: i for i, c in enumerate(self._chunks)}
        for chunk, vector in zip(chunks, vectors):
            if chunk.chunk_id in existing:
                # 同 ID 覆盖（重跑入库不产生重复）
                self._chunks[existing[chunk.chunk_id]] = chunk
                assert self._vectors is not None
                self._vectors[existing[chunk.chunk_id]] = vector
            else:
                existing[chunk.chunk_id] = len(self._chunks)
                self._chunks.append(chunk)
                if self._vectors is None or self._vectors.size == 0:
                    self._vectors = vector.reshape(1, -1)
                else:
                    self._vectors = np.vstack([self._vectors, vector.reshape(1, -1)])
        self.save()

    def reset(self) -> None:
        self._chunks = []
        self._vectors = np.zeros((0, 0), dtype=np.float32)
        self.save()

    def delete_document(self, doc_id: str) -> int:
        keep = [i for i, c in enumerate(self._chunks) if c.doc_id != doc_id]
        removed = len(self._chunks) - len(keep)
        if removed:
            self._chunks = [self._chunks[i] for i in keep]
            if self._vectors is not None and self._vectors.size:
                self._vectors = self._vectors[keep]
            self.save()
        return removed

    def search(
        self, vector: np.ndarray, top_k: int, where: dict[str, Any] | None = None
    ) -> list[SearchHit]:
        if self._vectors is None or self._vectors.size == 0 or not self._chunks:
            return []
        query = vector.reshape(-1).astype(np.float32)
        scores = self._vectors @ query
        order = np.argsort(-scores)
        hits: list[SearchHit] = []
        for idx in order:
            chunk = self._chunks[int(idx)]
            if not _match_where(chunk, where):
                continue
            hits.append(SearchHit(chunk=chunk, score=float(scores[int(idx)])))
            if len(hits) >= top_k:
                break
        return hits


class _ChromaBackend:
    """Chroma 持久化后端（开发文档 5.3）。"""

    name = "chroma"

    def __init__(self, root: Path, *, collection: str = "kb_chunks") -> None:
        import chromadb

        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(root))
        self._collection = self._client.get_or_create_collection(
            name=collection, metadata={"hnsw:space": "cosine"}
        )

    def count(self) -> int:
        return int(self._collection.count())

    def chunks(self) -> list[Chunk]:
        payload = self._collection.get(include=["metadatas", "documents"])
        out: list[Chunk] = []
        for meta in payload.get("metadatas") or []:
            data = dict(meta or {})
            out.append(Chunk.from_payload(json.loads(data.pop("_payload"))))
        return out

    def add(self, chunks: Sequence[Chunk], vectors: np.ndarray) -> None:
        if not chunks:
            return
        self._collection.upsert(
            ids=[c.chunk_id for c in chunks],
            embeddings=[v.tolist() for v in vectors],
            documents=[c.text for c in chunks],
            metadatas=[
                {
                    "_payload": json.dumps(c.to_payload(), ensure_ascii=False),
                    "doc_id": c.doc_id,
                    "doc_title": c.doc_title,
                    "version": c.version,
                    "category": c.category,
                    "device_model": c.device_model,
                    "page": int(c.page),
                    "section": c.section,
                }
                for c in chunks
            ],
        )

    def reset(self) -> None:
        self._client.delete_collection(self._collection.name)
        self._collection = self._client.get_or_create_collection(
            name=self._collection.name, metadata={"hnsw:space": "cosine"}
        )

    def delete_document(self, doc_id: str) -> int:
        found = self._collection.get(where={"doc_id": doc_id})
        ids = found.get("ids") or []
        if ids:
            self._collection.delete(ids=ids)
        return len(ids)

    def search(
        self, vector: np.ndarray, top_k: int, where: dict[str, Any] | None = None
    ) -> list[SearchHit]:
        if self.count() == 0:
            return []
        payload = self._collection.query(
            query_embeddings=[vector.reshape(-1).tolist()],
            n_results=top_k,
            where=_chroma_where(where),
            include=["metadatas", "distances"],
        )
        hits: list[SearchHit] = []
        metadatas = (payload.get("metadatas") or [[]])[0]
        distances = (payload.get("distances") or [[]])[0]
        for meta, distance in zip(metadatas, distances):
            data = dict(meta or {})
            chunk = Chunk.from_payload(json.loads(data.pop("_payload")))
            hits.append(SearchHit(chunk=chunk, score=1.0 - float(distance)))
        return hits


def _match_where(chunk: Chunk, where: dict[str, Any] | None) -> bool:
    """simple 后端的元数据过滤（与 Chroma 的 where 语义对齐）。"""

    if not where:
        return True
    for key, value in where.items():
        if key.startswith("$"):  # pragma: no cover - 暂不支持逻辑运算符
            continue
        actual = getattr(chunk, key, None)
        if isinstance(value, dict):
            if "$eq" in value and actual != value["$eq"]:
                return False
            if "$in" in value and actual not in value["$in"]:
                return False
        elif actual != value:
            return False
    return True


def _chroma_where(where: dict[str, Any] | None) -> dict[str, Any] | None:
    if not where:
        return None
    clauses = []
    for key, value in where.items():
        clauses.append({key: value if isinstance(value, dict) else {"$eq": value}})
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


# --------------------------------------------------------------------------- #
# VectorStore：对外唯一入口
# --------------------------------------------------------------------------- #


class VectorStore:
    """向量库门面：切片入库、向量检索、清单校验。

    上层只应该 `await get_store()` 拿到实例，然后调用 add_chunks / search /
    search_text / all_chunks；不要绕过它去碰 chromadb 或 numpy 索引文件。
    """

    def __init__(
        self,
        *,
        root: Path,
        embedder: Embedder,
        backend: _SimpleBackend | _ChromaBackend,
    ) -> None:
        self.root = root
        self.embedder = embedder
        self.backend = backend
        self._loaded = False
        self._load_lock = asyncio.Lock()

    # ---- 生命周期 ----
    async def ensure_ready(self) -> None:
        """加载索引并校验清单与当前 embedding 实例一致。"""

        async with self._load_lock:
            if self._loaded:
                return
            if isinstance(self.backend, _SimpleBackend):
                await asyncio.to_thread(self.backend.load)
            self._loaded = True
        manifest = self.manifest()
        if manifest and manifest.get("signature") not in (None, self.embedder.signature):
            raise EmbeddingMismatchError(
                "索引与当前 embedding 实例不匹配："
                f"索引 {manifest.get('signature')} / 当前 {self.embedder.signature}。"
                "请重建索引（scripts/build_index.py --rebuild）"
            )

    def manifest(self) -> dict[str, Any]:
        path = self.root / MANIFEST_NAME
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:  # pragma: no cover - 清单损坏兜底
            return {}

    def _write_manifest(self) -> None:
        payload = {
            "provider": self.embedder.name,
            "model": self.embedder.model,
            "signature": self.embedder.signature,
            "dim": int(self._vector_dim()),
            "backend": self.backend.name,
            "n_chunks": self.backend.count(),
            "documents": sorted({c.doc_title for c in self.backend.chunks()}),
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        (self.root / MANIFEST_NAME).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _vector_dim(self) -> int:
        try:
            return int(self.embedder.dim)
        except EmbeddingUnavailable:  # pragma: no cover - hashing 之外的懒加载
            return 0

    # ---- 写入 ----
    async def add_chunks(self, chunks: Sequence[Chunk]) -> int:
        """向量化并写入切片（同 chunk_id 覆盖，天然幂等）。"""

        await self.ensure_ready()
        if not chunks:
            return 0
        vectors = await self.embedder.embed([c.search_text for c in chunks])
        self.backend.add(chunks, vectors)
        self._write_manifest()
        return len(chunks)

    async def reset(self) -> None:
        await self.ensure_ready()
        self.backend.reset()
        self._write_manifest()

    async def delete_document(self, doc_id: str) -> int:
        await self.ensure_ready()
        removed = self.backend.delete_document(doc_id)
        self._write_manifest()
        return removed

    # ---- 读取 ----
    async def all_chunks(self) -> list[Chunk]:
        """全部切片（BM25 词法索引与切片查询接口共用）。"""

        await self.ensure_ready()
        return self.backend.chunks()

    async def count(self) -> int:
        await self.ensure_ready()
        return self.backend.count()

    async def search_vector(
        self,
        query: str,
        *,
        top_k: int,
        where: dict[str, Any] | None = None,
    ) -> list[SearchHit]:
        await self.ensure_ready()
        vectors = await self.embedder.embed([query])
        hits = self.backend.search(vectors[0], top_k, where)
        return hits

    async def get_chunk(self, chunk_id: str) -> Chunk | None:
        await self.ensure_ready()
        for chunk in self.backend.chunks():
            if chunk.chunk_id == chunk_id:
                return chunk
        return None

    async def neighbors(self, chunk_id: str, count: int = 1) -> list[Chunk]:
        """取同文档相邻切片（kb_get_chunk 的 neighbors 参数）。"""

        target = await self.get_chunk(chunk_id)
        if target is None:
            return []
        siblings = sorted(
            (c for c in await self.all_chunks() if c.doc_id == target.doc_id),
            key=lambda c: (c.page, c.index),
        )
        position = next((i for i, c in enumerate(siblings) if c.chunk_id == chunk_id), None)
        if position is None:  # pragma: no cover - 理论不可达
            return []
        lo = max(0, position - count)
        hi = min(len(siblings), position + count + 1)
        return siblings[lo:hi]

    async def stats(self) -> dict[str, Any]:
        """给 /api/health 与 scripts/inspect_chunks.py 用的统计信息。"""

        await self.ensure_ready()
        chunks = self.backend.chunks()
        docs = sorted({(c.doc_title, c.version) for c in chunks})
        total_tokens = sum(c.n_tokens for c in chunks)
        return {
            "backend": self.backend.name,
            "provider": self.embedder.name,
            "model": self.embedder.model,
            "dim": self._vector_dim(),
            "n_chunks": len(chunks),
            "n_documents": len(docs),
            "documents": [f"{title} {ver}".strip() for title, ver in docs],
            "avg_chars": round(sum(c.n_chars for c in chunks) / len(chunks), 1) if chunks else 0,
            "avg_tokens": round(total_tokens / len(chunks), 1) if chunks else 0,
            "max_tokens": max((c.n_tokens for c in chunks), default=0),
            "pages": sorted({c.page for c in chunks}),
            "manifest": self.manifest(),
        }


# --------------------------------------------------------------------------- #
# 单例
# --------------------------------------------------------------------------- #

_store: VectorStore | None = None
_store_lock: asyncio.Lock | None = None


def _lock() -> asyncio.Lock:
    """显式 asyncio.Lock（开发文档 6.4：异步初始化不使用缓存装饰器）。"""

    global _store_lock
    if _store_lock is None:
        _store_lock = asyncio.Lock()
    return _store_lock


def _build_backend(root: Path, settings: Settings) -> _SimpleBackend | _ChromaBackend:
    choice = (settings.vector_backend or "auto").lower()
    if choice == "simple":
        return _SimpleBackend(root)
    if choice == "chroma":
        return _ChromaBackend(root)
    try:
        return _ChromaBackend(root)
    except Exception as exc:  # noqa: BLE001 - chromadb 不可用时兜底
        logger.warning("Chroma 不可用（%s），改用内置 simple 向量库", exc)
        return _SimpleBackend(root)


async def get_store(*, force_new: bool = False) -> VectorStore:
    """获取向量库单例（进程内共享，建库与查询同一实例）。"""

    global _store
    async with _lock():
        if _store is None or force_new:
            settings = get_settings()
            settings.ensure_dirs()
            embedder = await create_embedder(settings)
            backend = _build_backend(Path(settings.chroma_dir), settings)
            _store = VectorStore(
                root=Path(settings.chroma_dir), embedder=embedder, backend=backend
            )
            await _store.ensure_ready()
            logger.debug(
                "VectorStore 就绪：backend=%s provider=%s chunks=%d",
                backend.name,
                embedder.name,
                backend.count(),
            )
        return _store


def reset_store() -> None:
    """清空单例（测试用；不影响磁盘索引）。"""

    global _store
    _store = None


__all__ = [
    "DashScopeEmbedder",
    "resolve_model_ref",
    "apply_hf_offline_env",
    "configure_hf_env",
    "Embedder",
    "EmbeddingMismatchError",
    "EmbeddingUnavailable",
    "HashingEmbedder",
    "OllamaEmbedder",
    "SearchHit",
    "SentenceTransformerEmbedder",
    "VectorStore",
    "create_embedder",
    "get_store",
    "reset_store",
]
