"""POLICY_QA 在线检索结果通过 RunLifecycle 外部行为验收。"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

from run_lifecycle_support import RunLifecyclePostgresTest


class FakePolicyRetriever:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.queries = []

    async def retrieve(self, query):
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return self.result


class PolicyQaLifecycleTests(RunLifecyclePostgresTest):
    async def test_policy_qa_rejects_a_profile_only_input(self):
        from app.run_lifecycle import (
            RunLifecycle,
            RunLifecycleError,
            StructuredProfileInput,
            SubmitRunCommand,
        )

        lifecycle = RunLifecycle(self.dsn)
        self.addAsyncCleanup(lifecycle.close)
        with self.assertRaises(RunLifecycleError) as raised:
            await lifecycle.SubmitRun(
                SubmitRunCommand(
                    principal_id="principal-policy",
                    session_id=None,
                    task_kind="POLICY_QA",
                    input=StructuredProfileInput(
                        province="湖南",
                        application_year=2025,
                        category="物理类",
                        batch="本科批",
                    ),
                    idempotency_key="policy-qa-wrong-input",
                )
            )
        self.assertEqual(
            raised.exception.reason,
            "RETRIEVAL_SCOPE_INVALID",
        )

    async def test_policy_qa_rejects_an_unpublished_year_before_creating_a_run(self):
        from app.run_lifecycle import (
            PolicyQaInput,
            RunLifecycle,
            RunLifecycleError,
            SubmitRunCommand,
        )

        await self.seed_release()
        lifecycle = RunLifecycle(self.dsn)
        self.addAsyncCleanup(lifecycle.close)

        with self.assertRaises(RunLifecycleError) as raised:
            await lifecycle.SubmitRun(
                SubmitRunCommand(
                    principal_id="principal-policy",
                    session_id=None,
                    task_kind="POLICY_QA",
                    input=PolicyQaInput(
                        query_text="转专业政策是什么？",
                        province="湖南",
                        application_year=2026,
                        category="物理类",
                        batch="本科批",
                    ),
                    idempotency_key="policy-qa-unpublished-year",
                )
            )

        self.assertEqual(
            raised.exception.reason,
            "RELEASE_UNAVAILABLE",
        )

    async def test_policy_qa_publishes_a_retrievable_evidence_result(self):
        from app.run_lifecycle import (
            PolicyQaInput,
            RunLifecycle,
            SubmitRunCommand,
        )
        from rag.policy_retrieval import (
            DocumentApplicability,
            DocumentEvidence,
            EvidenceRef,
            PolicyRetrievalResult,
        )

        release_id = await self.seed_release()
        evidence = EvidenceRef(
            evidence_id="evidence-policy-1",
            evidence_type="DOCUMENT",
            release_id=release_id,
            content_sha256="a" * 64,
            created_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
            producer_version="policy-retrieval-v1",
            document=DocumentEvidence(
                doc_id="doc-charter",
                chunk_id="chunk-transfer",
                title="湖南测试大学2025年招生章程",
                document_type="charter",
                issuer="湖南测试大学",
                issuer_type="university",
                issuer_id="school-1001",
                school_id="1001",
                source_uri="https://example.invalid/charter",
                fetched_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
                applicability=DocumentApplicability(
                    regions=("湖南",),
                    categories=("物理类",),
                    batches=("本科批",),
                    valid_from=date(2025, 1, 1),
                    valid_to=date(2025, 12, 31),
                ),
                section_path="第二章 > 转专业",
                excerpt_sha256="a" * 64,
                char_start=0,
                char_end=14,
                excerpt="第一条 学生可以申请转专业。",
                release_id=release_id,
                document_content_sha256="b" * 64,
                raw_storage_uri="s3://bucket/charter.txt",
                chunk_set_version="chunks-v1",
            ),
        )
        retriever = FakePolicyRetriever(
            PolicyRetrievalResult(
                status="FOUND",
                release_id=release_id,
                evidence=(evidence,),
            )
        )
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            policy_retriever=retriever,
        )
        self.addAsyncCleanup(lifecycle.close)

        receipt = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-policy",
                session_id=None,
                task_kind="POLICY_QA",
                input=PolicyQaInput(
                    query_text="转专业政策是什么？",
                    province="湖南",
                    application_year=2025,
                    category="物理类",
                    batch="本科批",
                    school_ids=("1001",),
                    document_types=("charter",),
                    mode="hybrid",
                ),
                idempotency_key="policy-qa-result",
            )
        )

        for _ in range(200):
            view = await lifecycle.GetRun(
                principal_id="principal-policy",
                run_id=receipt.run_id,
            )
            if view.state == "SUCCEEDED":
                break
            await asyncio.sleep(0.01)

        self.assertEqual(view.state, "SUCCEEDED")
        self.assertEqual(view.result.result_kind, "POLICY_EVIDENCE")
        self.assertEqual(view.result_payload["retrieval_status"], "FOUND")
        self.assertEqual(
            view.result_payload["evidence"][0]["evidence_id"],
            "evidence-policy-1",
        )
        self.assertEqual(
            view.result_payload["evidence"][0]["evidence_type"],
            "DOCUMENT",
        )
        self.assertEqual(
            view.result_payload["evidence"][0]["content_sha256"],
            "a" * 64,
        )
        self.assertEqual(
            view.result_payload["evidence"][0]["producer_version"],
            "policy-retrieval-v1",
        )
        self.assertEqual(retriever.queries[0].release_id, release_id)
        self.assertEqual(retriever.queries[0].school_ids, ("1001",))

    async def test_policy_qa_unsupported_scope_fails_with_the_stable_reason(self):
        from app.run_lifecycle import (
            PolicyQaInput,
            RunLifecycle,
            SubmitRunCommand,
        )
        from rag.policy_retrieval import PolicyRetrievalResult

        release_id = await self.seed_release()
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            policy_retriever=FakePolicyRetriever(
                PolicyRetrievalResult(
                    status="UNSUPPORTED_SCOPE",
                    release_id=release_id,
                )
            ),
        )
        self.addAsyncCleanup(lifecycle.close)
        receipt = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-policy",
                session_id=None,
                task_kind="POLICY_QA",
                input=PolicyQaInput(
                    query_text="转专业政策是什么？",
                    province="湖南",
                    application_year=2025,
                    category="不限",
                    batch="本科批",
                ),
                idempotency_key="policy-qa-unsupported",
            )
        )

        view = await self._wait_for_terminal(
            lifecycle,
            principal_id="principal-policy",
            run_id=receipt.run_id,
        )

        self.assertEqual(view.state, "FAILED")
        self.assertEqual(view.terminal_reason, "UNSUPPORTED_SCOPE")
        self.assertIsNone(view.result)

    async def test_policy_qa_index_identity_error_is_not_no_evidence(self):
        from app.run_lifecycle import (
            PolicyQaInput,
            RunLifecycle,
            SubmitRunCommand,
        )
        from rag.policy_retrieval import PolicyRetrievalError

        await self.seed_release()
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            policy_retriever=FakePolicyRetriever(
                error=PolicyRetrievalError(
                    "INDEX_MODEL_MISMATCH",
                )
            ),
        )
        self.addAsyncCleanup(lifecycle.close)
        receipt = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-policy",
                session_id=None,
                task_kind="POLICY_QA",
                input=PolicyQaInput(
                    query_text="转专业政策是什么？",
                    province="湖南",
                    application_year=2025,
                    category="物理类",
                    batch="本科批",
                ),
                idempotency_key="policy-qa-index-error",
            )
        )

        view = await self._wait_for_terminal(
            lifecycle,
            principal_id="principal-policy",
            run_id=receipt.run_id,
        )

        self.assertEqual(view.state, "FAILED")
        self.assertEqual(view.terminal_reason, "INDEX_MODEL_MISMATCH")
        self.assertIsNone(view.result)

    @staticmethod
    async def _wait_for_terminal(lifecycle, *, principal_id, run_id):
        for _ in range(200):
            view = await lifecycle.GetRun(
                principal_id=principal_id,
                run_id=run_id,
            )
            if view.state in {"SUCCEEDED", "FAILED", "EXPIRED", "CANCELLED"}:
                return view
            await asyncio.sleep(0.01)
        raise AssertionError("run did not reach a terminal state")
