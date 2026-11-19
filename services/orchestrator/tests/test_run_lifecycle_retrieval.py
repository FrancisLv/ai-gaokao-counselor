"""RunLifecycle 到在线检索图的生产装配测试。"""
from __future__ import annotations

import asyncio
from uuid import UUID

from run_lifecycle_support import RunLifecyclePostgresTest


class FakeRetrievePolicy:
    def __init__(self):
        self.queries = []

    def retrieve(self, query):
        from rag.policy_retrieval import PolicyRetrievalResult

        self.queries.append(query)
        return PolicyRetrievalResult("NO_EVIDENCE", query.release_id)


class RunLifecycleRetrievalBindingTests(RunLifecyclePostgresTest):
    async def test_confirmed_plan_builds_an_online_retrieval_graph(self):
        from app.run_lifecycle import (
            ConfirmProfileAnswer,
            ResumeRunCommand,
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )
        from app.server import OrchestratorServicer

        release_id = await self.seed_release()
        lifecycle = RunLifecycle(self.dsn, auto_profile=True)
        self.addAsyncCleanup(lifecycle.close)
        receipt = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-a",
                session_id=None,
                task_kind="GENERATE_PLAN",
                input=StructuredProfileInput(
                    province="湖南",
                    application_year=2025,
                    category="物理类",
                    batch="本科批",
                    score=600,
                    subjects=("物理", "化学", "生物"),
                ),
                idempotency_key="retrieval-binding-submit",
            )
        )
        waiting = await self._wait_for_interrupt(lifecycle, receipt.run_id)
        profile_version = UUID(
            waiting.current_interrupt.payload["profile_version"]
        )
        await lifecycle.ResumeRun(
            ResumeRunCommand(
                principal_id="principal-a",
                run_id=receipt.run_id,
                interrupt_id=waiting.current_interrupt.interrupt_id,
                expected_state_version=waiting.state_version,
                profile_version=profile_version,
                answer=ConfirmProfileAnswer(),
                idempotency_key="retrieval-binding-confirm",
            )
        )
        await self._wait_for_confirmed_profile(lifecycle, receipt.run_id)

        policy = FakeRetrievePolicy()
        graph = await OrchestratorServicer(
            lifecycle=lifecycle
        ).BuildRetrievalGraph(
            principal_id="principal-a",
            run_id=receipt.run_id,
            retrieve_policy=policy,
            mode="hybrid",
        )
        updated = await graph.ainvoke(
            {
                "user_text": "转专业政策是什么？",
                "principal_id": "forged",
                "release_id": "latest",
                "profile": {
                    "province": "湖南",
                    "year": "2025",
                    "track": "物理类",
                    "score": 600,
                    "subjects": ["物理", "化学", "生物"],
                },
            }
        )

        self.assertEqual(len(policy.queries), 1)
        query = policy.queries[0]
        self.assertEqual(query.principal_id, "principal-a")
        self.assertEqual(query.release_id, release_id)
        self.assertEqual(query.scope.province, "湖南")
        self.assertEqual(query.scope.application_year, 2025)
        self.assertEqual(query.scope.category, "物理类")
        self.assertEqual(query.scope.batch, "本科批")
        self.assertEqual(query.school_ids, ())
        self.assertEqual(query.document_types, ())
        self.assertEqual(updated["retrieval_status"], "NO_EVIDENCE")

    async def test_policy_qa_binding_uses_trusted_task_scope(self):
        from app.run_lifecycle import (
            RunLifecycle,
            RunLifecycleError,
            StructuredProfileInput,
            SubmitRunCommand,
        )

        release_id = await self.seed_release()
        lifecycle = RunLifecycle(self.dsn)
        self.addAsyncCleanup(lifecycle.close)
        receipt = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-policy",
                session_id=None,
                task_kind="POLICY_QA",
                input=StructuredProfileInput(
                    province="湖南",
                    application_year=2025,
                    category="不限",
                    batch="不限",
                    school_ids=("2002",),
                    document_types=("policy",),
                ),
                idempotency_key="policy-qa-submit",
            )
        )

        binding = await lifecycle.GetRetrievalRunBinding(
            principal_id="principal-policy",
            run_id=receipt.run_id,
            mode="keyword",
        )

        self.assertEqual(binding.principal_id, "principal-policy")
        self.assertEqual(binding.release_id, release_id)
        self.assertEqual(binding.task_kind, "POLICY_QA")
        self.assertIsNone(binding.confirmed_profile)
        self.assertEqual(binding.task_scope.province, "湖南")
        self.assertEqual(binding.task_scope.application_year, 2025)
        self.assertEqual(binding.task_scope.category, "不限")
        self.assertEqual(binding.task_scope.batch, "不限")
        self.assertEqual(binding.school_ids, ("2002",))
        self.assertEqual(binding.document_types, ("policy",))

        with self.assertRaises(RunLifecycleError) as raised:
            await lifecycle.GetRetrievalRunBinding(
                principal_id="principal-other",
                run_id=receipt.run_id,
            )
        self.assertEqual(raised.exception.reason, "RUN_NOT_FOUND")

    async def _wait_for_interrupt(self, lifecycle, run_id):
        for _ in range(200):
            view = await lifecycle.GetRun(
                principal_id="principal-a",
                run_id=run_id,
            )
            if (
                view.state == "WAITING_HITL"
                and view.current_interrupt is not None
            ):
                return view
            await asyncio.sleep(0.01)
        self.fail("run 未在超时内进入 interrupt")

    async def _wait_for_confirmed_profile(self, lifecycle, run_id):
        for _ in range(200):
            view = await lifecycle.GetRun(
                principal_id="principal-a",
                run_id=run_id,
            )
            if view.confirmed_profile_version is not None:
                return view
            await asyncio.sleep(0.01)
        self.fail("run 未在超时内确认画像")
