"""Release-bound 政策文档检索的唯一公开 seam。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
import hashlib
import json
import re
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row

from rag.retrieve import extract_terms

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DOCUMENT_TYPES = frozenset(
    {"charter", "policy", "physical_exam", "university_notice"}
)
_ISSUER_TYPES = frozenset({"national", "provincial", "university"})
_PRODUCER_VERSION = "policy-retrieval-v1"


@dataclass(frozen=True, slots=True)
class ApplicabilityScope:
    province: str
    application_year: int | None
    category: str
    batch: str


@dataclass(frozen=True, slots=True)
class PolicyRetrievalQuery:
    principal_id: str
    query_text: str
    scope: ApplicabilityScope
    release_id: str
    mode: str = "keyword"
    school_ids: tuple[str, ...] = ()
    document_types: tuple[str, ...] = ()
    top_k: int = 5
    pool_k: int = 20


@dataclass(frozen=True, slots=True)
class PolicyRetrievalDiagnostics:
    vector_candidates: int = 0
    keyword_candidates: int = 0
    rerank_enabled: bool = False
    degraded_reason: str | None = None


@dataclass(frozen=True, slots=True)
class DocumentApplicability:
    regions: tuple[str, ...]
    categories: tuple[str, ...]
    batches: tuple[str, ...]
    valid_from: date
    valid_to: date | None


@dataclass(frozen=True, slots=True)
class DocumentEvidence:
    doc_id: str
    chunk_id: str
    title: str
    document_type: str
    issuer: str
    issuer_type: str
    issuer_id: str
    school_id: str | None
    source_uri: str
    fetched_at: datetime
    applicability: DocumentApplicability
    section_path: str
    excerpt_sha256: str
    char_start: int | None
    char_end: int | None
    excerpt: str
    release_id: str
    document_content_sha256: str
    raw_storage_uri: str
    chunk_set_version: str


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    evidence_id: str
    evidence_type: str
    release_id: str
    content_sha256: str
    created_at: datetime
    producer_version: str
    document: DocumentEvidence


@dataclass(frozen=True, slots=True)
class PolicyRetrievalResult:
    status: str
    release_id: str
    evidence: tuple = ()
    diagnostics: PolicyRetrievalDiagnostics = field(
        default_factory=PolicyRetrievalDiagnostics
    )


class PolicyRetrievalError(Exception):
    def __init__(self, reason: str, message: str | None = None) -> None:
        self.reason = reason
        super().__init__(message or reason)


class RetrievePolicy:
    """调用方与测试共同依赖的唯一政策检索入口。"""

    _SUPPORTED_PROVINCES = frozenset({"湖南"})
    _SUPPORTED_CATEGORIES = frozenset({"物理类", "历史类", "不限"})
    _SUPPORTED_BATCHES = frozenset({"本科批", "不限"})

    def __init__(self, conn, embedder=None, today=None) -> None:
        self._conn = conn
        self._embedder = embedder
        self._today = today or (
            lambda: datetime.now(ZoneInfo("Asia/Shanghai")).date()
        )

    def retrieve(self, query: PolicyRetrievalQuery) -> PolicyRetrievalResult:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT status, revoked_at FROM release WHERE release_id = %s",
                (query.release_id,),
            )
            release = cur.fetchone()

        if (
            release is None
            or release["status"] != "PUBLISHED"
            or release["revoked_at"] is not None
        ):
            return PolicyRetrievalResult(
                status="RELEASE_UNAVAILABLE",
                release_id=query.release_id,
            )

        scope = query.scope
        if (
            scope.province not in self._SUPPORTED_PROVINCES
            or scope.application_year is None
            or scope.application_year < 1
            or scope.category not in self._SUPPORTED_CATEGORIES
            or scope.batch not in self._SUPPORTED_BATCHES
        ):
            return PolicyRetrievalResult(
                status="UNSUPPORTED_SCOPE",
                release_id=query.release_id,
            )

        if query.mode == "keyword":
            rows, candidate_count = self._keyword_candidates(query)
        elif query.mode == "vector":
            try:
                rows, candidate_count = self._vector_candidates(query)
            except psycopg.Error as exc:
                raise PolicyRetrievalError(
                    "VECTOR_UNAVAILABLE",
                    "vector retrieval is temporarily unavailable",
                ) from exc
        else:
            raise PolicyRetrievalError(
                "MODE_UNSUPPORTED",
                f"retrieval mode is not implemented in this slice: {query.mode}",
            )

        if not rows:
            return PolicyRetrievalResult(
                status="NO_EVIDENCE",
                release_id=query.release_id,
                diagnostics=PolicyRetrievalDiagnostics(
                    vector_candidates=candidate_count
                    if query.mode == "vector"
                    else 0,
                    keyword_candidates=candidate_count
                    if query.mode == "keyword"
                    else 0,
                ),
            )

        evidence = []
        for row in rows:
            projected = self._project_evidence(row, query.release_id)
            if projected is not None:
                evidence.append(projected)
            if len(evidence) >= query.top_k:
                break
        if not evidence:
            return PolicyRetrievalResult(
                status="NO_EVIDENCE",
                release_id=query.release_id,
                diagnostics=PolicyRetrievalDiagnostics(
                    vector_candidates=candidate_count
                    if query.mode == "vector"
                    else 0,
                    keyword_candidates=candidate_count
                    if query.mode == "keyword"
                    else 0,
                ),
            )
        return PolicyRetrievalResult(
            status="FOUND",
            release_id=query.release_id,
            evidence=tuple(evidence),
            diagnostics=PolicyRetrievalDiagnostics(
                vector_candidates=candidate_count
                if query.mode == "vector"
                else 0,
                keyword_candidates=candidate_count
                if query.mode == "keyword"
                else 0,
            ),
        )

    def _vector_candidates(
        self,
        query: PolicyRetrievalQuery,
    ) -> tuple[list[dict], int]:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT embedding_model, embedding_dim, index_version
                FROM release_policy_index
                WHERE release_id = %s
                """,
                (query.release_id,),
            )
            identity = cur.fetchone()

        if identity is None:
            raise PolicyRetrievalError(
                "INDEX_MODEL_MISMATCH",
                "the bound release has no complete policy index identity",
            )
        if self._embedder is None:
            raise PolicyRetrievalError(
                "VECTOR_UNAVAILABLE",
                "no query embedding model is configured",
            )
        if (
            identity["embedding_model"] != self._embedder.model_name
            or identity["embedding_dim"] != self._embedder.dim
        ):
            raise PolicyRetrievalError(
                "INDEX_MODEL_MISMATCH",
                "query embedding identity does not match the bound release",
            )

        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT DISTINCT embedding_model, embedding_dim, index_version
                FROM (
                    SELECT
                        cs.embedding_model,
                        cs.embedding_dim,
                        cs.index_version
                    FROM release_policy_doc m
                    JOIN policy_chunk_set cs
                      ON cs.chunk_set_version = m.chunk_set_version
                     AND cs.doc_id = m.doc_id
                    WHERE m.release_id = %s

                    UNION ALL

                    SELECT
                        c.embedding_model,
                        c.embedding_dim,
                        c.index_version
                    FROM release_policy_doc m
                    JOIN policy_chunk c
                      ON c.doc_id = m.doc_id
                     AND c.chunk_set_version = m.chunk_set_version
                    WHERE m.release_id = %s
                ) chunk_identities
                ORDER BY embedding_model, embedding_dim, index_version
                """,
                (query.release_id, query.release_id),
            )
            chunk_identities = cur.fetchall()
        for chunk_identity in chunk_identities:
            if (
                chunk_identity["embedding_model"] != identity["embedding_model"]
                or chunk_identity["embedding_dim"] != identity["embedding_dim"]
                or chunk_identity["index_version"] != identity["index_version"]
            ):
                raise PolicyRetrievalError(
                    "INDEX_MODEL_MISMATCH",
                    "a release chunk set does not match the bound index identity",
                )

        try:
            query_embedding = self._embedder.encode_single(query.query_text)
        except Exception as exc:
            raise PolicyRetrievalError(
                "VECTOR_UNAVAILABLE",
                "query embedding could not be generated",
            ) from exc
        vector = "[" + ",".join(f"{value:.6f}" for value in query_embedding) + "]"
        params = {
            "release_id": query.release_id,
            "query_embedding": vector,
            "pool_k": max(query.pool_k, query.top_k),
            "embedding_model": identity["embedding_model"],
            "embedding_dim": identity["embedding_dim"],
            "index_version": identity["index_version"],
            "province": query.scope.province,
            "category": query.scope.category,
            "batch": query.scope.batch,
            "today": self._today(),
            "school_ids": list(query.school_ids),
            "document_types": list(query.document_types),
        }
        sql = """
            WITH ann_candidates AS MATERIALIZED (
                SELECT
                    c.id AS chunk_id,
                    c.doc_id,
                    c.section_path,
                    c.text,
                    c.text_sha256,
                    c.char_start,
                    c.char_end,
                    c.chunk_set_version,
                    d.title,
                    d.document_type,
                    d.issuer,
                    d.issuer_type,
                    d.issuer_id,
                    d.school_id,
                    d.source_uri,
                    d.raw_storage_uri,
                    d.fetched_at,
                    d.content_sha256,
                    a.regions,
                    a.categories,
                    a.batches,
                    a.valid_from,
                    a.valid_to,
                    c.embedding <=> %(query_embedding)s::vector AS vector_distance
                FROM release_policy_doc m
                JOIN policy_chunk_set cs
                  ON cs.chunk_set_version = m.chunk_set_version
                 AND cs.doc_id = m.doc_id
                JOIN policy_doc d
                  ON d.id = m.doc_id
                JOIN policy_chunk c
                  ON c.doc_id = m.doc_id
                 AND c.chunk_set_version = m.chunk_set_version
                JOIN LATERAL (
                    SELECT
                        pa.regions,
                        pa.categories,
                        pa.batches,
                        pa.valid_from,
                        pa.valid_to
                    FROM policy_applicability pa
                    WHERE pa.doc_id = d.id
                      AND (
                          '全国' = ANY(pa.regions)
                          OR %(province)s = ANY(pa.regions)
                      )
                      AND (
                          %(category)s = '不限'
                          OR '不限' = ANY(pa.categories)
                          OR %(category)s = ANY(pa.categories)
                      )
                      AND (
                          %(batch)s = '不限'
                          OR '不限' = ANY(pa.batches)
                          OR %(batch)s = ANY(pa.batches)
                      )
                      AND pa.valid_from <= %(today)s
                      AND (pa.valid_to IS NULL OR %(today)s <= pa.valid_to)
                    ORDER BY pa.valid_from DESC, pa.valid_to DESC NULLS LAST, pa.id
                    LIMIT 1
                ) a ON TRUE
                WHERE m.release_id = %(release_id)s
                  AND cs.embedding_model = %(embedding_model)s
                  AND cs.embedding_dim = %(embedding_dim)s
                  AND cs.index_version = %(index_version)s
                  AND d.title IS NOT NULL
                  AND d.document_type IS NOT NULL
                  AND d.issuer IS NOT NULL
                  AND d.issuer_type IS NOT NULL
                  AND d.issuer_id IS NOT NULL
                  AND d.source_uri IS NOT NULL
                  AND d.raw_storage_uri IS NOT NULL
                  AND d.fetched_at IS NOT NULL
                  AND d.content_sha256 IS NOT NULL
                  AND c.text IS NOT NULL
                  AND c.text_sha256 IS NOT NULL
                  AND c.section_path IS NOT NULL
                  AND c.chunk_set_version IS NOT NULL
                  AND (
                      cardinality(%(school_ids)s::text[]) = 0
                      OR d.school_id = ANY(%(school_ids)s::text[])
                  )
                  AND (
                      cardinality(%(document_types)s::text[]) = 0
                      OR d.document_type = ANY(%(document_types)s::text[])
                  )
                  AND c.embedding IS NOT NULL
                ORDER BY c.embedding <=> %(query_embedding)s::vector
                LIMIT %(pool_k)s
            )
            SELECT
                ann_candidates.*
            FROM ann_candidates
            ORDER BY vector_distance, chunk_id ASC
        """
        with self._conn.transaction():
            with self._conn.cursor(row_factory=dict_row) as cur:
                # Accessing the vector operator loads the extension before its
                # custom GUCs are set in a fresh backend.
                cur.execute("SELECT NULL::vector <=> NULL::vector")
                cur.execute(
                    """
                    SELECT
                        set_config('hnsw.iterative_scan', 'strict_order', true),
                        set_config('hnsw.max_scan_tuples', '2147483647', true),
                        set_config('hnsw.ef_search', %s, true)
                    """,
                    (str(max(40, params["pool_k"])),),
                )
                cur.execute(sql, params)
                rows = [dict(row) for row in cur.fetchall()]
        return rows, len(rows)

    def _keyword_candidates(
        self,
        query: PolicyRetrievalQuery,
    ) -> tuple[list[dict], int]:
        terms = [
            term
            for term in extract_terms(query.query_text, max_terms=4)
            if len(term) >= 2
        ]
        if not terms:
            return [], 0

        scope = query.scope
        params = {
            "release_id": query.release_id,
            "province": scope.province,
            "category": scope.category,
            "batch": scope.batch,
            "today": self._today(),
            "school_ids": list(query.school_ids),
            "document_types": list(query.document_types),
            "sim_query": " ".join(terms),
            "pool_k": max(query.pool_k, query.top_k),
        }
        keyword_predicates = []
        for index, term in enumerate(terms):
            escaped = (
                term.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            params[f"term_{index}"] = f"%{escaped}%"
            keyword_predicates.append(
                f"text ILIKE %(term_{index})s ESCAPE '\\'"
            )

        sql = f"""
            WITH scope_candidates AS MATERIALIZED (
                SELECT
                    c.id AS chunk_id,
                    c.doc_id,
                    c.section_path,
                    c.text,
                    c.text_sha256,
                    c.char_start,
                    c.char_end,
                    c.chunk_set_version,
                    d.title,
                    d.document_type,
                    d.issuer,
                    d.issuer_type,
                    d.issuer_id,
                    d.school_id,
                    d.source_uri,
                    d.raw_storage_uri,
                    d.fetched_at,
                    d.content_sha256,
                    a.regions,
                    a.categories,
                    a.batches,
                    a.valid_from,
                    a.valid_to
                FROM release_policy_doc m
                JOIN policy_chunk_set cs
                  ON cs.chunk_set_version = m.chunk_set_version
                 AND cs.doc_id = m.doc_id
                JOIN policy_doc d
                  ON d.id = m.doc_id
                JOIN policy_chunk c
                  ON c.doc_id = m.doc_id
                 AND c.chunk_set_version = m.chunk_set_version
                JOIN LATERAL (
                    SELECT
                        pa.regions,
                        pa.categories,
                        pa.batches,
                        pa.valid_from,
                        pa.valid_to
                    FROM policy_applicability pa
                    WHERE pa.doc_id = d.id
                      AND (
                          '全国' = ANY(pa.regions)
                          OR %(province)s = ANY(pa.regions)
                      )
                      AND (
                          %(category)s = '不限'
                          OR '不限' = ANY(pa.categories)
                          OR %(category)s = ANY(pa.categories)
                      )
                      AND (
                          %(batch)s = '不限'
                          OR '不限' = ANY(pa.batches)
                          OR %(batch)s = ANY(pa.batches)
                      )
                      AND pa.valid_from <= %(today)s
                      AND (pa.valid_to IS NULL OR %(today)s <= pa.valid_to)
                    ORDER BY pa.valid_from DESC, pa.valid_to DESC NULLS LAST, pa.id
                    LIMIT 1
                ) a ON TRUE
                WHERE m.release_id = %(release_id)s
                  AND d.title IS NOT NULL
                  AND d.document_type IS NOT NULL
                  AND d.issuer IS NOT NULL
                  AND d.issuer_type IS NOT NULL
                  AND d.issuer_id IS NOT NULL
                  AND d.source_uri IS NOT NULL
                  AND d.raw_storage_uri IS NOT NULL
                  AND d.fetched_at IS NOT NULL
                  AND d.content_sha256 IS NOT NULL
                  AND c.text IS NOT NULL
                  AND c.text_sha256 IS NOT NULL
                  AND c.section_path IS NOT NULL
                  AND c.chunk_set_version IS NOT NULL
                  AND cs.index_version IS NOT NULL
                  AND (
                      cardinality(%(school_ids)s::text[]) = 0
                      OR d.school_id = ANY(%(school_ids)s::text[])
                  )
                  AND (
                      cardinality(%(document_types)s::text[]) = 0
                      OR d.document_type = ANY(%(document_types)s::text[])
                  )
            )
            SELECT
                scope_candidates.*,
                similarity(text, %(sim_query)s) AS keyword_similarity,
                COUNT(*) OVER () AS candidate_count
            FROM scope_candidates
            WHERE {" OR ".join(keyword_predicates)}
            ORDER BY keyword_similarity DESC, chunk_id ASC
            LIMIT %(pool_k)s
        """
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            rows = [dict(row) for row in cur.fetchall()]
        count = int(rows[0]["candidate_count"]) if rows else 0
        return rows, count

    @staticmethod
    def _project_evidence(
        row: dict,
        release_id: str,
    ) -> EvidenceRef | None:
        text = row.get("text")
        text_sha256 = row.get("text_sha256")
        section_path = row.get("section_path")
        title = row.get("title")
        document_type = row.get("document_type")
        issuer = row.get("issuer")
        issuer_type = row.get("issuer_type")
        issuer_id = row.get("issuer_id")
        source_uri = row.get("source_uri")
        raw_storage_uri = row.get("raw_storage_uri")
        fetched_at = row.get("fetched_at")
        document_sha256 = row.get("content_sha256")
        chunk_set_version = row.get("chunk_set_version")

        if not all(
            isinstance(value, str) and value.strip()
            for value in (
                row.get("doc_id"),
                row.get("chunk_id"),
                section_path,
                title,
                issuer,
                issuer_id,
                source_uri,
                raw_storage_uri,
                chunk_set_version,
            )
        ):
            return None
        if (
            document_type not in _DOCUMENT_TYPES
            or issuer_type not in _ISSUER_TYPES
            or not isinstance(text, str)
            or not text
            or len(text) > 600
        ):
            return None
        if (
            not isinstance(text_sha256, str)
            or not _SHA256.fullmatch(text_sha256)
            or hashlib.sha256(text.encode("utf-8")).hexdigest() != text_sha256
            or not isinstance(document_sha256, str)
            or not _SHA256.fullmatch(document_sha256)
        ):
            return None
        if not isinstance(fetched_at, datetime) or fetched_at.tzinfo is None:
            return None

        char_start = row.get("char_start")
        char_end = row.get("char_end")
        if (char_start is None) != (char_end is None):
            return None
        if char_start is not None and (
            isinstance(char_start, bool)
            or isinstance(char_end, bool)
            or not isinstance(char_start, int)
            or not isinstance(char_end, int)
            or char_start < 0
            or char_end < char_start
            or char_end - char_start != len(text)
        ):
            return None

        regions = tuple(row.get("regions") or ())
        categories = tuple(row.get("categories") or ())
        batches = tuple(row.get("batches") or ())
        valid_from = row.get("valid_from")
        valid_to = row.get("valid_to")
        if (
            not regions
            or not categories
            or not batches
            or not all(
                isinstance(value, str) and value
                for value in (*regions, *categories, *batches)
            )
            or not isinstance(valid_from, date)
            or (valid_to is not None and not isinstance(valid_to, date))
            or (valid_to is not None and valid_to < valid_from)
        ):
            return None

        evidence_identity = {
            "document_type": document_type,
            "release_id": release_id,
            "chunk_id": row["chunk_id"],
            "excerpt_sha256": text_sha256,
            "char_start": char_start,
            "char_end": char_end,
        }
        canonical = json.dumps(
            evidence_identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        evidence_id = "evidence_" + hashlib.sha256(
            canonical.encode("utf-8")
        ).hexdigest()
        document = DocumentEvidence(
            doc_id=row["doc_id"],
            chunk_id=row["chunk_id"],
            title=title,
            document_type=document_type,
            issuer=issuer,
            issuer_type=issuer_type,
            issuer_id=issuer_id,
            school_id=row.get("school_id"),
            source_uri=source_uri,
            fetched_at=fetched_at,
            applicability=DocumentApplicability(
                regions=regions,
                categories=categories,
                batches=batches,
                valid_from=valid_from,
                valid_to=valid_to,
            ),
            section_path=section_path,
            excerpt_sha256=text_sha256,
            char_start=char_start,
            char_end=char_end,
            excerpt=text,
            release_id=release_id,
            document_content_sha256=document_sha256,
            raw_storage_uri=raw_storage_uri,
            chunk_set_version=chunk_set_version,
        )
        return EvidenceRef(
            evidence_id=evidence_id,
            evidence_type="DOCUMENT",
            release_id=release_id,
            content_sha256=text_sha256,
            created_at=fetched_at,
            producer_version=_PRODUCER_VERSION,
            document=document,
        )
