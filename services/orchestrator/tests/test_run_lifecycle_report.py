"""#41：确认画像后自动发布受控 V1_FORMAL 报告。"""
from __future__ import annotations

import asyncio
import unittest
from uuid import UUID

import grpc

from run_lifecycle_support import RunLifecyclePostgresTest
from test_run_lifecycle_algorithm_preview import FakeAlgoClient


class FakeReportNarrativeWriter:
    def __init__(
        self,
        *,
        overview: str,
        candidate_reasons: tuple[str, ...],
        note: str | None = None,
    ) -> None:
        self.overview = overview
        self.candidate_reasons = candidate_reasons
        self.note = note
        self.requests = []

    async def compose(self, request):
        from app.run_lifecycle.report import ReportNarrative

        self.requests.append(request)
        return ReportNarrative(
            overview=self.overview,
            candidate_reasons=self.candidate_reasons,
            note=self.note,
        )


class UnavailableReportNarrativeWriter:
    async def compose(self, request):
        from app.run_lifecycle.report import ReportNarrativeUnavailable

        raise ReportNarrativeUnavailable("PROVIDER_TIMEOUT")


class SchemaPollutingReportNarrativeWriter:
    async def compose(self, request):
        from types import SimpleNamespace

        return SimpleNamespace(
            overview="方案兼顾不同录取机会。",
            candidate_reasons=("该院校专业组可作为重点核对对象。",),
            note=None,
            evidence_refs=("ev-forged",),
        )


class RewriteOnceReportNarrativeWriter:
    def __init__(self) -> None:
        self.calls = 0

    async def compose(self, request):
        from app.run_lifecycle.report import (
            ReportNarrative,
            ReportNarrativeRewriteRequired,
        )

        self.calls += 1
        if self.calls == 1:
            raise ReportNarrativeRewriteRequired("NARRATIVE_RETRY")
        return ReportNarrative(
            overview="方案兼顾不同录取机会，请结合个人偏好谨慎复核。",
            candidate_reasons=("该院校专业组可作为重点核对对象。",),
        )


class FormalReportLifecycleTests(RunLifecyclePostgresTest):
    async def test_confirmed_profile_publishes_a_deterministic_formal_report(self):
        from app.run_lifecycle import (
            ConfirmProfileAnswer,
            ResumeRunCommand,
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )
        from app.run_lifecycle.worker import RunLifecycleWorker

        release_id = await self.seed_release()
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="worker-formal-report",
            algo_client=FakeAlgoClient(),
        )
        self.addAsyncCleanup(lifecycle.close)
        worker = RunLifecycleWorker(lifecycle, poll_interval=0.01)
        await worker.start()
        self.addAsyncCleanup(worker.stop)

        submitted = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-report",
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
                idempotency_key="formal-report-submit",
            )
        )
        waiting = await self._wait_for_interrupt(lifecycle, submitted.run_id)
        await lifecycle.ResumeRun(
            ResumeRunCommand(
                principal_id="principal-report",
                run_id=submitted.run_id,
                interrupt_id=waiting.current_interrupt.interrupt_id,
                expected_state_version=waiting.state_version,
                profile_version=UUID(
                    waiting.current_interrupt.payload["profile_version"]
                ),
                answer=ConfirmProfileAnswer(),
                idempotency_key="formal-report-confirm",
            )
        )

        completed = await self._wait_for_terminal(lifecycle, submitted.run_id)

        self.assertEqual(completed.state, "SUCCEEDED")
        self.assertEqual(completed.result.result_kind, "V1_FORMAL")
        self.assertEqual(completed.release_id, release_id)
        self.assertIsNotNone(completed.formal_result)
        self.assertEqual(
            completed.formal_result.verification_report["status"],
            "PASS",
        )
        self.assertEqual(
            completed.formal_result.report["report_version"],
            "m5b-report-v1",
        )
        self.assertEqual(
            completed.formal_result.report["generation_mode"],
            "DETERMINISTIC",
        )
        self.assertEqual(
            [
                section["section_key"]
                for section in completed.formal_result.report["sections"]
            ],
            [
                "plan",
                "candidate:U001|01",
                "qualification",
                "risk",
                "data_gaps",
            ],
        )
        candidate_section = completed.formal_result.report["sections"][1]
        self.assertIn("测试大学", candidate_section["body"])
        self.assertIn("测试专业（M01）", candidate_section["body"])
        self.assertIn("67%", candidate_section["body"])
        self.assertIn("策略分档：稳", candidate_section["body"])
        self.assertIn("招生计划数：UNKNOWN", candidate_section["body"])
        self.assertIn("2022、2023、2024", candidate_section["body"])
        self.assertTrue(candidate_section["citations"])
        self.assertTrue(
            all(
                citation["evidence_id"]
                and citation["content_sha256"]
                and citation["release_id"] == release_id
                for citation in candidate_section["citations"]
            )
        )
        major_claim_id = "claim:candidate:U001|01:major:M01:major_code"
        tuition_claim_id = "claim:candidate:U001|01:major:M01:tuition"
        strategy_claim_id = "claim:candidate:U001|01:strategy_tier"
        self.assertIn(
            major_claim_id,
            completed.formal_result.report["claim_ids"],
        )
        self.assertIn(
            tuition_claim_id,
            completed.formal_result.report["claim_ids"],
        )
        self.assertIn(
            strategy_claim_id,
            completed.formal_result.report["claim_ids"],
        )
        self.assertIn(
            "candidate:U001|01:plan_count=UNKNOWN",
            completed.formal_result.report["unverified_facts"],
        )
        claims = {
            claim["claim_id"]: claim
            for claim in completed.formal_result.verification_report["claims"]
        }
        self.assertEqual(claims[major_claim_id]["status"], "VERIFIED")
        self.assertEqual(claims[tuition_claim_id]["status"], "VERIFIED")
        self.assertEqual(claims[strategy_claim_id]["status"], "VERIFIED")
        self.assertIn("15860", completed.formal_result.report["sections"][0]["body"])
        self.assertEqual(
            completed.formal_result.disclaimer,
            "本方案仅供志愿参考，不构成录取保证。",
        )
        self.assertEqual(
            completed.formal_result.no_admission_guarantee,
            "不保证录取。",
        )
        events = lifecycle.WatchRun(
            principal_id="principal-report",
            run_id=submitted.run_id,
            after_event_seq=0,
            poll_interval=0.01,
        )
        result_event = None
        try:
            while result_event is None:
                event = await anext(events)
                if event.type == "RESULT":
                    result_event = event
        finally:
            await events.aclose()
        self.assertEqual(
            result_event.payload["formal_result"]["report"],
            completed.formal_result.report,
        )
        self.assertEqual(
            result_event.payload["formal_result"]["verification_report"],
            completed.formal_result.verification_report,
        )

        from api.orchestrator.v1 import orchestrator_pb2 as pb
        from api.orchestrator.v1 import orchestrator_pb2_grpc as pb_grpc
        from app.server import OrchestratorServicer

        server = grpc.aio.server()
        pb_grpc.add_OrchestratorServicer_to_server(
            OrchestratorServicer(lifecycle=lifecycle),
            server,
        )
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        self.addAsyncCleanup(server.stop, None)
        channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
        self.addAsyncCleanup(channel.close)
        reply = await pb_grpc.OrchestratorStub(channel).GetRun(
            pb.GetRunRequest(run_id=str(submitted.run_id)),
            metadata=(("x-principal-id", "principal-report"),),
        )
        self.assertEqual(
            reply.formal_result.report.report_version,
            "m5b-report-v1",
        )
        self.assertEqual(
            [
                section.section_key
                for section in reply.formal_result.report.sections
            ],
            [
                "plan",
                "candidate:U001|01",
                "qualification",
                "risk",
                "data_gaps",
            ],
        )
        self.assertTrue(
            reply.formal_result.report.sections[1].citations
        )
        self.assertIn(
            "candidate:U001|01:plan_count=UNKNOWN",
            reply.formal_result.report.unverified_facts,
        )

    async def test_formal_report_uses_the_confirmed_profile_scope(self):
        from app.run_lifecycle import (
            ConfirmProfileAnswer,
            RejectOrEditProfileAnswer,
            ResumeRunCommand,
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )
        from app.run_lifecycle.worker import RunLifecycleWorker

        class RequestScopeAlgo(FakeAlgoClient):
            async def EstimateProb(self, request):
                reply = await super().EstimateProb(request)
                reply.basis.category = request.category
                return reply

        release_id = await self.seed_release()
        conn = await self._connect()
        try:
            await conn.execute(
                """
                INSERT INTO release_scope (
                    release_id, province, application_year, category, batch
                )
                VALUES (%s, '湖南', 2025, '历史类', '本科批')
                ON CONFLICT DO NOTHING
                """,
                (release_id,),
            )
        finally:
            await conn.close()
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="worker-report-confirmed-scope",
            algo_client=RequestScopeAlgo(),
        )
        self.addAsyncCleanup(lifecycle.close)
        worker = RunLifecycleWorker(lifecycle, poll_interval=0.01)
        await worker.start()
        self.addAsyncCleanup(worker.stop)

        submitted = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-report",
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
                idempotency_key="formal-report-confirmed-scope-submit",
            )
        )
        waiting = await self._wait_for_interrupt(lifecycle, submitted.run_id)
        await lifecycle.ResumeRun(
            ResumeRunCommand(
                principal_id="principal-report",
                run_id=submitted.run_id,
                interrupt_id=waiting.current_interrupt.interrupt_id,
                expected_state_version=waiting.state_version,
                profile_version=UUID(
                    waiting.current_interrupt.payload["profile_version"]
                ),
                answer=RejectOrEditProfileAnswer(
                    patch={
                        "category": waiting.current_interrupt.payload["fields"][
                            "category"
                        ]
                        | {"value": "历史类"},
                        "subjects": waiting.current_interrupt.payload["fields"][
                            "subjects"
                        ]
                        | {"value": ["历史", "政治", "地理"]},
                    }
                ),
                idempotency_key="formal-report-confirmed-scope-edit",
            )
        )
        revised = await self._wait_for_interrupt(lifecycle, submitted.run_id)
        await lifecycle.ResumeRun(
            ResumeRunCommand(
                principal_id="principal-report",
                run_id=submitted.run_id,
                interrupt_id=revised.current_interrupt.interrupt_id,
                expected_state_version=revised.state_version,
                profile_version=UUID(
                    revised.current_interrupt.payload["profile_version"]
                ),
                answer=ConfirmProfileAnswer(),
                idempotency_key="formal-report-confirmed-scope-confirm",
            )
        )

        completed = await self._wait_for_terminal(lifecycle, submitted.run_id)

        self.assertEqual(completed.state, "SUCCEEDED")
        plan = completed.formal_result.report["sections"][0]
        self.assertIn("湖南历史类考生", plan["body"])
        self.assertNotIn("湖南物理类考生", plan["body"])

    async def test_controlled_narrative_is_rendered_without_changing_facts(self):
        from app.run_lifecycle import (
            ConfirmProfileAnswer,
            ResumeRunCommand,
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )
        from app.run_lifecycle.worker import RunLifecycleWorker

        writer = FakeReportNarrativeWriter(
            overview="方案兼顾不同录取机会，请结合个人偏好谨慎复核。",
            candidate_reasons=("该院校专业组可作为重点核对对象。",),
        )
        await self.seed_release()
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="worker-report-narrative",
            algo_client=FakeAlgoClient(),
            report_narrative_writer=writer,
        )
        self.addAsyncCleanup(lifecycle.close)
        worker = RunLifecycleWorker(lifecycle, poll_interval=0.01)
        await worker.start()
        self.addAsyncCleanup(worker.stop)

        submitted = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-report",
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
                idempotency_key="formal-report-narrative-submit",
            )
        )
        waiting = await self._wait_for_interrupt(lifecycle, submitted.run_id)
        await lifecycle.ResumeRun(
            ResumeRunCommand(
                principal_id="principal-report",
                run_id=submitted.run_id,
                interrupt_id=waiting.current_interrupt.interrupt_id,
                expected_state_version=waiting.state_version,
                profile_version=UUID(
                    waiting.current_interrupt.payload["profile_version"]
                ),
                answer=ConfirmProfileAnswer(),
                idempotency_key="formal-report-narrative-confirm",
            )
        )

        completed = await self._wait_for_terminal(lifecycle, submitted.run_id)

        self.assertEqual(completed.state, "SUCCEEDED")
        self.assertEqual(
            completed.formal_result.report["generation_mode"],
            "LLM_ASSISTED",
        )
        sections = {
            section["section_key"]: section
            for section in completed.formal_result.report["sections"]
        }
        self.assertEqual(
            sections["narrative"]["body"],
            "方案兼顾不同录取机会，请结合个人偏好谨慎复核。",
        )
        candidate = sections["candidate:U001|01"]["body"]
        self.assertIn("该院校专业组可作为重点核对对象。", candidate)
        self.assertIn("67%", candidate)
        self.assertIn("2022、2023、2024", candidate)
        self.assertEqual(len(writer.requests), 1)
        self.assertEqual(writer.requests[0].candidate_count, 1)

    async def test_narrative_that_rewrites_an_algorithm_fact_fails_closed(self):
        from app.run_lifecycle import (
            ConfirmProfileAnswer,
            ResumeRunCommand,
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )
        from app.run_lifecycle.worker import RunLifecycleWorker

        writer = FakeReportNarrativeWriter(
            overview="测试大学录取概率为 67%，样本年份为 2022。",
            candidate_reasons=("该院校专业组可报考。",),
        )
        await self.seed_release()
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="worker-report-narrative-violation",
            algo_client=FakeAlgoClient(),
            report_narrative_writer=writer,
        )
        self.addAsyncCleanup(lifecycle.close)
        worker = RunLifecycleWorker(lifecycle, poll_interval=0.01)
        await worker.start()
        self.addAsyncCleanup(worker.stop)

        submitted = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-report",
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
                idempotency_key="formal-report-narrative-violation-submit",
            )
        )
        waiting = await self._wait_for_interrupt(lifecycle, submitted.run_id)
        await lifecycle.ResumeRun(
            ResumeRunCommand(
                principal_id="principal-report",
                run_id=submitted.run_id,
                interrupt_id=waiting.current_interrupt.interrupt_id,
                expected_state_version=waiting.state_version,
                profile_version=UUID(
                    waiting.current_interrupt.payload["profile_version"]
                ),
                answer=ConfirmProfileAnswer(),
                idempotency_key="formal-report-narrative-violation-confirm",
            )
        )

        completed = await self._wait_for_terminal(lifecycle, submitted.run_id)

        self.assertEqual(completed.state, "FAILED")
        self.assertEqual(completed.terminal_reason, "VALIDATION_FAILED")
        self.assertIsNone(completed.result)
        self.assertIsNone(completed.formal_result)

    async def test_unavailable_report_model_marks_a_deterministic_fallback(self):
        from app.run_lifecycle import (
            ConfirmProfileAnswer,
            ResumeRunCommand,
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )
        from app.run_lifecycle.worker import RunLifecycleWorker

        await self.seed_release()
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="worker-report-narrative-fallback",
            algo_client=FakeAlgoClient(),
            report_narrative_writer=UnavailableReportNarrativeWriter(),
        )
        self.addAsyncCleanup(lifecycle.close)
        worker = RunLifecycleWorker(lifecycle, poll_interval=0.01)
        await worker.start()
        self.addAsyncCleanup(worker.stop)

        submitted = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-report",
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
                idempotency_key="formal-report-narrative-fallback-submit",
            )
        )
        waiting = await self._wait_for_interrupt(lifecycle, submitted.run_id)
        await lifecycle.ResumeRun(
            ResumeRunCommand(
                principal_id="principal-report",
                run_id=submitted.run_id,
                interrupt_id=waiting.current_interrupt.interrupt_id,
                expected_state_version=waiting.state_version,
                profile_version=UUID(
                    waiting.current_interrupt.payload["profile_version"]
                ),
                answer=ConfirmProfileAnswer(),
                idempotency_key="formal-report-narrative-fallback-confirm",
            )
        )

        completed = await self._wait_for_terminal(lifecycle, submitted.run_id)

        self.assertEqual(completed.state, "SUCCEEDED")
        self.assertEqual(
            completed.formal_result.report["generation_mode"],
            "DETERMINISTIC_FALLBACK",
        )
        risk = next(
            section
            for section in completed.formal_result.report["sections"]
            if section["section_key"] == "risk"
        )
        self.assertIn("生成质量降级", risk["body"])
        self.assertIn("67%", completed.formal_result.report["sections"][1]["body"])

    async def test_unsupported_noncritical_narrative_is_stripped(self):
        from app.run_lifecycle import (
            ConfirmProfileAnswer,
            ResumeRunCommand,
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )
        from app.run_lifecycle.worker import RunLifecycleWorker

        writer = FakeReportNarrativeWriter(
            overview="方案兼顾不同录取机会，请结合个人偏好谨慎复核。",
            candidate_reasons=("该院校专业组可作为重点核对对象。",),
            note="校园生活信息尚需进一步核对。",
        )
        await self.seed_release()
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="worker-report-narrative-strip",
            algo_client=FakeAlgoClient(),
            report_narrative_writer=writer,
        )
        self.addAsyncCleanup(lifecycle.close)
        worker = RunLifecycleWorker(lifecycle, poll_interval=0.01)
        await worker.start()
        self.addAsyncCleanup(worker.stop)

        submitted = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-report",
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
                idempotency_key="formal-report-narrative-strip-submit",
            )
        )
        waiting = await self._wait_for_interrupt(lifecycle, submitted.run_id)
        await lifecycle.ResumeRun(
            ResumeRunCommand(
                principal_id="principal-report",
                run_id=submitted.run_id,
                interrupt_id=waiting.current_interrupt.interrupt_id,
                expected_state_version=waiting.state_version,
                profile_version=UUID(
                    waiting.current_interrupt.payload["profile_version"]
                ),
                answer=ConfirmProfileAnswer(),
                idempotency_key="formal-report-narrative-strip-confirm",
            )
        )

        completed = await self._wait_for_terminal(lifecycle, submitted.run_id)

        self.assertEqual(completed.state, "SUCCEEDED")
        self.assertIn(
            "claim:report-narrative-note",
            completed.formal_result.verification_report["stripped_claim_ids"],
        )
        self.assertNotIn(
            "claim:report-narrative-note",
            completed.formal_result.report["claim_ids"],
        )
        self.assertNotIn(
            "校园生活信息尚需进一步核对。",
            str(completed.formal_result.report),
        )

    async def test_narrative_admission_guarantee_fails_closed(self):
        from app.run_lifecycle import (
            ConfirmProfileAnswer,
            ResumeRunCommand,
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )
        from app.run_lifecycle.worker import RunLifecycleWorker

        writer = FakeReportNarrativeWriter(
            overview="该方案录取稳了。",
            candidate_reasons=("该院校专业组可作为重点核对对象。",),
        )
        await self.seed_release()
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="worker-report-narrative-guarantee",
            algo_client=FakeAlgoClient(),
            report_narrative_writer=writer,
        )
        self.addAsyncCleanup(lifecycle.close)
        worker = RunLifecycleWorker(lifecycle, poll_interval=0.01)
        await worker.start()
        self.addAsyncCleanup(worker.stop)

        submitted = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-report",
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
                idempotency_key="formal-report-narrative-guarantee-submit",
            )
        )
        waiting = await self._wait_for_interrupt(lifecycle, submitted.run_id)
        await lifecycle.ResumeRun(
            ResumeRunCommand(
                principal_id="principal-report",
                run_id=submitted.run_id,
                interrupt_id=waiting.current_interrupt.interrupt_id,
                expected_state_version=waiting.state_version,
                profile_version=UUID(
                    waiting.current_interrupt.payload["profile_version"]
                ),
                answer=ConfirmProfileAnswer(),
                idempotency_key="formal-report-narrative-guarantee-confirm",
            )
        )

        completed = await self._wait_for_terminal(lifecycle, submitted.run_id)

        self.assertEqual(completed.state, "FAILED", completed.terminal_reason)
        self.assertEqual(completed.terminal_reason, "VALIDATION_FAILED")
        self.assertIsNone(completed.formal_result)

    async def test_report_model_schema_pollution_fails_closed(self):
        from app.run_lifecycle import (
            ConfirmProfileAnswer,
            ResumeRunCommand,
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )
        from app.run_lifecycle.worker import RunLifecycleWorker

        await self.seed_release()
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="worker-report-narrative-schema",
            algo_client=FakeAlgoClient(),
            report_narrative_writer=SchemaPollutingReportNarrativeWriter(),
        )
        self.addAsyncCleanup(lifecycle.close)
        worker = RunLifecycleWorker(lifecycle, poll_interval=0.01)
        await worker.start()
        self.addAsyncCleanup(worker.stop)

        submitted = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-report",
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
                idempotency_key="formal-report-narrative-schema-submit",
            )
        )
        waiting = await self._wait_for_interrupt(lifecycle, submitted.run_id)
        await lifecycle.ResumeRun(
            ResumeRunCommand(
                principal_id="principal-report",
                run_id=submitted.run_id,
                interrupt_id=waiting.current_interrupt.interrupt_id,
                expected_state_version=waiting.state_version,
                profile_version=UUID(
                    waiting.current_interrupt.payload["profile_version"]
                ),
                answer=ConfirmProfileAnswer(),
                idempotency_key="formal-report-narrative-schema-confirm",
            )
        )

        completed = await self._wait_for_terminal(lifecycle, submitted.run_id)

        self.assertEqual(completed.state, "FAILED", completed.terminal_reason)
        self.assertEqual(completed.terminal_reason, "VALIDATION_FAILED")
        self.assertIsNone(completed.formal_result)

    async def test_report_narrative_rewrites_at_most_twice(self):
        from app.run_lifecycle import (
            ConfirmProfileAnswer,
            ResumeRunCommand,
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )
        from app.run_lifecycle.worker import RunLifecycleWorker

        writer = RewriteOnceReportNarrativeWriter()
        await self.seed_release()
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="worker-report-narrative-retry",
            algo_client=FakeAlgoClient(),
            report_narrative_writer=writer,
        )
        self.addAsyncCleanup(lifecycle.close)
        worker = RunLifecycleWorker(lifecycle, poll_interval=0.01)
        await worker.start()
        self.addAsyncCleanup(worker.stop)

        submitted = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-report",
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
                idempotency_key="formal-report-narrative-retry-submit",
            )
        )
        waiting = await self._wait_for_interrupt(lifecycle, submitted.run_id)
        await lifecycle.ResumeRun(
            ResumeRunCommand(
                principal_id="principal-report",
                run_id=submitted.run_id,
                interrupt_id=waiting.current_interrupt.interrupt_id,
                expected_state_version=waiting.state_version,
                profile_version=UUID(
                    waiting.current_interrupt.payload["profile_version"]
                ),
                answer=ConfirmProfileAnswer(),
                idempotency_key="formal-report-narrative-retry-confirm",
            )
        )

        completed = await self._wait_for_terminal(lifecycle, submitted.run_id)

        self.assertEqual(completed.state, "SUCCEEDED")
        self.assertEqual(
            completed.formal_result.report["generation_mode"],
            "LLM_ASSISTED",
        )
        self.assertEqual(writer.calls, 2)

    async def test_narrative_cannot_decide_eligibility(self):
        from app.run_lifecycle import (
            ConfirmProfileAnswer,
            ResumeRunCommand,
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )
        from app.run_lifecycle.worker import RunLifecycleWorker

        writer = FakeReportNarrativeWriter(
            overview="方案兼顾不同录取机会，请结合个人偏好谨慎复核。",
            candidate_reasons=("该院校专业组可以报考。",),
        )
        await self.seed_release()
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="worker-report-narrative-eligibility",
            algo_client=FakeAlgoClient(),
            report_narrative_writer=writer,
        )
        self.addAsyncCleanup(lifecycle.close)
        worker = RunLifecycleWorker(lifecycle, poll_interval=0.01)
        await worker.start()
        self.addAsyncCleanup(worker.stop)

        submitted = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-report",
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
                idempotency_key="formal-report-narrative-eligibility-submit",
            )
        )
        waiting = await self._wait_for_interrupt(lifecycle, submitted.run_id)
        await lifecycle.ResumeRun(
            ResumeRunCommand(
                principal_id="principal-report",
                run_id=submitted.run_id,
                interrupt_id=waiting.current_interrupt.interrupt_id,
                expected_state_version=waiting.state_version,
                profile_version=UUID(
                    waiting.current_interrupt.payload["profile_version"]
                ),
                answer=ConfirmProfileAnswer(),
                idempotency_key="formal-report-narrative-eligibility-confirm",
            )
        )

        completed = await self._wait_for_terminal(lifecycle, submitted.run_id)

        self.assertEqual(completed.state, "FAILED", completed.terminal_reason)
        self.assertEqual(completed.terminal_reason, "VALIDATION_FAILED")
        self.assertIsNone(completed.formal_result)

    async def test_missing_algorithm_sample_years_fail_the_formal_report_closed(self):
        from app.run_lifecycle import (
            ConfirmProfileAnswer,
            ResumeRunCommand,
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )
        from app.run_lifecycle.worker import RunLifecycleWorker

        class MissingSampleYearsAlgo(FakeAlgoClient):
            async def EstimateProb(self, request):
                reply = await super().EstimateProb(request)
                reply.basis.ClearField("sample_years")
                return reply

        await self.seed_release()
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="worker-report-missing-years",
            algo_client=MissingSampleYearsAlgo(),
        )
        self.addAsyncCleanup(lifecycle.close)
        worker = RunLifecycleWorker(lifecycle, poll_interval=0.01)
        await worker.start()
        self.addAsyncCleanup(worker.stop)

        submitted = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-report",
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
                idempotency_key="formal-report-missing-years-submit",
            )
        )
        waiting = await self._wait_for_interrupt(lifecycle, submitted.run_id)
        await lifecycle.ResumeRun(
            ResumeRunCommand(
                principal_id="principal-report",
                run_id=submitted.run_id,
                interrupt_id=waiting.current_interrupt.interrupt_id,
                expected_state_version=waiting.state_version,
                profile_version=UUID(
                    waiting.current_interrupt.payload["profile_version"]
                ),
                answer=ConfirmProfileAnswer(),
                idempotency_key="formal-report-missing-years-confirm",
            )
        )

        completed = await self._wait_for_terminal(lifecycle, submitted.run_id)

        self.assertEqual(completed.state, "FAILED")
        self.assertEqual(completed.terminal_reason, "VALIDATION_FAILED")
        self.assertIsNone(completed.result)
        self.assertIsNone(completed.formal_result)

    async def _wait_for_interrupt(self, lifecycle, run_id):
        for _ in range(300):
            view = await lifecycle.GetRun(
                principal_id="principal-report",
                run_id=run_id,
            )
            if view.state == "WAITING_HITL" and view.current_interrupt is not None:
                return view
            await asyncio.sleep(0.01)
        self.fail("run did not expose a profile interrupt")

    async def _wait_for_terminal(self, lifecycle, run_id):
        for _ in range(300):
            view = await lifecycle.GetRun(
                principal_id="principal-report",
                run_id=run_id,
            )
            if view.state in {
                "EXPIRED",
                "SUCCEEDED",
                "FAILED",
                "CANCELLED",
            }:
                return view
            await asyncio.sleep(0.01)
        self.fail("run did not reach a terminal state")


class FormalReportWireCompatibilityTests(unittest.TestCase):
    def test_formal_report_fields_are_additive(self):
        from api.orchestrator.v1 import orchestrator_pb2 as pb

        formal_fields = {
            field.name: field.number
            for field in pb.FormalResultSummary.DESCRIPTOR.fields
        }
        self.assertEqual(formal_fields["result_version"], 1)
        self.assertEqual(formal_fields["result_kind"], 2)
        self.assertEqual(formal_fields["evidence_package_sha256"], 5)
        self.assertEqual(formal_fields["no_admission_guarantee"], 16)
        self.assertEqual(formal_fields["report"], 17)

        claim_fields = {
            field.name: field.number
            for field in pb.FormalClaimVerification.DESCRIPTOR.fields
        }
        self.assertEqual(claim_fields["criticality"], 2)
        self.assertEqual(claim_fields["deterministic"], 6)
        self.assertEqual(claim_fields["claim_type"], 7)

        report_fields = {
            field.name: field.number
            for field in pb.FormalReport.DESCRIPTOR.fields
        }
        self.assertEqual(
            report_fields,
            {
                "report_version": 1,
                "generation_mode": 2,
                "sections": 3,
                "claim_ids": 4,
                "unverified_facts": 5,
            },
        )
        section_fields = {
            field.name: field.number
            for field in pb.FormalReportSection.DESCRIPTOR.fields
        }
        self.assertEqual(section_fields["citations"], 4)
