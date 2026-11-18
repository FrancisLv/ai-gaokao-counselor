"""检索层：切分、embedding、rerank、RRF 融合（自建薄层，压在 pgvector/tsvector 上）。"""

from rag.etl import Chunk, DocMeta, split_charter, process_single_file, iter_charter_files
from rag.embedder import Embedder
from rag.retrieve import HybridRetriever, RetrievedChunk, extract_terms, rrf_fuse

__all__ = [
    "Chunk",
    "DocMeta",
    "Embedder",
    "HybridRetriever",
    "RetrievedChunk",
    "extract_terms",
    "rrf_fuse",
    "split_charter",
    "process_single_file",
    "iter_charter_files",
]
