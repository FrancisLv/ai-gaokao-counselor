"""RetrievePolicy 外部行为测试：默认不依赖真实数据库或模型。"""
from __future__ import annotations

import unittest
from collections.abc import Mapping
from contextlib import nullcontext
from datetime import date, datetime, timezone
import hashlib
import os
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from rag.policy_retrieval import (
    ApplicabilityScope,
    PolicyRetrievalError,
    PolicyRetrievalQuery,
    RetrievePolicy,
)

TEST_DSN = os.getenv("ORCHESTRATOR_TEST_DSN")
ROOT = Path(__file__).resolve().parents[3]


class FakeCursor:
    def __init__(self, responses):
        self._responses = iter(responses)
        self._current = None
        self.sql = ()
        self.params = ()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.sql = (*self.sql, sql)
        self.params = (*self.params, params)
        normalized = " ".join(sql.split())
        if normalized.startswith(("SELECT NULL::vector <=>", "SELECT set_config(")):
            self._current = {}
        else:
            self._current = next(self._responses)
        if isinstance(self._current, BaseException):
            raise self._current

    def fetchone(self):
        return self._current if isinstance(self._current, Mapping) else None

    def fetchall(self):
        return self._current if isinstance(self._current, list) else []


class FakeConnection:
    def __init__(self, *responses):
        self.cursor_obj = FakeCursor(responses)

    def cursor(self, row_factory=None):
        return self.cursor_obj

    def transaction(self):
        return nullcontext()


class TracingCursor:
    def __init__(self, cursor, capture):
        self._cursor = cursor
        self._capture = capture

    def __enter__(self):
        self._cursor.__enter__()
        return self

    def __exit__(self, *args):
        return self._cursor.__exit__(*args)

    def execute(self, sql, params=None):
        if "<=>" in sql:
            self._capture.vector_sql = sql
            self._capture.vector_params = params
        return self._cursor.execute(sql, params)

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()


class TracingConnection:
    def __init__(self, conn):
        self._conn = conn
        self.vector_sql = None
        self.vector_params = None

    def cursor(self, row_factory=None):
        return TracingCursor(
            self._conn.cursor(row_factory=row_factory),
            self,
        )

    def transaction(self):
        return self._conn.transaction()


class DeterministicEmbedder:
    def __init__(self, model_name="deterministic-test", dim=1024, vector=None):
        self.model_name = model_name
        self.dim = dim
        self.vector = list(vector or [0.0] * dim)
        self.calls = 0

    def encode_single(self, text):
        self.calls += 1
        return list(self.vector)


class FailingEmbedder(DeterministicEmbedder):
    def encode_single(self, text):
        self.calls += 1
        raise RuntimeError("deterministic embedding outage")


class PolicyRetrievalTests(unittest.TestCase):
    def query(self, **changes) -> PolicyRetrievalQuery:
        values = {
            "principal_id": "principal-test",
            "query_text": "转专业政策",
            "scope": ApplicabilityScope(
                province="湖南",
                application_year=2025,
                category="物理类",
                batch="本科批",
            ),
            "release_id": "rel-test",
            "mode": "keyword",
        }
        values.update(changes)
        return PolicyRetrievalQuery(**values)

    def evidence_row(self, chunk_id: str) -> dict:
        text = f"{chunk_id}：学生可以申请转专业。"
        return {
            "chunk_id": chunk_id,
            "doc_id": f"doc-{chunk_id}",
            "section_path": "第二章 > 转专业",
            "text": text,
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "char_start": 0,
            "char_end": len(text),
            "chunk_set_version": f"{chunk_id}-chunks",
            "title": "湖南测试大学2025年招生章程",
            "document_type": "charter",
            "issuer": "湖南测试大学",
            "issuer_type": "university",
            "issuer_id": "school-1001",
            "school_id": "1001",
            "source_uri": "https://example.invalid/charter",
            "raw_storage_uri": "s3://bucket/charter.txt",
            "fetched_at": datetime(
                2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc
            ),
            "content_sha256": "a" * 64,
            "regions": ["湖南"],
            "categories": ["物理类"],
            "batches": ["本科批"],
            "valid_from": date(2025, 1, 1),
            "valid_to": date(2025, 12, 31),
            "candidate_count": 2,
        }

    def test_unpublished_release_is_unavailable(self):
        result = RetrievePolicy(
            FakeConnection({"status": "STAGING", "revoked_at": None})
        ).retrieve(self.query())

        self.assertEqual(result.status, "RELEASE_UNAVAILABLE")
        self.assertEqual(result.release_id, "rel-test")
        self.assertEqual(result.evidence, ())

    def test_database_failure_before_release_validation_is_a_technical_failure(self):
        with self.assertRaises(PolicyRetrievalError) as raised:
            RetrievePolicy(
                FakeConnection(
                    psycopg.OperationalError("deterministic database outage")
                )
            ).retrieve(self.query())

        self.assertEqual(raised.exception.reason, "RETRIEVAL_UNAVAILABLE")

    def test_keyword_query_failure_is_a_technical_failure(self):
        with self.assertRaises(PolicyRetrievalError) as raised:
            RetrievePolicy(
                FakeConnection(
                    {"status": "PUBLISHED", "revoked_at": None},
                    psycopg.OperationalError("deterministic keyword outage"),
                )
            ).retrieve(self.query())

        self.assertEqual(raised.exception.reason, "KEYWORD_UNAVAILABLE")

    def test_missing_or_unsupported_scope_is_rejected_without_defaults(self):
        published = {"status": "PUBLISHED", "revoked_at": None}
        cases = (
            ApplicabilityScope("湖南", None, "物理类", "本科批"),
            ApplicabilityScope("湖北", 2025, "物理类", "本科批"),
            ApplicabilityScope("湖南", 2025, "艺术类", "本科批"),
            ApplicabilityScope("湖南", 2025, "物理类", "专科批"),
        )
        for scope in cases:
            with self.subTest(scope=scope):
                result = RetrievePolicy(FakeConnection(published)).retrieve(
                    self.query(scope=scope)
                )
                self.assertEqual(result.status, "UNSUPPORTED_SCOPE")
                self.assertEqual(result.evidence, ())

    def test_vector_mode_returns_the_same_stable_document_evidence(self):
        row = {
            "chunk_id": "chunk-a",
            "doc_id": "doc-a",
            "section_path": "第二章 > 转专业",
            "text": "第一条 学生可以申请转专业。",
            "text_sha256": "74c7043699be90ad02a14a796532109e54f9d3eb936dde9a24e41cea6acc1eea",
            "char_start": 100,
            "char_end": 114,
            "chunk_set_version": "chunkset-a",
            "title": "湖南测试大学2025年招生章程",
            "document_type": "charter",
            "issuer": "湖南测试大学",
            "issuer_type": "university",
            "issuer_id": "school-1001",
            "school_id": "1001",
            "source_uri": "https://example.invalid/charter",
            "raw_storage_uri": "s3://bucket/charter.txt",
            "fetched_at": datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
            "content_sha256": "a" * 64,
            "regions": ["湖南"],
            "categories": ["物理类"],
            "batches": ["本科批"],
            "valid_from": date(2025, 1, 1),
            "valid_to": date(2025, 12, 31),
            "vector_distance": 0.1,
            "candidate_count": 1,
        }
        result = RetrievePolicy(
            FakeConnection(
                {"status": "PUBLISHED", "revoked_at": None},
                {
                    "embedding_model": "deterministic-test",
                    "embedding_dim": 1024,
                    "index_version": "idx-v1",
                },
                [],
                [row],
            ),
            embedder=DeterministicEmbedder(),
            today=lambda: date(2025, 11, 19),
        ).retrieve(self.query(mode="vector"))

        self.assertEqual(result.status, "FOUND")
        self.assertEqual(result.release_id, "rel-test")
        self.assertEqual(
            [item.evidence_id for item in result.evidence],
            [
                "evidence_1cdc6455cab9c62515557ed6763c1607fa27c79b4fbb2ec7a6c8eaf41c21da37"
            ],
        )
        self.assertEqual(result.evidence[0].document.excerpt, row["text"])
        self.assertEqual(result.diagnostics.vector_candidates, 1)
        self.assertEqual(result.diagnostics.keyword_candidates, 0)

    def test_vector_mode_rejects_a_chunk_set_outside_the_release_index_identity(self):
        embedder = DeterministicEmbedder()
        with self.assertRaises(PolicyRetrievalError) as raised:
            RetrievePolicy(
                FakeConnection(
                    {"status": "PUBLISHED", "revoked_at": None},
                    {
                        "embedding_model": "deterministic-test",
                        "embedding_dim": 1024,
                        "index_version": "idx-v1",
                    },
                    [
                        {
                            "chunk_set_version": "chunkset-a",
                            "embedding_model": "deterministic-test",
                            "embedding_dim": 1024,
                            "index_version": "idx-v2",
                        }
                    ],
                ),
                embedder=embedder,
            ).retrieve(self.query(mode="vector"))

        self.assertEqual(raised.exception.reason, "INDEX_MODEL_MISMATCH")
        self.assertEqual(embedder.calls, 0)

    def test_vector_model_failure_is_a_technical_fault_not_no_evidence(self):
        with self.assertRaises(PolicyRetrievalError) as raised:
            RetrievePolicy(
                FakeConnection(
                    {"status": "PUBLISHED", "revoked_at": None},
                    {
                        "embedding_model": "deterministic-test",
                        "embedding_dim": 1024,
                        "index_version": "idx-v1",
                    },
                    [],
                ),
                embedder=FailingEmbedder(),
            ).retrieve(self.query(mode="vector"))

        self.assertEqual(raised.exception.reason, "VECTOR_UNAVAILABLE")

    def test_vector_query_failure_is_a_technical_fault_not_no_evidence(self):
        with self.assertRaises(PolicyRetrievalError) as raised:
            RetrievePolicy(
                FakeConnection(
                    {"status": "PUBLISHED", "revoked_at": None},
                    {
                        "embedding_model": "deterministic-test",
                        "embedding_dim": 1024,
                        "index_version": "idx-v1",
                    },
                    [],
                    psycopg.OperationalError("deterministic vector query outage"),
                ),
                embedder=DeterministicEmbedder(),
            ).retrieve(self.query(mode="vector"))

        self.assertEqual(raised.exception.reason, "VECTOR_UNAVAILABLE")

    def test_vector_without_an_embedder_is_unavailable_not_a_model_mismatch(self):
        with self.assertRaises(PolicyRetrievalError) as raised:
            RetrievePolicy(
                FakeConnection(
                    {"status": "PUBLISHED", "revoked_at": None},
                    {
                        "embedding_model": "deterministic-test",
                        "embedding_dim": 1024,
                        "index_version": "idx-v1",
                    },
                )
            ).retrieve(self.query(mode="vector"))

        self.assertEqual(raised.exception.reason, "VECTOR_UNAVAILABLE")

    def test_vector_rejects_a_query_model_or_dimension_that_differs_from_release(self):
        for embedder in (
            DeterministicEmbedder(model_name="other-model"),
            DeterministicEmbedder(dim=3, vector=[1.0, 0.0, 0.0]),
        ):
            with self.subTest(embedder=embedder):
                with self.assertRaises(PolicyRetrievalError) as raised:
                    RetrievePolicy(
                        FakeConnection(
                            {"status": "PUBLISHED", "revoked_at": None},
                            {
                                "embedding_model": "deterministic-test",
                                "embedding_dim": 1024,
                                "index_version": "idx-v1",
                            },
                        ),
                        embedder=embedder,
                    ).retrieve(self.query(mode="vector"))

                self.assertEqual(
                    raised.exception.reason,
                    "INDEX_MODEL_MISMATCH",
                )
                self.assertEqual(embedder.calls, 0)

    def test_no_keyword_candidate_is_no_evidence(self):
        result = RetrievePolicy(
            FakeConnection(
                {"status": "PUBLISHED", "revoked_at": None},
                [],
            )
        ).retrieve(self.query())

        self.assertEqual(result.status, "NO_EVIDENCE")
        self.assertEqual(result.evidence, ())
        self.assertEqual(result.diagnostics.keyword_candidates, 0)

    def test_hybrid_uses_one_based_rrf_with_stable_chunk_tiebreak(self):
        dual_rank_one_and_two = self.evidence_row("chunk-b")
        vector_only_rank_two = self.evidence_row("chunk-a")
        keyword_only_rank_one = self.evidence_row("chunk-c")
        result = RetrievePolicy(
            FakeConnection(
                {"status": "PUBLISHED", "revoked_at": None},
                {
                    "embedding_model": "deterministic-test",
                    "embedding_dim": 1024,
                    "index_version": "idx-v1",
                },
                [],
                [dual_rank_one_and_two, vector_only_rank_two],
                [keyword_only_rank_one, dual_rank_one_and_two],
            ),
            embedder=DeterministicEmbedder(),
            today=lambda: date(2025, 11, 19),
        ).retrieve(self.query(mode="hybrid"))

        self.assertEqual(result.status, "FOUND")
        # b=1/61+1/62, c=1/61, a=1/62.
        self.assertEqual(
            [item.document.chunk_id for item in result.evidence],
            ["chunk-b", "chunk-c", "chunk-a"],
        )
        self.assertEqual(result.diagnostics.vector_candidates, 2)
        self.assertEqual(result.diagnostics.keyword_candidates, 2)
        self.assertIsNone(result.diagnostics.degraded_reason)

    def test_hybrid_equal_single_channel_scores_break_ties_by_chunk_id(self):
        vector_only_chunk_b = self.evidence_row("chunk-b")
        keyword_only_chunk_a = self.evidence_row("chunk-a")
        result = RetrievePolicy(
            FakeConnection(
                {"status": "PUBLISHED", "revoked_at": None},
                {
                    "embedding_model": "deterministic-test",
                    "embedding_dim": 1024,
                    "index_version": "idx-v1",
                },
                [],
                [vector_only_chunk_b],
                [keyword_only_chunk_a],
            ),
            embedder=DeterministicEmbedder(),
            today=lambda: date(2025, 11, 19),
        ).retrieve(self.query(mode="hybrid"))

        self.assertEqual(result.status, "FOUND")
        # Both channels contribute exactly 1/(60+1); chunk_id breaks the tie.
        self.assertEqual(
            [item.document.chunk_id for item in result.evidence],
            ["chunk-a", "chunk-b"],
        )

    def test_hybrid_vector_failure_degrades_to_same_scope_keyword_route(self):
        keyword_row = self.evidence_row("chunk-keyword")
        keyword_row["candidate_count"] = 1
        result = RetrievePolicy(
            FakeConnection(
                {"status": "PUBLISHED", "revoked_at": None},
                psycopg.OperationalError("deterministic vector outage"),
                [keyword_row],
            ),
            embedder=DeterministicEmbedder(),
            today=lambda: date(2025, 11, 19),
        ).retrieve(self.query(mode="hybrid"))

        self.assertEqual(result.status, "FOUND")
        self.assertEqual(
            [item.document.chunk_id for item in result.evidence],
            ["chunk-keyword"],
        )
        self.assertEqual(result.diagnostics.vector_candidates, 0)
        self.assertEqual(result.diagnostics.keyword_candidates, 1)
        self.assertEqual(
            result.diagnostics.degraded_reason,
            "VECTOR_UNAVAILABLE",
        )

    def test_hybrid_keyword_failure_degrades_to_same_scope_vector_route(self):
        vector_row = self.evidence_row("chunk-vector")
        result = RetrievePolicy(
            FakeConnection(
                {"status": "PUBLISHED", "revoked_at": None},
                {
                    "embedding_model": "deterministic-test",
                    "embedding_dim": 1024,
                    "index_version": "idx-v1",
                },
                [],
                [vector_row],
                psycopg.OperationalError("deterministic keyword outage"),
            ),
            embedder=DeterministicEmbedder(),
            today=lambda: date(2025, 11, 19),
        ).retrieve(self.query(mode="hybrid"))

        self.assertEqual(result.status, "FOUND")
        self.assertEqual(
            [item.document.chunk_id for item in result.evidence],
            ["chunk-vector"],
        )
        self.assertEqual(result.diagnostics.vector_candidates, 1)
        self.assertEqual(result.diagnostics.keyword_candidates, 0)
        self.assertEqual(
            result.diagnostics.degraded_reason,
            "KEYWORD_UNAVAILABLE",
        )

    def test_hybrid_both_channel_failures_are_a_technical_failure(self):
        with self.assertRaises(PolicyRetrievalError) as raised:
            RetrievePolicy(
                FakeConnection(
                    {"status": "PUBLISHED", "revoked_at": None},
                    psycopg.OperationalError("deterministic vector outage"),
                    psycopg.OperationalError("deterministic keyword outage"),
                ),
                embedder=DeterministicEmbedder(),
                today=lambda: date(2025, 11, 19),
            ).retrieve(self.query(mode="hybrid"))

        self.assertEqual(
            raised.exception.reason,
            "RETRIEVAL_UNAVAILABLE",
        )

    def test_hybrid_failed_route_plus_successful_empty_route_is_no_evidence(self):
        result = RetrievePolicy(
            FakeConnection(
                {"status": "PUBLISHED", "revoked_at": None},
                {
                    "embedding_model": "deterministic-test",
                    "embedding_dim": 1024,
                    "index_version": "idx-v1",
                },
                [],
                [],
                psycopg.OperationalError("deterministic keyword outage"),
            ),
            embedder=DeterministicEmbedder(),
            today=lambda: date(2025, 11, 19),
        ).retrieve(self.query(mode="hybrid"))

        self.assertEqual(result.status, "NO_EVIDENCE")
        self.assertEqual(result.evidence, ())
        self.assertEqual(result.diagnostics.vector_candidates, 0)
        self.assertEqual(result.diagnostics.keyword_candidates, 0)
        self.assertEqual(
            result.diagnostics.degraded_reason,
            "KEYWORD_UNAVAILABLE",
        )

    def test_hybrid_returns_valid_evidence_when_only_one_route_has_hits(self):
        for vector_rows, keyword_rows, expected_chunk in (
            ([self.evidence_row("chunk-vector")], [], "chunk-vector"),
            ([], [self.evidence_row("chunk-keyword")], "chunk-keyword"),
        ):
            with self.subTest(expected_chunk=expected_chunk):
                result = RetrievePolicy(
                    FakeConnection(
                        {"status": "PUBLISHED", "revoked_at": None},
                        {
                            "embedding_model": "deterministic-test",
                            "embedding_dim": 1024,
                            "index_version": "idx-v1",
                        },
                        [],
                        vector_rows,
                        keyword_rows,
                    ),
                    embedder=DeterministicEmbedder(),
                    today=lambda: date(2025, 11, 19),
                ).retrieve(self.query(mode="hybrid"))

                self.assertEqual(result.status, "FOUND")
                self.assertEqual(
                    [item.document.chunk_id for item in result.evidence],
                    [expected_chunk],
                )

    def test_hybrid_returns_no_evidence_only_when_both_routes_have_zero_hits(self):
        result = RetrievePolicy(
            FakeConnection(
                {"status": "PUBLISHED", "revoked_at": None},
                {
                    "embedding_model": "deterministic-test",
                    "embedding_dim": 1024,
                    "index_version": "idx-v1",
                },
                [],
                [],
                [],
            ),
            embedder=DeterministicEmbedder(),
            today=lambda: date(2025, 11, 19),
        ).retrieve(self.query(mode="hybrid"))

        self.assertEqual(result.status, "NO_EVIDENCE")
        self.assertEqual(result.evidence, ())
        self.assertEqual(result.diagnostics.vector_candidates, 0)
        self.assertEqual(result.diagnostics.keyword_candidates, 0)

    def test_chunk_is_mapped_to_stable_release_bound_document_evidence(self):
        row = {
            "chunk_id": "chunk-a",
            "doc_id": "doc-a",
            "section_path": "第二章 > 转专业",
            "text": "第一条 学生可以申请转专业。",
            "text_sha256": "74c7043699be90ad02a14a796532109e54f9d3eb936dde9a24e41cea6acc1eea",
            "char_start": 100,
            "char_end": 114,
            "chunk_set_version": "chunkset-a",
            "title": "湖南测试大学2025年招生章程",
            "document_type": "charter",
            "issuer": "湖南测试大学",
            "issuer_type": "university",
            "issuer_id": "school-1001",
            "school_id": "1001",
            "source_uri": "https://example.invalid/charter",
            "raw_storage_uri": "s3://bucket/charter.txt",
            "fetched_at": datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
            "content_sha256": "a" * 64,
            "regions": ["湖南"],
            "categories": ["物理类"],
            "batches": ["本科批"],
            "valid_from": date(2025, 1, 1),
            "valid_to": date(2025, 12, 31),
            "keyword_similarity": 0.9,
            "candidate_count": 1,
        }
        result = RetrievePolicy(
            FakeConnection(
                {"status": "PUBLISHED", "revoked_at": None},
                [row],
            ),
            today=lambda: date(2025, 11, 19),
        ).retrieve(self.query())

        self.assertEqual(result.status, "FOUND")
        self.assertEqual(result.release_id, "rel-test")
        self.assertEqual(len(result.evidence), 1)
        reference = result.evidence[0]
        self.assertEqual(reference.evidence_id, "evidence_1cdc6455cab9c62515557ed6763c1607fa27c79b4fbb2ec7a6c8eaf41c21da37")
        self.assertEqual(reference.content_sha256, row["text_sha256"])
        self.assertEqual(reference.release_id, "rel-test")
        document = reference.document
        self.assertEqual(document.doc_id, "doc-a")
        self.assertEqual(document.chunk_id, "chunk-a")
        self.assertEqual(document.title, row["title"])
        self.assertEqual(document.issuer, row["issuer"])
        self.assertEqual(document.source_uri, row["source_uri"])
        self.assertEqual(document.raw_storage_uri, row["raw_storage_uri"])
        self.assertEqual(document.excerpt, row["text"])
        self.assertEqual(document.excerpt_sha256, row["text_sha256"])
        self.assertEqual((document.char_start, document.char_end), (100, 114))
        self.assertEqual(document.release_id, "rel-test")
        self.assertEqual(document.applicability.regions, ("湖南",))
        self.assertEqual(result.diagnostics.keyword_candidates, 1)

    def test_evidence_projection_failure_is_a_technical_failure(self):
        invalid_row = self.evidence_row("chunk-invalid")
        invalid_row["source_uri"] = None

        with self.assertRaises(PolicyRetrievalError) as raised:
            RetrievePolicy(
                FakeConnection(
                    {"status": "PUBLISHED", "revoked_at": None},
                    [invalid_row],
                ),
                today=lambda: date(2025, 11, 19),
            ).retrieve(self.query())

        self.assertEqual(raised.exception.reason, "EVIDENCE_PROJECTION_FAILED")


@unittest.skipUnless(TEST_DSN, "ORCHESTRATOR_TEST_DSN 未设置")
class PolicyRetrievalPostgresTests(unittest.TestCase):
    dsn = TEST_DSN or ""

    def setUp(self):
        self.schema = "policy_retrieval_" + uuid4().hex
        self.conn = psycopg.connect(
            self.dsn,
            row_factory=dict_row,
            autocommit=True,
        )
        self.conn.execute(f'CREATE SCHEMA "{self.schema}"')
        self.conn.execute(f'SET search_path TO "{self.schema}", public')
        self.conn.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
        self.conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public")
        for migration in (
            "migrations/00_schema.sql",
            "migrations/07_policy_chunk_trgm.sql",
            "migrations/08_release_and_run_foundation.sql",
            "migrations/15_policy_retrieval_release_scope.sql",
            "migrations/16_policy_vector_release_identity.sql",
        ):
            self._apply_sql_file(migration)

    def tearDown(self):
        try:
            self.conn.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')
        finally:
            self.conn.close()

    def _apply_sql_file(self, relative_path):
        sql = (ROOT / relative_path).read_text(encoding="utf-8")
        for statement in sql.split(";"):
            statement = statement.strip()
            if statement:
                self.conn.execute(statement)

    def _seed_release_with_two_chunk_sets(self):
        release_id = "rel-policy-test"
        self.conn.execute(
            """
            INSERT INTO release (release_id, status, manifest, manifest_sha256, published_at)
            VALUES (%s, 'PUBLISHED', '{}'::jsonb, %s, NOW())
            """,
            (release_id, "1" * 64),
        )
        document_sha256 = hashlib.sha256(b"document").hexdigest()
        self.conn.execute(
            """
            INSERT INTO policy_doc (
                id, title, issuer, effective_year, region, source_uri, raw_storage_uri,
                document_type, issuer_type, issuer_id, school_id, fetched_at,
                content_sha256
            )
            VALUES (
                'doc-a', '湖南测试大学2025年招生章程', '湖南测试大学', 2025,
                '湖南', 'https://example.invalid/charter', 's3://bucket/charter.txt',
                'charter', 'university', 'school-1001', '1001', NOW(),
                %s
            )
            """,
            (document_sha256,),
        )
        for chunk_set_version in ("chunkset-old", "chunkset-current"):
            self.conn.execute(
                """
                INSERT INTO policy_chunk_set (
                    chunk_set_version, doc_id, content_sha256, embedding_model,
                    embedding_dim, index_version
                )
                VALUES (%s, 'doc-a', %s, 'deterministic-test', 1024, 'idx-v1')
                """,
                (chunk_set_version, document_sha256),
            )
        for chunk_set_version, chunk_id, text in (
            ("chunkset-old", "chunk-old", "旧分块：学生可以申请转专业。"),
            ("chunkset-current", "chunk-current", "现行分块：学生可以申请转专业。"),
        ):
            self.conn.execute(
                """
                INSERT INTO policy_chunk (
                    id, doc_id, chunk_set_version, section_path, text, text_sha256,
                    char_start, char_end, embedding_model, embedding_dim, index_version
                )
                VALUES (
                    %s, 'doc-a', %s, '第二章 > 转专业', %s, %s,
                    0, %s, 'deterministic-test', 1024, 'idx-v1'
                )
                """,
                (
                    chunk_id,
                    chunk_set_version,
                    text,
                    hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    len(text),
                ),
            )
        self.conn.execute(
            """
            INSERT INTO policy_applicability (
                doc_id, regions, categories, batches, valid_from, valid_to
            )
            VALUES ('doc-a', ARRAY['湖南'], ARRAY['物理类'], ARRAY['本科批'], DATE '2025-01-01', DATE '2025-12-31')
            """
        )
        self.conn.execute(
            """
            INSERT INTO release_policy_doc (release_id, doc_id, chunk_set_version)
            VALUES (%s, 'doc-a', 'chunkset-current')
            """,
            (release_id,),
        )
        self._seed_release_policy_index(release_id)
        return release_id

    def _seed_release(self, release_id="rel-filter-test"):
        self.conn.execute(
            """
            INSERT INTO release (release_id, status, manifest, manifest_sha256, published_at)
            VALUES (%s, 'PUBLISHED', '{}'::jsonb, %s, NOW())
            """,
            (release_id, "2" * 64),
        )
        self._seed_release_policy_index(release_id)
        return release_id

    def _seed_release_policy_index(
        self,
        release_id,
        *,
        embedding_model="deterministic-test",
        embedding_dim=1024,
        index_version="idx-v1",
    ):
        self.conn.execute(
            """
            INSERT INTO release_policy_index (
                release_id, embedding_model, embedding_dim, index_version
            )
            VALUES (%s, %s, %s, %s)
            """,
            (release_id, embedding_model, embedding_dim, index_version),
        )

    @staticmethod
    def _vector_literal(axis=0, dim=1024):
        values = ["0"] * dim
        values[axis] = "1"
        return "[" + ",".join(values) + "]"

    def _seed_policy_document(
        self,
        release_id,
        doc_id,
        *,
        text="学生可以申请转专业。",
        school_id="1001",
        document_type="charter",
        issuer_type="university",
        regions=("湖南",),
        categories=("物理类",),
        batches=("本科批",),
        valid_from="2025-01-01",
        valid_to="2025-12-31",
        with_applicability=True,
        embedding_axis=0,
    ):
        content_sha256 = hashlib.sha256(doc_id.encode("utf-8")).hexdigest()
        chunk_set_version = f"{doc_id}-chunks"
        chunk_id = f"{doc_id}-chunk"
        self.conn.execute(
            """
            INSERT INTO policy_doc (
                id, title, issuer, effective_year, region, source_uri, raw_storage_uri,
                document_type, issuer_type, issuer_id, school_id, fetched_at,
                content_sha256
            )
            VALUES (
                %s, %s, '湖南测试大学', 2025, '湖南',
                %s, 's3://bucket/charter.txt', %s, %s, %s, %s, NOW(), %s
            )
            """,
            (
                doc_id,
                f"{doc_id} 标题",
                f"https://example.invalid/{doc_id}",
                document_type,
                issuer_type,
                f"issuer-{doc_id}",
                school_id,
                content_sha256,
            ),
        )
        self.conn.execute(
            """
            INSERT INTO policy_chunk_set (
                chunk_set_version, doc_id, content_sha256, embedding_model,
                embedding_dim, index_version
            )
            VALUES (%s, %s, %s, 'deterministic-test', 1024, 'idx-v1')
            """,
            (chunk_set_version, doc_id, content_sha256),
        )
        self.conn.execute(
            """
            INSERT INTO policy_chunk (
                id, doc_id, chunk_set_version, section_path, text, text_sha256,
                char_start, char_end, embedding, embedding_model, embedding_dim,
                index_version
            )
            VALUES (
                %s, %s, %s, '第二章 > 转专业', %s, %s,
                0, %s, %s::vector, 'deterministic-test', 1024, 'idx-v1'
            )
            """,
            (
                chunk_id,
                doc_id,
                chunk_set_version,
                text,
                hashlib.sha256(text.encode("utf-8")).hexdigest(),
                len(text),
                self._vector_literal(embedding_axis),
            ),
        )
        if with_applicability:
            self.conn.execute(
                """
                INSERT INTO policy_applicability (
                    doc_id, regions, categories, batches, valid_from, valid_to
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    doc_id,
                    list(regions),
                    list(categories),
                    list(batches),
                    valid_from,
                    valid_to,
                ),
            )
        self.conn.execute(
            """
            INSERT INTO release_policy_doc (release_id, doc_id, chunk_set_version)
            VALUES (%s, %s, %s)
            """,
            (release_id, doc_id, chunk_set_version),
        )

    def test_release_reads_only_its_mapped_chunk_set(self):
        release_id = self._seed_release_with_two_chunk_sets()
        query = PolicyRetrievalQuery(
            principal_id="principal-test",
            query_text="转专业",
            scope=ApplicabilityScope("湖南", 2025, "物理类", "本科批"),
            release_id=release_id,
            mode="keyword",
        )

        result = RetrievePolicy(
            self.conn,
            today=lambda: date(2025, 11, 19),
        ).retrieve(query)

        self.assertEqual(result.status, "FOUND")
        self.assertEqual(
            [item.document.chunk_id for item in result.evidence],
            ["chunk-current"],
        )

    def test_release_mapping_rejects_a_chunk_set_owned_by_another_document(self):
        release_id = self._seed_release("rel-cross-document-test")
        self._seed_policy_document(release_id, "doc-a")
        self._seed_policy_document(release_id, "doc-b")

        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            with self.conn.transaction():
                self.conn.execute(
                    """
                    UPDATE release_policy_doc
                    SET chunk_set_version = 'doc-b-chunks'
                    WHERE release_id = %s AND doc_id = 'doc-a'
                    """,
                    (release_id,),
                )

    def test_hard_filters_exclude_wrong_province_category_batch_school_year_and_type(self):
        release_id = self._seed_release()
        for doc_id, changes in (
            ("doc-match", {}),
            ("doc-wrong-province", {"regions": ("湖北",)}),
            ("doc-wrong-category", {"categories": ("历史类",)}),
            ("doc-wrong-batch", {"batches": ("专科批",)}),
            ("doc-wrong-school", {"school_id": "2002"}),
            ("doc-wrong-type", {"document_type": "policy"}),
            ("doc-expired", {"valid_from": "2024-01-01", "valid_to": "2024-12-31"}),
            ("doc-no-applicability", {"with_applicability": False}),
        ):
            self._seed_policy_document(release_id, doc_id, **changes)

        result = RetrievePolicy(
            self.conn,
            today=lambda: date(2025, 11, 19),
        ).retrieve(
            PolicyRetrievalQuery(
                principal_id="principal-test",
                query_text="转专业",
                scope=ApplicabilityScope("湖南", 2025, "物理类", "本科批"),
                release_id=release_id,
                mode="keyword",
                school_ids=("1001",),
                document_types=("charter",),
            )
        )

        self.assertEqual(result.status, "FOUND")
        self.assertEqual(
            [item.document.doc_id for item in result.evidence],
            ["doc-match"],
        )

    def test_vector_mode_hard_filters_before_ranking_high_similarity_decoys(self):
        release_id = self._seed_release("rel-vector-scope-test")
        for doc_id, changes in (
            ("doc-match", {"embedding_axis": 1}),
            ("doc-wrong-province", {"regions": ("湖北",)}),
            ("doc-wrong-category", {"categories": ("历史类",)}),
            ("doc-wrong-batch", {"batches": ("专科批",)}),
            ("doc-wrong-school", {"school_id": "2002"}),
            ("doc-wrong-type", {"document_type": "policy"}),
            (
                "doc-expired",
                {"valid_from": "2024-01-01", "valid_to": "2024-12-31"},
            ),
        ):
            self._seed_policy_document(release_id, doc_id, **changes)

        result = RetrievePolicy(
            self.conn,
            embedder=DeterministicEmbedder(vector=[1.0] + [0.0] * 1023),
            today=lambda: date(2025, 11, 19),
        ).retrieve(
            PolicyRetrievalQuery(
                principal_id="principal-test",
                query_text="转专业",
                scope=ApplicabilityScope("湖南", 2025, "物理类", "本科批"),
                release_id=release_id,
                mode="vector",
                school_ids=("1001",),
                document_types=("charter",),
            )
        )

        self.assertEqual(result.status, "FOUND")
        self.assertEqual(
            [item.document.doc_id for item in result.evidence],
            ["doc-match"],
        )
        self.assertEqual(result.diagnostics.vector_candidates, 1)

    def test_hybrid_uses_the_same_hard_filter_base_for_both_routes(self):
        release_id = self._seed_release("rel-hybrid-scope-test")
        for doc_id, changes in (
            (
                "doc-a-in-scope-vector-only",
                {"text": "完全无关规定。", "embedding_axis": 0},
            ),
            (
                "doc-b-in-scope-keyword-only",
                {"text": "学生可以申请转专业。", "embedding_axis": 1},
            ),
            (
                "doc-z-in-scope-dual",
                {"text": "学生可以申请转专业。", "embedding_axis": 0},
            ),
            (
                "doc-wrong-province",
                {
                    "text": "学生可以申请转专业。",
                    "embedding_axis": 0,
                    "regions": ("湖北",),
                },
            ),
            (
                "doc-wrong-category",
                {
                    "text": "学生可以申请转专业。",
                    "embedding_axis": 0,
                    "categories": ("历史类",),
                },
            ),
            (
                "doc-wrong-batch",
                {
                    "text": "学生可以申请转专业。",
                    "embedding_axis": 0,
                    "batches": ("专科批",),
                },
            ),
            (
                "doc-wrong-school",
                {
                    "text": "学生可以申请转专业。",
                    "embedding_axis": 0,
                    "school_id": "2002",
                },
            ),
            (
                "doc-wrong-type",
                {
                    "text": "学生可以申请转专业。",
                    "embedding_axis": 0,
                    "document_type": "policy",
                },
            ),
            (
                "doc-expired",
                {
                    "text": "学生可以申请转专业。",
                    "embedding_axis": 0,
                    "valid_from": "2024-01-01",
                    "valid_to": "2024-12-31",
                },
            ),
            (
                "doc-no-applicability",
                {
                    "text": "学生可以申请转专业。",
                    "embedding_axis": 0,
                    "with_applicability": False,
                },
            ),
        ):
            self._seed_policy_document(release_id, doc_id, **changes)

        service = RetrievePolicy(
            self.conn,
            embedder=DeterministicEmbedder(vector=[1.0] + [0.0] * 1023),
            today=lambda: date(2025, 11, 19),
        )
        query = PolicyRetrievalQuery(
            principal_id="principal-test",
            query_text="转专业",
            scope=ApplicabilityScope("湖南", 2025, "物理类", "本科批"),
            release_id=release_id,
            mode="hybrid",
            school_ids=("1001",),
            document_types=("charter",),
        )
        result = service.retrieve(query)
        repeated = service.retrieve(query)

        self.assertEqual(result.status, "FOUND")
        self.assertEqual(
            [item.document.doc_id for item in result.evidence],
            [
                "doc-b-in-scope-keyword-only",
                "doc-z-in-scope-dual",
                "doc-a-in-scope-vector-only",
            ],
        )
        self.assertEqual(result.diagnostics.vector_candidates, 3)
        self.assertEqual(result.diagnostics.keyword_candidates, 2)
        self.assertIsNone(result.diagnostics.degraded_reason)
        self.assertEqual(
            [item.evidence_id for item in result.evidence],
            [item.evidence_id for item in repeated.evidence],
        )

    def test_hybrid_without_embedding_model_degrades_to_keyword_evidence(self):
        release_id = self._seed_release("rel-hybrid-degraded-test")
        self._seed_policy_document(release_id, "doc-keyword")

        result = RetrievePolicy(
            self.conn,
            today=lambda: date(2025, 11, 19),
        ).retrieve(
            PolicyRetrievalQuery(
                principal_id="principal-test",
                query_text="转专业",
                scope=ApplicabilityScope("湖南", 2025, "物理类", "本科批"),
                release_id=release_id,
                mode="hybrid",
            )
        )

        self.assertEqual(result.status, "FOUND")
        self.assertEqual(
            [item.document.doc_id for item in result.evidence],
            ["doc-keyword"],
        )
        self.assertEqual(result.diagnostics.vector_candidates, 0)
        self.assertEqual(result.diagnostics.keyword_candidates, 1)
        self.assertEqual(
            result.diagnostics.degraded_reason,
            "VECTOR_UNAVAILABLE",
        )

    def test_vector_query_uses_hnsw_cosine_order_for_filtered_candidates(self):
        release_id = self._seed_release("rel-vector-hnsw-plan-test")
        for doc_id, changes in (
            ("doc-match", {"embedding_axis": 0}),
            ("doc-in-scope", {"embedding_axis": 1}),
            ("doc-wrong-province", {"regions": ("湖北",)}),
            ("doc-wrong-category", {"categories": ("历史类",)}),
            ("doc-wrong-school", {"school_id": "2002"}),
        ):
            self._seed_policy_document(release_id, doc_id, **changes)

        tracing_conn = TracingConnection(self.conn)
        result = RetrievePolicy(
            tracing_conn,
            embedder=DeterministicEmbedder(vector=[1.0] + [0.0] * 1023),
            today=lambda: date(2025, 11, 19),
        ).retrieve(
            PolicyRetrievalQuery(
                principal_id="principal-test",
                query_text="转专业",
                scope=ApplicabilityScope("湖南", 2025, "物理类", "本科批"),
                release_id=release_id,
                mode="vector",
                school_ids=("1001",),
                top_k=2,
                pool_k=2,
            )
        )

        self.assertEqual(result.status, "FOUND")
        self.assertEqual(
            [item.document.doc_id for item in result.evidence],
            ["doc-match", "doc-in-scope"],
        )
        self.assertIsNotNone(tracing_conn.vector_sql)

        with self.conn.transaction():
            self.conn.execute("SET LOCAL enable_seqscan = off")
            self.conn.execute("SET LOCAL enable_sort = off")
            self.conn.execute("SET LOCAL hnsw.iterative_scan = 'strict_order'")
            explained = self.conn.execute(
                "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "
                + tracing_conn.vector_sql,
                tracing_conn.vector_params,
            ).fetchone()

        plan = explained["QUERY PLAN"][0]["Plan"]
        nodes = []

        def visit(node):
            nodes.append(node)
            for child in node.get("Plans", ()):
                visit(child)

        visit(plan)
        self.assertTrue(
            any(
                node.get("Relation Name") == "policy_chunk"
                and node.get("Index Name") == "idx_chunk_embedding"
                and any(
                    "<=>" in item
                    for item in (
                        (node["Order By"],)
                        if isinstance(node.get("Order By"), str)
                        else node.get("Order By", ())
                    )
                )
                for node in nodes
            ),
            plan,
        )

    def test_filtered_ann_does_not_starve_on_cross_scope_decoys(self):
        release_id = self._seed_release("rel-vector-filtered-ann-test")
        for index in range(60):
            self._seed_policy_document(
                release_id,
                f"doc-decoy-{index:02d}",
                regions=("湖北",),
                embedding_axis=0,
            )
        self._seed_policy_document(
            release_id,
            "doc-in-scope",
            embedding_axis=1,
        )

        result = RetrievePolicy(
            self.conn,
            embedder=DeterministicEmbedder(vector=[1.0] + [0.0] * 1023),
            today=lambda: date(2025, 11, 19),
        ).retrieve(
            PolicyRetrievalQuery(
                principal_id="principal-test",
                query_text="转专业",
                scope=ApplicabilityScope("湖南", 2025, "物理类", "本科批"),
                release_id=release_id,
                mode="vector",
                top_k=1,
                pool_k=1,
            )
        )

        self.assertEqual(result.status, "FOUND")
        self.assertEqual(
            [item.document.doc_id for item in result.evidence],
            ["doc-in-scope"],
        )

    def test_vector_mode_rejects_a_chunk_with_a_different_index_identity(self):
        release_id = self._seed_release("rel-vector-chunk-mismatch-test")
        self._seed_policy_document(release_id, "doc-mismatch")
        self.conn.execute(
            """
            UPDATE policy_chunk
            SET embedding_model = 'other-model'
            WHERE id = 'doc-mismatch-chunk'
            """
        )

        with self.assertRaises(PolicyRetrievalError) as raised:
            RetrievePolicy(
                self.conn,
                embedder=DeterministicEmbedder(vector=[1.0] + [0.0] * 1023),
                today=lambda: date(2025, 11, 19),
            ).retrieve(
                PolicyRetrievalQuery(
                    principal_id="principal-test",
                    query_text="转专业",
                    scope=ApplicabilityScope(
                        "湖南", 2025, "物理类", "本科批"
                    ),
                    release_id=release_id,
                    mode="vector",
                )
            )

        self.assertEqual(raised.exception.reason, "INDEX_MODEL_MISMATCH")

    def test_vector_mode_returns_repeatable_evidence_with_stable_tiebreak(self):
        release_id = self._seed_release("rel-vector-deterministic-test")
        for doc_id in ("doc-b", "doc-a"):
            self._seed_policy_document(
                release_id,
                doc_id,
                text="学生可以申请转专业。",
                embedding_axis=0,
            )
        query = PolicyRetrievalQuery(
            principal_id="principal-test",
            query_text="转专业",
            scope=ApplicabilityScope("湖南", 2025, "物理类", "本科批"),
            release_id=release_id,
            mode="vector",
        )
        service = RetrievePolicy(
            self.conn,
            embedder=DeterministicEmbedder(vector=[1.0] + [0.0] * 1023),
            today=lambda: date(2025, 11, 19),
        )

        first = service.retrieve(query)
        second = service.retrieve(query)

        self.assertEqual(first.status, "FOUND")
        self.assertEqual(
            [item.document.chunk_id for item in first.evidence],
            ["doc-a-chunk", "doc-b-chunk"],
        )
        self.assertEqual(
            [item.evidence_id for item in first.evidence],
            [item.evidence_id for item in second.evidence],
        )
        self.assertEqual(first.evidence[0].release_id, release_id)
        self.assertEqual(first.evidence[0].document.release_id, release_id)
        self.assertEqual(
            first.evidence[0].content_sha256,
            first.evidence[0].document.excerpt_sha256,
        )

    def test_vector_mode_returns_no_evidence_after_a_successful_empty_search(self):
        release_id = self._seed_release("rel-vector-empty-test")

        result = RetrievePolicy(
            self.conn,
            embedder=DeterministicEmbedder(vector=[1.0] + [0.0] * 1023),
            today=lambda: date(2025, 11, 19),
        ).retrieve(
            PolicyRetrievalQuery(
                principal_id="principal-test",
                query_text="转专业",
                scope=ApplicabilityScope("湖南", 2025, "物理类", "本科批"),
                release_id=release_id,
                mode="vector",
            )
        )

        self.assertEqual(result.status, "NO_EVIDENCE")
        self.assertEqual(result.evidence, ())
        self.assertEqual(result.diagnostics.vector_candidates, 0)

    def test_vector_candidate_pool_excludes_unprojectable_chunks_before_ranking(self):
        release_id = self._seed_release("rel-vector-invalid-candidate-test")
        self._seed_policy_document(
            release_id,
            "doc-invalid",
            embedding_axis=0,
        )
        self._seed_policy_document(
            release_id,
            "doc-valid",
            embedding_axis=1,
        )
        self.conn.execute(
            """
            UPDATE policy_doc
            SET source_uri = NULL
            WHERE id = 'doc-invalid'
            """
        )

        result = RetrievePolicy(
            self.conn,
            embedder=DeterministicEmbedder(vector=[1.0] + [0.0] * 1023),
            today=lambda: date(2025, 11, 19),
        ).retrieve(
            PolicyRetrievalQuery(
                principal_id="principal-test",
                query_text="转专业",
                scope=ApplicabilityScope("湖南", 2025, "物理类", "本科批"),
                release_id=release_id,
                mode="vector",
                top_k=1,
                pool_k=1,
            )
        )

        self.assertEqual(result.status, "FOUND")
        self.assertEqual(
            [item.document.doc_id for item in result.evidence],
            ["doc-valid"],
        )

    def test_national_and_long_term_policies_match_without_equal_year_filter(self):
        release_id = self._seed_release("rel-long-term-test")
        self._seed_policy_document(
            release_id,
            "doc-national",
            school_id=None,
            issuer_type="national",
            regions=("全国",),
            categories=("不限",),
            batches=("不限",),
            valid_from="2020-01-01",
            valid_to=None,
        )
        self._seed_policy_document(
            release_id,
            "doc-province",
            school_id=None,
            issuer_type="provincial",
            regions=("湖南",),
            categories=("不限",),
            batches=("不限",),
            valid_from="2020-01-01",
            valid_to=None,
        )
        self._seed_policy_document(
            release_id,
            "doc-expired",
            school_id=None,
            regions=("全国",),
            categories=("不限",),
            batches=("不限",),
            valid_from="2020-01-01",
            valid_to="2024-12-31",
        )

        result = RetrievePolicy(
            self.conn,
            today=lambda: date(2025, 11, 19),
        ).retrieve(
            PolicyRetrievalQuery(
                principal_id="principal-test",
                query_text="转专业",
                scope=ApplicabilityScope("湖南", 2025, "物理类", "本科批"),
                release_id=release_id,
                mode="keyword",
            )
        )

        self.assertEqual(result.status, "FOUND")
        self.assertEqual(
            [item.document.doc_id for item in result.evidence],
            ["doc-national", "doc-province"],
        )

    def test_unprojectable_candidate_is_a_technical_failure(self):
        release_id = self._seed_release("rel-invalid-evidence-test")
        self._seed_policy_document(release_id, "doc-invalid")
        self.conn.execute(
            """
            UPDATE policy_chunk
            SET text_sha256 = %s, char_start = 0, char_end = 1
            WHERE id = 'doc-invalid-chunk'
            """,
            ("0" * 64,),
        )

        with self.assertRaises(PolicyRetrievalError) as raised:
            RetrievePolicy(
                self.conn,
                today=lambda: date(2025, 11, 19),
            ).retrieve(
                PolicyRetrievalQuery(
                    principal_id="principal-test",
                    query_text="转专业",
                    scope=ApplicabilityScope(
                        "湖南", 2025, "物理类", "本科批"
                    ),
                    release_id=release_id,
                    mode="keyword",
                )
            )

        self.assertEqual(
            raised.exception.reason,
            "EVIDENCE_PROJECTION_FAILED",
        )

    def test_keyword_percent_and_underscore_are_literal_not_wildcards(self):
        release_id = self._seed_release("rel-keyword-escaping-test")
        self._seed_policy_document(
            release_id,
            "doc-percent",
            text="奖学金比例A%B政策。",
        )
        self._seed_policy_document(
            release_id,
            "doc-percent-decoy",
            text="奖学金比例AxyzB政策。",
        )
        self._seed_policy_document(
            release_id,
            "doc-underscore",
            text="字段A_B规定。",
        )
        self._seed_policy_document(
            release_id,
            "doc-underscore-decoy",
            text="字段AXB规定。",
        )

        result = RetrievePolicy(
            self.conn,
            today=lambda: date(2025, 11, 19),
        ).retrieve(
            PolicyRetrievalQuery(
                principal_id="principal-test",
                query_text="A%B A_B",
                scope=ApplicabilityScope("湖南", 2025, "物理类", "本科批"),
                release_id=release_id,
                mode="keyword",
            )
        )

        self.assertEqual(result.status, "FOUND")
        self.assertEqual(
            {item.document.doc_id for item in result.evidence},
            {"doc-percent", "doc-underscore"},
        )

    def test_missing_staging_and_revoked_releases_are_unavailable(self):
        self.conn.execute(
            """
            INSERT INTO release (release_id, status, manifest, manifest_sha256)
            VALUES ('rel-staging', 'STAGING', '{}'::jsonb, %s)
            """,
            ("3" * 64,),
        )
        self.conn.execute(
            """
            INSERT INTO release (
                release_id, status, manifest, manifest_sha256, published_at, revoked_at
            )
            VALUES ('rel-revoked', 'REVOKED', '{}'::jsonb, %s, NOW(), NOW())
            """,
            ("4" * 64,),
        )

        for release_id in ("rel-missing", "rel-staging", "rel-revoked"):
            with self.subTest(release_id=release_id):
                result = RetrievePolicy(self.conn).retrieve(
                    PolicyRetrievalQuery(
                        principal_id="principal-test",
                        query_text="转专业",
                        scope=ApplicabilityScope(
                            "湖南", 2025, "物理类", "本科批"
                        ),
                        release_id=release_id,
                        mode="keyword",
                    )
                )
                self.assertEqual(result.status, "RELEASE_UNAVAILABLE")
                self.assertEqual(result.evidence, ())

    def test_repeated_query_has_stable_order_and_evidence_ids(self):
        release_id = self._seed_release("rel-deterministic-test")
        for doc_id in ("doc-b", "doc-a"):
            self._seed_policy_document(
                release_id,
                doc_id,
                text="学生可以申请转专业。",
            )
        query = PolicyRetrievalQuery(
            principal_id="principal-test",
            query_text="转专业",
            scope=ApplicabilityScope("湖南", 2025, "物理类", "本科批"),
            release_id=release_id,
            mode="keyword",
        )
        service = RetrievePolicy(
            self.conn,
            today=lambda: date(2025, 11, 19),
        )

        first = service.retrieve(query)
        second = service.retrieve(query)

        self.assertEqual(
            [item.evidence_id for item in first.evidence],
            [item.evidence_id for item in second.evidence],
        )
        self.assertEqual(
            [item.document.doc_id for item in first.evidence],
            ["doc-a", "doc-b"],
        )


if __name__ == "__main__":
    unittest.main()
