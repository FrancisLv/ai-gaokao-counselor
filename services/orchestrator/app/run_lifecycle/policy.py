"""RunLifecycle 到 RetrievePolicy 的后台适配器。"""
from __future__ import annotations

import asyncio

import psycopg
from psycopg.rows import dict_row

from rag.policy_retrieval import (
    PolicyRetrievalError,
    PolicyRetrievalQuery,
    PolicyRetrievalResult,
    RetrievePolicy,
)


class PostgresPolicyRetriever:
    """为每次 POLICY_QA 创建独立只读连接，避免共享同步连接跨线程。"""

    def __init__(
        self,
        dsn: str,
        *,
        embedding_model: str | None = None,
    ) -> None:
        self._dsn = dsn
        self._embedding_model = embedding_model
        self._embedder = None

    async def retrieve(
        self,
        query: PolicyRetrievalQuery,
    ) -> PolicyRetrievalResult:
        return await asyncio.to_thread(self._retrieve_sync, query)

    def _retrieve_sync(
        self,
        query: PolicyRetrievalQuery,
    ) -> PolicyRetrievalResult:
        conn = psycopg.connect(self._dsn, row_factory=dict_row)
        try:
            if (
                query.mode in {"hybrid", "vector"}
                and self._embedder is None
                and self._embedding_model
            ):
                try:
                    from rag.embedder import Embedder

                    self._embedder = Embedder(
                        model_name=self._embedding_model,
                    )
                except Exception as exc:
                    if query.mode == "vector":
                        raise PolicyRetrievalError(
                            "VECTOR_UNAVAILABLE",
                            "query embedding model could not be loaded",
                        ) from exc
                    return RetrievePolicy(conn).retrieve(query)
            return RetrievePolicy(
                conn,
                embedder=self._embedder,
            ).retrieve(query)
        finally:
            conn.close()
