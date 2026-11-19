"""检索 golden set 的外部 seam 测试，默认不连接数据库或模型。"""
from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from eval.retrieval_eval import (
    RetrievalDataset,
    build_baseline,
    evaluate_retrieval,
    load_retrieval_dataset,
)
import psycopg
from psycopg.rows import dict_row
from rag.policy_retrieval import (
    DocumentApplicability,
    DocumentEvidence,
    EvidenceRef,
    PolicyRetrievalError,
    PolicyRetrievalResult,
    RetrievePolicy,
)

ROOT = Path(__file__).resolve().parents[3]
TEST_DSN = os.getenv("ORCHESTRATOR_TEST_DSN")


def evidence(
    chunk_id: str,
    doc_id: str,
    *,
    region: str = "湖南",
    category: str = "物理类",
    batch: str = "本科批",
    school_id: str = "school-001",
    valid_from: date = date(2025, 1, 1),
    valid_to: date | None = date(2025, 12, 31),
) -> EvidenceRef:
    text = f"{chunk_id} evidence"
    document = DocumentEvidence(
        doc_id=doc_id,
        chunk_id=chunk_id,
        title="测试章程",
        document_type="charter",
        issuer="测试大学",
        issuer_type="university",
        issuer_id="issuer-001",
        school_id=school_id,
        source_uri="https://example.invalid/document",
        fetched_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        applicability=DocumentApplicability(
            regions=(region,),
            categories=(category,),
            batches=(batch,),
            valid_from=valid_from,
            valid_to=valid_to,
        ),
        section_path="第一章",
        excerpt_sha256="a" * 64,
        char_start=0,
        char_end=len(text),
        excerpt=text,
        release_id="rel-test",
        document_content_sha256="b" * 64,
        raw_storage_uri="s3://test/document",
        chunk_set_version="chunks-v1",
    )
    return EvidenceRef(
        evidence_id=f"evidence-{chunk_id}",
        evidence_type="DOCUMENT",
        release_id="rel-test",
        content_sha256="a" * 64,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        producer_version="test",
        document=document,
    )


class DeterministicRetriever:
    def __init__(self, results):
        self.results = results
        self.queries = []

    def retrieve(self, query):
        self.queries.append(query)
        return self.results[query.query_text]


class GoldenContractFixture:
    """按标注投影结果的 boundary 替身；只验证 harness，不评价检索算法。"""

    def __init__(self, cases):
        self.cases = {case["query"]: case for case in cases}
        self.calls = 0

    def retrieve(self, query):
        self.calls += 1
        case = self.cases[query.query_text]
        if "no_evidence" in case["tags"]:
            return PolicyRetrievalResult("NO_EVIDENCE", query.release_id)
        items = []
        for chunk_id in case["expected_chunk_ids"]:
            items.append(
                evidence(
                    chunk_id,
                    case["expected_doc_ids"][0],
                    region=(
                        "全国"
                        if "national_policy" in case["tags"]
                        else query.scope.province
                    ),
                    category=query.scope.category,
                    batch=query.scope.batch,
                    school_id=(
                        query.school_ids[0] if query.school_ids else None
                    ),
                )
            )
        return PolicyRetrievalResult(
            "FOUND",
            query.release_id,
            tuple(items),
        )


class RetrievalDatasetTests(unittest.TestCase):
    def write_dataset(self, root: Path, case: dict) -> None:
        qa_path = root / "qa.jsonl"
        qa_bytes = (
            json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n"
        ).encode("utf-8")
        qa_path.write_bytes(qa_bytes)
        manifest = {
            "dataset_version": "retrieval-test-v1",
            "retrieval_cases_file": "qa.jsonl",
            "retrieval_cases_sha256": hashlib.sha256(qa_bytes).hexdigest(),
            "case_count": 1,
            "created_at": "2026-11-19T00:00:00+08:00",
            "release_id": "rel-test",
            "owner": "Francis",
            "owner_status": "PENDING_SIGNOFF",
            "immutable": True,
        }
        (root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def valid_case(self) -> dict:
        return {
            "case_id": "case-001",
            "query": "湘潭大学转专业政策",
            "scope": {
                "province": "湖南",
                "application_year": 2025,
                "category": "物理类",
                "batch": "本科批",
            },
            "expected_chunk_ids": ["doc-001_c0001"],
            "expected_doc_ids": ["doc-001"],
            "must_exclude_sources": ["school-999"],
            "tags": ["charter", "school_major"],
            "criticality": "CRITICAL",
        }

    def test_dataset_hash_mismatch_is_rejected_before_retrieval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_dataset(root, self.valid_case())
            with (root / "qa.jsonl").open("ab") as qa_file:
                qa_file.write(b"\n")

            with self.assertRaisesRegex(ValueError, "sha256"):
                load_retrieval_dataset(root)

    def test_first_retrieval_golden_set_is_thirty_agent_drafts(self):
        dataset = load_retrieval_dataset(
            ROOT
            / "data"
            / "golden"
            / "retrieval-hunan-2025-v0.1.0-draft"
        )

        self.assertEqual(len(dataset.cases), 30)
        self.assertEqual(dataset.manifest["owner_status"], "PENDING_SIGNOFF")
        self.assertEqual(
            {case["case_id"] for case in dataset.cases},
            {f"retrieval-{index:03d}" for index in range(1, 31)},
        )
        tags = {tag for case in dataset.cases for tag in case["tags"]}
        self.assertTrue(
            {
                "charter",
                "policy",
                "national_policy",
                "province_policy",
                "school_major",
                "no_evidence",
                "scope_conflict",
                "cross_province",
                "cross_year",
                "cross_category",
                "wrong_school",
            }
            <= tags
        )
        no_evidence = [
            case
            for case in dataset.cases
            if "no_evidence" in case["tags"]
        ]
        self.assertEqual(len(no_evidence), 2)
        self.assertTrue(
            all(
                not case["expected_chunk_ids"]
                and not case["expected_doc_ids"]
                for case in no_evidence
            )
        )

    def test_agent_draft_runs_end_to_end_through_the_retrieve_seam(self):
        dataset = load_retrieval_dataset(
            ROOT
            / "data"
            / "golden"
            / "retrieval-hunan-2025-v0.1.0-draft"
        )
        retriever = GoldenContractFixture(dataset.cases)

        report = evaluate_retrieval(dataset, retriever, mode="hybrid")

        self.assertEqual(retriever.calls, 30)
        self.assertEqual(report.eligible_case_count, 28)
        self.assertEqual(report.no_evidence_passed, 2)
        self.assertEqual(report.no_evidence_failed, 0)
        self.assertEqual(report.metrics.recall_at_5, 1.0)
        self.assertEqual(report.metrics.recall_at_20, 1.0)
        self.assertEqual(report.metrics.mrr, 1.0)
        self.assertEqual(report.metrics.scope_leak_rate, 0.0)
        self.assertEqual(report.gate_status, "PASS")

    def test_expected_and_must_exclude_sources_cannot_overlap(self):
        case = self.valid_case()
        case["must_exclude_sources"] = ["doc-001", "school-999"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_dataset(root, case)

            with self.assertRaisesRegex(ValueError, "must_exclude_sources"):
                load_retrieval_dataset(root)

    def test_scope_values_are_validated_before_retrieval(self):
        case = self.valid_case()
        case["scope"]["province"] = "湖北"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_dataset(root, case)

            with self.assertRaisesRegex(ValueError, "scope.province"):
                load_retrieval_dataset(root)

    def test_required_case_fields_and_enums_are_validated(self):
        case = self.valid_case()
        case["criticality"] = "LOW"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_dataset(root, case)

            with self.assertRaisesRegex(ValueError, "criticality"):
                load_retrieval_dataset(root)

    def test_recall_mrr_and_no_evidence_use_the_contract_denominator(self):
        cases = []
        for index, (query, tags) in enumerate(
            (
                ("命中@5", ["charter"]),
                ("仅命中@20", ["charter"]),
                ("应无证据", ["no_evidence"]),
            ),
            start=1,
        ):
            case = self.valid_case()
            case.update(
                case_id=f"case-{index:03d}",
                query=query,
                expected_chunk_ids=(
                    [] if "no_evidence" in tags else [f"chunk-{index}"]
                ),
                expected_doc_ids=(
                    [] if "no_evidence" in tags else [f"doc-{index}"]
                ),
                tags=tags,
            )
            cases.append(case)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            qa_path = root / "qa.jsonl"
            qa_bytes = (
                "".join(
                    json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n"
                    for case in cases
                )
            ).encode("utf-8")
            qa_path.write_bytes(qa_bytes)
            manifest = {
                "dataset_version": "retrieval-test-v1",
                "retrieval_cases_file": "qa.jsonl",
                "retrieval_cases_sha256": hashlib.sha256(qa_bytes).hexdigest(),
                "case_count": 3,
                "created_at": "2026-11-19T00:00:00+08:00",
                "release_id": "rel-test",
                "owner": "Francis",
                "owner_status": "PENDING_SIGNOFF",
                "immutable": True,
            }
            (root / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            dataset = load_retrieval_dataset(root)

        retriever = DeterministicRetriever(
            {
                "命中@5": PolicyRetrievalResult(
                    "FOUND",
                    "rel-test",
                    (
                        evidence("chunk-1", "doc-1"),
                        evidence("noise-a", "noise-a-doc"),
                    ),
                ),
                "仅命中@20": PolicyRetrievalResult(
                    "FOUND",
                    "rel-test",
                    tuple(
                        evidence(f"noise-{rank}", f"noise-doc-{rank}")
                        for rank in range(2, 7)
                    )
                    + (evidence("chunk-2", "doc-2"),),
                ),
                "应无证据": PolicyRetrievalResult("NO_EVIDENCE", "rel-test"),
            }
        )

        report = evaluate_retrieval(dataset, retriever, mode="keyword")

        self.assertEqual(len(retriever.queries), 3)
        self.assertEqual(
            [query.top_k for query in retriever.queries], [20, 20, 20]
        )
        self.assertEqual(report.metrics.recall_at_5, 0.5)
        self.assertEqual(report.metrics.recall_at_20, 1.0)
        self.assertAlmostEqual(report.metrics.mrr, (1.0 + 1.0 / 6.0) / 2.0)
        self.assertEqual(report.no_evidence_passed, 1)
        self.assertEqual(report.eligible_case_count, 2)
        self.assertEqual(report.gate_status, "FAIL")

    def test_scope_leaks_are_reported_per_case_and_fail_the_gate(self):
        case = self.valid_case()
        case.update(
            query="湘潭大学转专业政策",
            expected_chunk_ids=["chunk-1"],
            expected_doc_ids=["doc-1"],
            school_ids=["school-001"],
            must_exclude_sources=["blocked-doc"],
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_dataset(root, case)
            dataset = load_retrieval_dataset(root)

        retriever = DeterministicRetriever(
            {
                "湘潭大学转专业政策": PolicyRetrievalResult(
                    "FOUND",
                    "rel-test",
                    (
                        evidence("chunk-1", "doc-1"),
                        evidence("cross-province", "doc-p", region="湖北"),
                        evidence(
                            "cross-year",
                            "doc-y",
                            valid_from=date(2024, 1, 1),
                            valid_to=date(2024, 12, 31),
                        ),
                        evidence(
                            "cross-category",
                            "doc-c",
                            category="历史类",
                        ),
                        evidence(
                            "wrong-school",
                            "doc-s",
                            school_id="school-999",
                        ),
                        evidence("blocked", "blocked-doc"),
                    ),
                )
            }
        )

        report = evaluate_retrieval(dataset, retriever, mode="keyword")

        self.assertEqual(report.metrics.recall_at_5, 1.0)
        self.assertEqual(report.metrics.scope_leak_rate, 1.0)
        self.assertEqual(report.gate_status, "FAIL")
        self.assertEqual(
            [leak["kind"] for leak in report.cases[0].scope_leaks],
            [
                "province",
                "year",
                "category",
                "school",
                "must_exclude_source",
            ],
        )
        self.assertEqual(report.cases[0].first_diff["field"], "scope_leaks")

    def test_retrieval_fault_becomes_a_failed_case_not_a_crashed_report(self):
        case = self.valid_case()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_dataset(root, case)
            dataset = load_retrieval_dataset(root)

        class FailingRetriever:
            def retrieve(self, query):
                raise PolicyRetrievalError(
                    "RETRIEVAL_UNAVAILABLE",
                    "deterministic unavailable",
                )

        report = evaluate_retrieval(
            dataset,
            FailingRetriever(),
            mode="keyword",
        )

        self.assertEqual(report.cases[0].status, "RETRIEVAL_UNAVAILABLE")
        self.assertEqual(report.cases[0].actual_evidence, ())
        self.assertEqual(report.cases[0].first_diff["field"], "retrieval_error")
        self.assertEqual(
            report.cases[0].first_diff["actual"]["reason"],
            "RETRIEVAL_UNAVAILABLE",
        )
        self.assertEqual(report.gate_status, "FAIL")

    def test_baseline_identity_and_required_run_fields_are_reproducible(self):
        case = self.valid_case()
        case.update(expected_chunk_ids=["chunk-1"], expected_doc_ids=["doc-1"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_dataset(root, case)
            dataset = load_retrieval_dataset(root)

        retriever = DeterministicRetriever(
            {
                case["query"]: PolicyRetrievalResult(
                    "FOUND",
                    "rel-test",
                    (evidence("chunk-1", "doc-1"),),
                )
            }
        )
        report = evaluate_retrieval(dataset, retriever, mode="hybrid")
        identity = {
            "git_sha": "0123456789abcdef",
            "source_tree_sha256": "f" * 64,
            "python_version": "3.11.0",
            "platform": "Linux",
        }
        retrieval = {
            "mode": "hybrid",
            "embedder": "deterministic-fixture-v1",
            "rerank_enabled": False,
            "rerank_model": None,
            "rrf_k": 60,
            "index_version": "fixture-index-v1",
            "as_of": "2025-11-19",
            "top_k": 20,
            "pool_k": 20,
        }

        first = build_baseline(
            report,
            dataset,
            code_identity=identity,
            retrieval_config=retrieval,
            generated_at="2026-11-19T12:00:00+08:00",
        )
        second = build_baseline(
            report,
            dataset,
            code_identity=identity,
            retrieval_config=retrieval,
            generated_at="2026-11-19T13:00:00+08:00",
        )

        self.assertEqual(first["eval_run_id"], second["eval_run_id"])
        self.assertEqual(first["result_sha256"], second["result_sha256"])
        self.assertEqual(
            first["dataset"]["sha256"],
            dataset.manifest["retrieval_cases_sha256"],
        )
        self.assertEqual(first["code_identity"]["git_sha"], identity["git_sha"])
        self.assertEqual(first["retrieval"]["embedder"], "deterministic-fixture-v1")
        self.assertFalse(first["retrieval"]["rerank_enabled"])
        self.assertEqual(first["retrieval"]["rrf_k"], 60)
        self.assertEqual(
            first["cases"][0]["actual_evidence"][0]["chunk_id"], "chunk-1"
        )


@unittest.skipUnless(TEST_DSN, "ORCHESTRATOR_TEST_DSN is not set")
class RetrievalEvaluationPostgresTests(unittest.TestCase):
    dsn = TEST_DSN or ""

    def setUp(self):
        self.schema = "retrieval_eval_" + uuid4().hex
        self.conn = psycopg.connect(
            self.dsn,
            row_factory=dict_row,
            autocommit=True,
        )
        self.conn.execute(f'CREATE SCHEMA "{self.schema}"')
        self.conn.execute(f'SET search_path TO "{self.schema}", public')
        self.conn.execute(
            "CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public"
        )
        self.conn.execute(
            "CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public"
        )
        for migration in (
            "migrations/00_schema.sql",
            "migrations/07_policy_chunk_trgm.sql",
            "migrations/08_release_and_run_foundation.sql",
            "migrations/15_policy_retrieval_release_scope.sql",
            "migrations/16_policy_vector_release_identity.sql",
        ):
            self.apply_sql_file(migration)
        self.seed_release()

    def tearDown(self):
        try:
            self.conn.execute(
                f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE'
            )
        finally:
            self.conn.close()

    def apply_sql_file(self, relative_path):
        sql = (ROOT / relative_path).read_text(encoding="utf-8")
        for statement in sql.split(";"):
            statement = statement.strip()
            if statement:
                self.conn.execute(statement)

    def seed_release(self):
        self.conn.execute(
            """
            INSERT INTO release (
                release_id, status, manifest, manifest_sha256, published_at
            )
            VALUES (
                'rel-eval-test', 'PUBLISHED', '{}'::jsonb, %s, NOW()
            )
            """,
            ("1" * 64,),
        )
        document_sha256 = hashlib.sha256(b"eval fixture").hexdigest()
        self.conn.execute(
            """
            INSERT INTO policy_doc (
                id, title, issuer, effective_year, region, source_uri,
                raw_storage_uri, document_type, issuer_type, issuer_id,
                school_id, fetched_at, content_sha256
            )
            VALUES (
                'doc-eval', '湖南测试大学2025年招生章程', '湖南测试大学',
                2025, '湖南', 'https://example.invalid/eval',
                's3://bucket/eval.txt', 'charter', 'university',
                'issuer-eval', 'school-eval', NOW(), %s
            )
            """,
            (document_sha256,),
        )
        self.conn.execute(
            """
            INSERT INTO policy_chunk_set (
                chunk_set_version, doc_id, content_sha256, embedding_model,
                embedding_dim, index_version
            )
            VALUES (
                'chunks-eval', 'doc-eval', %s, 'deterministic-test',
                1024, 'idx-eval'
            )
            """,
            (document_sha256,),
        )
        text = "学生可以申请转专业。"
        self.conn.execute(
            """
            INSERT INTO policy_chunk (
                id, doc_id, chunk_set_version, section_path, text,
                text_sha256, char_start, char_end, embedding_model,
                embedding_dim, index_version
            )
            VALUES (
                'chunk-eval', 'doc-eval', 'chunks-eval',
                '第二章 > 转专业', %s, %s, 0, %s,
                'deterministic-test', 1024, 'idx-eval'
            )
            """,
            (
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
            VALUES (
                'doc-eval', ARRAY['湖南'], ARRAY['物理类'],
                ARRAY['本科批'], DATE '2025-01-01', DATE '2025-12-31'
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO release_policy_doc (
                release_id, doc_id, chunk_set_version
            )
            VALUES ('rel-eval-test', 'doc-eval', 'chunks-eval')
            """
        )

    def test_runner_uses_retrieve_policy_against_an_isolated_database(self):
        scope = {
            "province": "湖南",
            "application_year": 2025,
            "category": "物理类",
            "batch": "本科批",
        }
        cases = (
            {
                "case_id": "postgres-found",
                "query": "转专业",
                "scope": scope,
                "school_ids": ["school-eval"],
                "document_types": ["charter"],
                "expected_chunk_ids": ["chunk-eval"],
                "expected_doc_ids": ["doc-eval"],
                "must_exclude_sources": [],
                "tags": ["charter", "school_major"],
                "criticality": "CRITICAL",
            },
            {
                "case_id": "postgres-no-evidence",
                "query": "该校核工程专业体检要求",
                "scope": scope,
                "school_ids": ["school-eval"],
                "document_types": ["charter"],
                "expected_chunk_ids": [],
                "expected_doc_ids": [],
                "must_exclude_sources": [],
                "tags": ["no_evidence", "physical_exam"],
                "criticality": "CRITICAL",
            },
        )
        dataset = RetrievalDataset(
            manifest={
                "dataset_version": "retrieval-postgres-test-v1",
                "release_id": "rel-eval-test",
            },
            cases=cases,
        )
        retriever = RetrievePolicy(
            self.conn,
            today=lambda: date(2025, 11, 19),
        )

        report = evaluate_retrieval(dataset, retriever, mode="keyword")

        self.assertEqual(report.metrics.recall_at_5, 1.0)
        self.assertEqual(report.metrics.recall_at_20, 1.0)
        self.assertEqual(report.metrics.mrr, 1.0)
        self.assertEqual(report.metrics.scope_leak_rate, 0.0)
        self.assertEqual(report.no_evidence_passed, 1)
        self.assertEqual(report.gate_status, "PASS")


if __name__ == "__main__":
    unittest.main()
