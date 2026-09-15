"""RAG 链路：解析 → 切片 → 索引 → 检索 → 精排 → 上下文 → 引用。

模块职责（开发文档 6.2，依赖方向 rag → config，不得反向 import）：

    ingest.py     解析 + 抽图 + 清洗 + 切片（写入 page / version 等元数据）
    store.py      embedding + 向量库（**向量库唯一出口**，换库只改此文件）
    retriever.py  BM25 + 向量 + RRF + 精排 + 上下文构建
    citation.py   引用生成与校验（编号由代码分配，禁止模型编造）

设计约束：
1. **离线可用**：向量化与精排都做多级降级（本地模型 → 云端接口 → 确定性兜底），
   保证没有密钥、没有外网时链路仍可端到端跑通、仍可评测；
2. **建库与查询共用同一 embedding 实例**（开发文档 5.3）：索引清单里记录
   provider 与维度，不匹配时直接报错，而不是静默给出垃圾相似度；
3. **元数据完整率 100%**：每个切片必须带 page 与 version，否则入库即失败。
"""

from __future__ import annotations

from .ingest import Chunk, ParsedDocument, ingest_path, parse_document
from .store import VectorStore, get_store

__all__ = [
    "Chunk",
    "ParsedDocument",
    "VectorStore",
    "get_store",
    "ingest_path",
    "parse_document",
]
