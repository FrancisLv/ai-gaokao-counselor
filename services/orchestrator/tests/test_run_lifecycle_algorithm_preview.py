"""Ticket #5：确认画像后的四 RPC 与 ALGORITHM_PREVIEW。"""
from __future__ import annotations

import asyncio
import os
import unittest
from uuid import UUID

import grpc
from api.algo.v1 import algo_pb2
from api.algo.v1 import algo_pb2_grpc

from run_lifecycle_support import RunLifecyclePostgresTest


class FakeAlgoClient:
    def __init__(self) -> None:
        candidate = algo_pb2.CandidateWithProb(
            candidate=algo_pb2.Candidate(
                university_code="U001",
                university_name="测试大学",
                major_group="01",
                majors=[
                    algo_pb2.MajorDetail(
                        major_code="M01",
                        major_name="测试专业",
                        subject_req="首选物理，再选不限",
                    )
                ],
                school_tier="985",
                location="长沙",
                province="湖南",
            ),
            prob=algo_pb2.ProbEstimate(
                probability=0.67,
                method="weighted_hit",
                data_years=3,
                category="稳",
            ),
        )
        self.candidate = candidate
        self.calls: list[tuple[str, object]] = []

    async def RankConvert(self, request):
        self.calls.append(("RankConvert", request))
        return algo_pb2.RankConvertReply(rank=15860, percentile=0.1234)

    async def EstimateProb(self, request):
        self.calls.append(("EstimateProb", request))
        return algo_pb2.EstimateProbReply(
            stable_candidates=[self.candidate],
            stable_total=1,
            basis=algo_pb2.AuditBasis(
                user_rank=15860,
                user_percentile=0.1234,
                sample_years=[2022, 2023, 2024],
                method="weighted_hit",
                province="湖南",
                category="物理类",
            ),
        )

    async def CheckConstraints(self, request):
        self.calls.append(("CheckConstraints", request))
        return algo_pb2.CheckConstraintsReply(kept=[self.candidate])

    async def EvaluateEligibility(self, request):
        self.calls.append(("EvaluateEligibility", request))
        candidate = request.candidate_groups[0]
        return algo_pb2.EvaluateEligibilityReply(
            status=(
                algo_pb2.EligibilityResultStatus
                .ELIGIBILITY_RESULT_STATUS_COMPLETE
            ),
            groups=[
                algo_pb2.GroupEligibility(
                    group_key=(
                        f"{candidate.university_code}|"
                        f"{candidate.major_group}"
                    ),
                    status=(
                        algo_pb2.GroupEligibilityStatus
                        .GROUP_ELIGIBILITY_STATUS_PASS
                    ),
                    majors=[
                        algo_pb2.MajorEligibility(
                            major_code=major.major_code,
                            status=(
                                algo_pb2.MajorEligibilityStatus
                                .MAJOR_ELIGIBILITY_STATUS_PASS
                            ),
                        )
                        for major in candidate.majors
                    ],
                )
            ],
            coverage=[
                algo_pb2.EligibilityRuleCoverage(
                    rule_type="SUBJECT_REQUIREMENT",
                    status=(
                        algo_pb2.RuleCoverageStatus
                        .RULE_COVERAGE_STATUS_FULL
                    ),
                    covered_data_sources=["test admissions plan"],
                )
            ],
            evidence_bundle_id="eligibility:test",
            diagnostics=algo_pb2.EligibilityDiagnostics(
                evaluated_rule_count=1,
                unknown_count=0,
            ),
        )

    async def OptimizeWishlist(self, request):
        self.calls.append(("OptimizeWishlist", request))
        return algo_pb2.OptimizeWishlistReply(
            wishlist=algo_pb2.WishlistResult(
                candidates=[
                    algo_pb2.CandidateWithScore(
                        cwp=self.candidate,
                        utility=algo_pb2.UtilityScore(score=0.8),
                        final_rank=1,
                    )
                ],
                report=algo_pb2.OptimizationReport(
                    stable_count=1,
                    safe_guard_ok=False,
                ),
            )
        )


class WireFakeAlgoService(algo_pb2_grpc.AlgoServicer):
    def __init__(self) -> None:
        self.fake = FakeAlgoClient()
        self.calls = self.fake.calls

    async def RankConvert(self, request, context):
        return await self.fake.RankConvert(request)

    async def EstimateProb(self, request, context):
        return await self.fake.EstimateProb(request)

    async def CheckConstraints(self, request, context):
        return await self.fake.CheckConstraints(request)

    async def EvaluateEligibility(self, request, context):
        return await self.fake.EvaluateEligibility(request)

    async def OptimizeWishlist(self, request, context):
        return await self.fake.OptimizeWishlist(request)


class AlgorithmPreviewLifecycleTests(RunLifecyclePostgresTest):
    async def test_confirmed_profile_publishes_nonempty_preview_from_four_algo_calls(self):
        from app.run_lifecycle import (
            ConfirmProfileAnswer,
            ResumeRunCommand,
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )
        from app.run_lifecycle.worker import RunLifecycleWorker

        release_id = await self.seed_release()
        algo = FakeAlgoClient()
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="worker-preview",
            algo_client=algo,
        )
        self.addAsyncCleanup(lifecycle.close)
        worker = RunLifecycleWorker(lifecycle, poll_interval=0.01)
        await worker.start()
        self.addAsyncCleanup(worker.stop)

        submitted = await lifecycle.SubmitRun(
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
                idempotency_key="algorithm-preview-submit",
            )
        )
        waiting = await self._wait_for(lifecycle, submitted.run_id, "interrupt")
        await lifecycle.ResumeRun(
            ResumeRunCommand(
                principal_id="principal-a",
                run_id=submitted.run_id,
                interrupt_id=waiting.current_interrupt.interrupt_id,
                expected_state_version=waiting.state_version,
                profile_version=UUID(
                    waiting.current_interrupt.payload["profile_version"]
                ),
                answer=ConfirmProfileAnswer(),
                idempotency_key="algorithm-preview-confirm",
            )
        )

        completed = await self._wait_for(lifecycle, submitted.run_id, "result")

        self.assertEqual(completed.state, "SUCCEEDED")
        self.assertEqual(completed.result.result_kind, "ALGORITHM_PREVIEW")
        preview = completed.result_payload
        self.assertEqual(preview["result_kind"], "ALGORITHM_PREVIEW")
        self.assertEqual(preview["release_id"], release_id)
        self.assertEqual(
            preview["confirmed_profile_version"],
            str(completed.confirmed_profile_version),
        )
        self.assertEqual(preview["rank"], 15860)
        self.assertEqual(preview["percentile"], 0.1234)
        self.assertEqual(preview["stable_candidates"][0]["candidate"]["university_code"], "U001")
        self.assertEqual(
            preview["stable_candidates"][0]["prob"]["probability"],
            0.67,
        )
        self.assertEqual(preview["audit_basis"]["user_rank"], 15860)
        self.assertEqual(preview["wishlist"]["candidates"][0]["final_rank"], 1)
        self.assertFalse(preview["formal_recommendation"])
        self.assertIn("不得作为正式志愿方案", preview["disclaimer"])
        self.assertNotIn("保证录取", preview["risk_notice"] + preview["disclaimer"])

        unverified = {item["code"] for item in preview["unverified_conditions"]}
        self.assertTrue(
            {
                "ONLINE_RAG_EVIDENCE",
                "LLM_GATEWAY",
                "STRATEGY",
                "CRITIC",
                "FORMAL_REPORT",
            }.issubset(unverified)
        )

        self.assertEqual(
            [name for name, _ in algo.calls],
            [
                "RankConvert",
                "EstimateProb",
                "EvaluateEligibility",
                "OptimizeWishlist",
            ],
        )
        self.assertEqual(
            {request.release_id for _, request in algo.calls},
            {release_id},
        )
        evaluated_candidates = algo.calls[2][1].candidate_groups
        self.assertEqual(len(evaluated_candidates), 1)
        self.assertEqual(
            [
                (item.candidate.university_code, item.candidate.major_group)
                for item in algo.calls[3][1].candidates
            ],
            [
                (
                    evaluated_candidates[0].university_code,
                    evaluated_candidates[0].major_group,
                )
            ],
        )

        events = lifecycle.WatchRun(
            principal_id="principal-a",
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
            result_event.payload["preview"],
            completed.result_payload,
        )
        self.assertEqual(
            result_event.payload["result_version"],
            str(completed.result.result_version),
        )

    async def test_missing_audit_basis_fails_closed_without_result_event(self):
        from app.run_lifecycle import (
            ConfirmProfileAnswer,
            ResumeRunCommand,
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )
        from app.run_lifecycle.worker import RunLifecycleWorker

        class MissingBasisAlgo(FakeAlgoClient):
            async def EstimateProb(self, request):
                self.calls.append(("EstimateProb", request))
                return algo_pb2.EstimateProbReply(stable_candidates=[self.candidate])

        await self.seed_release()
        algo = MissingBasisAlgo()
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="worker-preview",
            algo_client=algo,
        )
        self.addAsyncCleanup(lifecycle.close)
        worker = RunLifecycleWorker(lifecycle, poll_interval=0.01)
        await worker.start()
        self.addAsyncCleanup(worker.stop)

        submitted = await lifecycle.SubmitRun(
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
                idempotency_key="algorithm-preview-no-basis-submit",
            )
        )
        waiting = await self._wait_for(lifecycle, submitted.run_id, "interrupt")
        await lifecycle.ResumeRun(
            ResumeRunCommand(
                principal_id="principal-a",
                run_id=submitted.run_id,
                interrupt_id=waiting.current_interrupt.interrupt_id,
                expected_state_version=waiting.state_version,
                profile_version=UUID(
                    waiting.current_interrupt.payload["profile_version"]
                ),
                answer=ConfirmProfileAnswer(),
                idempotency_key="algorithm-preview-no-basis-confirm",
            )
        )

        failed = await self._wait_for(lifecycle, submitted.run_id, "terminal")

        self.assertEqual(failed.state, "FAILED")
        self.assertEqual(failed.terminal_reason, "VALIDATION_FAILED")
        self.assertIsNone(failed.result)
        events = lifecycle.WatchRun(
            principal_id="principal-a",
            run_id=submitted.run_id,
            after_event_seq=0,
            poll_interval=0.01,
        )
        event_types = []
        try:
            while True:
                event = await anext(events)
                event_types.append(event.type)
                if event.state_version >= failed.state_version:
                    break
        finally:
            await events.aclose()
        self.assertNotIn("RESULT", event_types)

    async def test_empty_candidate_pool_fails_without_publishing_preview(self):
        from app.run_lifecycle import (
            ConfirmProfileAnswer,
            ResumeRunCommand,
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )
        from app.run_lifecycle.worker import RunLifecycleWorker

        class EmptyCandidateAlgo(FakeAlgoClient):
            async def EstimateProb(self, request):
                self.calls.append(("EstimateProb", request))
                return algo_pb2.EstimateProbReply(
                    basis=algo_pb2.AuditBasis(
                        user_rank=15860,
                        user_percentile=0.1234,
                        sample_years=[2022, 2023, 2024],
                        method="weighted_hit",
                        province="湖南",
                        category="物理类",
                    )
                )

        await self.seed_release()
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="worker-empty-preview",
            algo_client=EmptyCandidateAlgo(),
        )
        self.addAsyncCleanup(lifecycle.close)
        worker = RunLifecycleWorker(lifecycle, poll_interval=0.01)
        await worker.start()
        self.addAsyncCleanup(worker.stop)
        submitted = await lifecycle.SubmitRun(
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
                idempotency_key="algorithm-preview-empty-submit",
            )
        )
        waiting = await self._wait_for(lifecycle, submitted.run_id, "interrupt")
        await lifecycle.ResumeRun(
            ResumeRunCommand(
                principal_id="principal-a",
                run_id=submitted.run_id,
                interrupt_id=waiting.current_interrupt.interrupt_id,
                expected_state_version=waiting.state_version,
                profile_version=UUID(
                    waiting.current_interrupt.payload["profile_version"]
                ),
                answer=ConfirmProfileAnswer(),
                idempotency_key="algorithm-preview-empty-confirm",
            )
        )

        failed = await self._wait_for(lifecycle, submitted.run_id, "terminal")

        self.assertEqual(failed.state, "FAILED")
        self.assertEqual(failed.terminal_reason, "NO_RELIABLE_DATA")
        self.assertIsNone(failed.result)
        self.assertIsNone(failed.result_payload)

    async def test_generated_grpc_algo_client_drives_the_preview_seam(self):
        from app.run_lifecycle import (
            ConfirmProfileAnswer,
            ResumeRunCommand,
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )
        from app.run_lifecycle.algo import GrpcAlgoClient
        from app.run_lifecycle.worker import RunLifecycleWorker

        release_id = await self.seed_release()
        service = WireFakeAlgoService()
        server = grpc.aio.server()
        algo_pb2_grpc.add_AlgoServicer_to_server(service, server)
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        self.addAsyncCleanup(server.stop, None)
        channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
        self.addAsyncCleanup(channel.close)
        client = GrpcAlgoClient(channel=channel)

        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="worker-wire",
            algo_client=client,
        )
        self.addAsyncCleanup(lifecycle.close)
        worker = RunLifecycleWorker(lifecycle, poll_interval=0.01)
        await worker.start()
        self.addAsyncCleanup(worker.stop)
        submitted = await lifecycle.SubmitRun(
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
                idempotency_key="algorithm-preview-wire-submit",
            )
        )
        waiting = await self._wait_for(lifecycle, submitted.run_id, "interrupt")
        await lifecycle.ResumeRun(
            ResumeRunCommand(
                principal_id="principal-a",
                run_id=submitted.run_id,
                interrupt_id=waiting.current_interrupt.interrupt_id,
                expected_state_version=waiting.state_version,
                profile_version=UUID(
                    waiting.current_interrupt.payload["profile_version"]
                ),
                answer=ConfirmProfileAnswer(),
                idempotency_key="algorithm-preview-wire-confirm",
            )
        )

        completed = await self._wait_for(lifecycle, submitted.run_id, "result")

        self.assertEqual(completed.state, "SUCCEEDED")
        self.assertEqual(
            [name for name, _ in service.calls],
            [
                "RankConvert",
                "EstimateProb",
                "EvaluateEligibility",
                "OptimizeWishlist",
            ],
        )
        self.assertEqual(
            {request.release_id for _, request in service.calls},
            {release_id},
        )
        self.assertEqual(
            completed.result_payload["stable_candidates"][0]["candidate"][
                "university_code"
            ],
            "U001",
        )

    @unittest.skipUnless(
        os.getenv("ORCHESTRATOR_ALGO_ADDR"),
        "ORCHESTRATOR_ALGO_ADDR 未设置",
    )
    async def test_real_svc_algo_contract_drives_the_preview_seam(self):
        from app.run_lifecycle import (
            ConfirmProfileAnswer,
            ResumeRunCommand,
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )
        from app.run_lifecycle.algo import GrpcAlgoClient
        from app.run_lifecycle.worker import RunLifecycleWorker

        release_id = await self.seed_release()
        client = GrpcAlgoClient(address=os.environ["ORCHESTRATOR_ALGO_ADDR"])
        self.addAsyncCleanup(client.Close)
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="worker-real-algo",
            algo_client=client,
        )
        self.addAsyncCleanup(lifecycle.close)
        worker = RunLifecycleWorker(lifecycle, poll_interval=0.02)
        await worker.start()
        self.addAsyncCleanup(worker.stop)
        submitted = await lifecycle.SubmitRun(
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
                idempotency_key="algorithm-preview-real-submit",
            )
        )
        waiting = await self._wait_for(lifecycle, submitted.run_id, "interrupt")
        await lifecycle.ResumeRun(
            ResumeRunCommand(
                principal_id="principal-a",
                run_id=submitted.run_id,
                interrupt_id=waiting.current_interrupt.interrupt_id,
                expected_state_version=waiting.state_version,
                profile_version=UUID(
                    waiting.current_interrupt.payload["profile_version"]
                ),
                answer=ConfirmProfileAnswer(),
                idempotency_key="algorithm-preview-real-confirm",
            )
        )

        completed = await self._wait_for(
            lifecycle,
            submitted.run_id,
            "result",
            attempts=6000,
        )

        self.assertEqual(completed.state, "SUCCEEDED")
        self.assertEqual(completed.result_payload["release_id"], release_id)
        self.assertTrue(
            completed.result_payload["rush_candidates"]
            or completed.result_payload["stable_candidates"]
            or completed.result_payload["safe_candidates"]
        )
        self.assertTrue(completed.result_payload["wishlist"]["candidates"])

    async def _wait_for(self, lifecycle, run_id, target: str, *, attempts: int = 300):
        for _ in range(attempts):
            view = await lifecycle.GetRun(
                principal_id="principal-a",
                run_id=run_id,
            )
            if (
                target == "interrupt"
                and view.state == "WAITING_HITL"
                and view.current_interrupt is not None
            ):
                return view
            if target == "result" and view.result is not None:
                return view
            if target == "terminal" and view.state in {
                "EXPIRED",
                "SUCCEEDED",
                "FAILED",
                "CANCELLED",
            }:
                return view
            await asyncio.sleep(0.01)
        self.fail(f"run 未在超时内达到 {target}")
