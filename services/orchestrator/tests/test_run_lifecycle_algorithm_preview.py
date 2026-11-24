"""Ticket #5/#41：确认画像后的四 RPC 与确定性 V1_FORMAL 报告。"""
from __future__ import annotations

import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from uuid import UUID

import grpc
from google.protobuf import any_pb2
from google.rpc import error_details_pb2, status_pb2
from api.algo.v1 import algo_pb2
from api.algo.v1 import algo_pb2_grpc

from run_lifecycle_support import RunLifecyclePostgresTest


class FakeAlgoClient:
    def __init__(self, *, plan_count: int | None = 120) -> None:
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
                        tuition=5000,
                        has_tuition=True,
                        duration="四年",
                        min_score=610,
                        min_rank=17000,
                        reference_year=2024,
                        plan_count=10,
                        admission_probability=0.73,
                        probability_data_years=2,
                        probability_sample_years=[2023, 2024],
                        recommended=True,
                        recommendation_rank=1,
                        selectable=True,
                        in_current_plan=True,
                    )
                ],
                school_tier="985",
                location="长沙",
                province="湖南",
                history_data=[
                    algo_pb2.AdmissionRecord(
                        year=2022,
                        group_min_score=610,
                        group_min_rank=18000,
                        total_count=300000,
                        min_percentile=0.06,
                        mapping_method="EXACT_CODE",
                        mapping_confidence=1,
                    ),
                    algo_pb2.AdmissionRecord(
                        year=2024,
                        group_min_score=620,
                        group_min_rank=16000,
                        total_count=313685,
                        min_percentile=16000 / 313685,
                        mapping_method="MAJOR_NAME_JACCARD",
                        mapping_confidence=0.75,
                    ),
                ],
                **(
                    {"plan_count": plan_count}
                    if plan_count is not None
                    else {}
                ),
                previous_plan_count=138,
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

    async def SummarizeWishlistDraft(self, request):
        self.calls.append(("SummarizeWishlistDraft", request))
        groups = list(request.groups)
        slip_rate = 1.0
        utility_values = []
        summary = algo_pb2.WishlistDraftRisk(
            group_count=len(groups),
            safe_guard_target=9,
        )
        for group in groups:
            slip_rate *= 1 - group.probability
            if group.probability >= 0.9:
                summary.safe_guard_count += 1
            if group.category == "冲":
                summary.rush_count += 1
            elif group.category == "稳":
                summary.stable_count += 1
            elif group.category == "保":
                summary.safe_tier_count += 1
            if group.HasField("utility_score"):
                utility_values.append(group.utility_score)
        if len(utility_values) == len(groups) and groups:
            summary.expected_satisfaction = (
                sum(utility_values) / len(utility_values)
            )
        summary.safe_guard_ok = (
            summary.safe_guard_count >= summary.safe_guard_target
        )
        summary.slip_rate = slip_rate
        return algo_pb2.SummarizeWishlistDraftReply(summary=summary)


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

    async def SummarizeWishlistDraft(self, request, context):
        return await self.fake.SummarizeWishlistDraft(request)


class AlgorithmPreviewLifecycleTests(RunLifecyclePostgresTest):
    async def test_initial_aggressive_plan_warns_when_rush_pool_is_exhausted(self):
        from app.run_lifecycle import (
            ConfirmProfileAnswer,
            ResumeRunCommand,
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )
        from app.run_lifecycle.worker import RunLifecycleWorker

        class ExhaustedRushAlgo(FakeAlgoClient):
            def __init__(self):
                super().__init__()
                self.candidate.prob.category = "冲"

            async def EstimateProb(self, request):
                self.calls.append(("EstimateProb", request))
                return algo_pb2.EstimateProbReply(
                    rush_candidates=[self.candidate],
                    rush_total=1,
                    basis=algo_pb2.AuditBasis(
                        user_rank=15860,
                        user_percentile=0.1234,
                        sample_years=[2022, 2023, 2024],
                        method="weighted_hit",
                        province="湖南",
                        category="物理类",
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
                            rush_count=1,
                            stable_count=0,
                            safe_count=0,
                            safe_guard_ok=False,
                        ),
                    )
                )

        await self.seed_release()
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="worker-exhausted-rush",
            algo_client=ExhaustedRushAlgo(),
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
                    risk_appetite="aggressive",
                ),
                idempotency_key="exhausted-rush-submit",
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
                idempotency_key="exhausted-rush-confirm",
            )
        )
        completed = await self._wait_for(lifecycle, submitted.run_id, "result")

        self.assertEqual(completed.state, "SUCCEEDED")
        risk = next(
            section
            for section in completed.formal_result.report["sections"]
            if section["section_key"] == "risk"
        )
        self.assertIn("当前冲档候选仅 1 个", risk["body"])
        self.assertIn("已全部纳入最终志愿表", risk["body"])

    async def test_confirmed_profile_publishes_nonempty_formal_report_from_four_algo_calls(self):
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
                    interests=("物理学",),
                    required_major_names=("物理学",),
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
        self.assertEqual(completed.result.result_kind, "V1_FORMAL")
        formal = completed.formal_result
        report = formal.report
        self.assertEqual(report["report_version"], "m5b-report-v1")
        self.assertEqual(report["generation_mode"], "DETERMINISTIC")
        self.assertEqual(formal.release_id, release_id)
        self.assertEqual(
            str(formal.confirmed_profile_version),
            str(completed.confirmed_profile_version),
        )
        sections = {
            section["section_key"]: section
            for section in report["sections"]
        }
        self.assertIn("15860", sections["plan"]["body"])
        self.assertIn("测试大学", sections["candidate:U001|01"]["body"])
        self.assertIn("67%", sections["candidate:U001|01"]["body"])
        self.assertIn("2022年：最低录取分 610 分", sections["candidate:U001|01"]["body"])
        self.assertIn("2023年：暂无数据", sections["candidate:U001|01"]["body"])
        self.assertEqual(
            [
                (
                    item["year"],
                    item["min_score"],
                    item["min_rank"],
                    item["mapping_method"],
                    item["mapping_confidence"],
                )
                for item in sections["candidate:U001|01"]["history"]
            ],
            [
                (2022, 610, 18000, "EXACT_CODE", 1.0),
                (2023, None, None, "", 0.0),
                (2024, 620, 16000, "MAJOR_NAME_JACCARD", 0.75),
            ],
        )
        self.assertIn(
            "U001|01=PASS",
            sections["qualification"]["body"],
        )
        self.assertEqual(formal.verification_report["status"], "PASS")
        self.assertEqual(
            formal.verification_report["citation_coverage"],
            1.0,
        )
        self.assertIn("不保证录取", formal.no_admission_guarantee)
        conn = await self._connect()
        try:
            artifact = await (
                await conn.execute(
                    """
                    SELECT release_id, confirmed_profile_version,
                           verifier_version, applicable_as_of,
                           algorithm_config_version, artifact_sha256,
                           artifact_json
                    FROM run_formal_gate_artifact
                    WHERE run_id = %s
                    """,
                    (submitted.run_id,),
                )
            ).fetchone()
        finally:
            await conn.close()

        self.assertEqual(artifact["release_id"], release_id)
        self.assertEqual(
            artifact["confirmed_profile_version"],
            completed.confirmed_profile_version,
        )
        self.assertEqual(
            artifact["verifier_version"],
            "evidence-verifier-v1",
        )
        self.assertEqual(
            artifact["algorithm_config_version"],
            release_id,
        )
        self.assertEqual(len(artifact["artifact_sha256"]), 64)
        self.assertEqual(
            artifact["artifact_json"]["qualification"]["status"],
            "COMPLETE",
        )
        self.assertEqual(
            artifact["artifact_json"]["qualification"]["coverage"],
            [
                {
                    "rule_type": "SUBJECT_REQUIREMENT",
                    "status": "FULL",
                }
            ],
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
        estimate_request = algo.calls[1][1]
        self.assertEqual(
            list(estimate_request.profile.interests),
            ["物理学"],
        )
        self.assertEqual(
            list(estimate_request.profile.required_major_names),
            ["物理学"],
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
            result_event.payload["formal_result"]["report"],
            report,
        )
        self.assertEqual(
            result_event.payload["result_version"],
            str(completed.result.result_version),
        )

    async def test_formal_gate_artifact_rebuild_is_stable_across_dates(self):
        from app.run_lifecycle import (
            ConfirmProfileAnswer,
            ResumeRunCommand,
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )
        from app.run_lifecycle.worker import RunLifecycleWorker

        class InterruptedPreviewAlgo(FakeAlgoClient):
            def __init__(self):
                super().__init__()
                self.optimize_entered = asyncio.Event()
                self.allow_failure = asyncio.Event()

            async def OptimizeWishlist(self, request):
                self.calls.append(("OptimizeWishlist", request))
                self.optimize_entered.set()
                await self.allow_failure.wait()
                raise RuntimeError("simulate interrupted preview")

        await self.seed_release()
        first_algo = InterruptedPreviewAlgo()
        first_lifecycle = RunLifecycle(
            self.dsn,
            worker_owner="worker-artifact-date",
            algo_client=first_algo,
            lease_duration=timedelta(seconds=1),
            clock=lambda: datetime(
                2024,
                1,
                2,
                23,
                59,
                59,
                tzinfo=timezone.utc,
            ),
        )
        self.addAsyncCleanup(first_lifecycle.close)
        worker = RunLifecycleWorker(first_lifecycle, poll_interval=0.01)
        await worker.start()

        submitted = await first_lifecycle.SubmitRun(
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
                idempotency_key="algorithm-preview-artifact-date-submit",
            )
        )
        waiting = await self._wait_for(
            first_lifecycle,
            submitted.run_id,
            "interrupt",
        )
        await first_lifecycle.ResumeRun(
            ResumeRunCommand(
                principal_id="principal-a",
                run_id=submitted.run_id,
                interrupt_id=waiting.current_interrupt.interrupt_id,
                expected_state_version=waiting.state_version,
                profile_version=UUID(
                    waiting.current_interrupt.payload["profile_version"]
                ),
                answer=ConfirmProfileAnswer(),
                idempotency_key="algorithm-preview-artifact-date-confirm",
            )
        )
        await asyncio.wait_for(first_algo.optimize_entered.wait(), timeout=5)

        conn = await self._connect()
        try:
            first_artifact = await (
                await conn.execute(
                    """
                    SELECT run.created_at, artifact.applicable_as_of,
                           artifact.artifact_sha256
                    FROM run
                    JOIN run_formal_gate_artifact artifact
                      ON artifact.run_id = run.run_id
                    WHERE run.run_id = %s
                    """,
                    (submitted.run_id,),
                )
            ).fetchone()
        finally:
            await conn.close()

        with self.assertLogs(
            "app.run_lifecycle.worker",
            level="ERROR",
        ):
            first_algo.allow_failure.set()
            await asyncio.sleep(0.05)
            await worker.stop()
        await first_lifecycle.close()

        restarted = RunLifecycle(
            self.dsn,
            worker_owner="worker-artifact-date-retry",
            algo_client=FakeAlgoClient(),
            clock=lambda: datetime(
                2024,
                1,
                3,
                0,
                0,
                1,
                tzinfo=timezone.utc,
            ),
        )
        self.addAsyncCleanup(restarted.close)
        await restarted.run_due_work_once()
        completed = await self._wait_for(
            restarted,
            submitted.run_id,
            "result",
        )

        conn = await self._connect()
        try:
            rebuilt_artifact = await (
                await conn.execute(
                    """
                    SELECT applicable_as_of, artifact_sha256
                    FROM run_formal_gate_artifact
                    WHERE run_id = %s
                    """,
                    (submitted.run_id,),
                )
            ).fetchone()
        finally:
            await conn.close()

        self.assertEqual(completed.state, "SUCCEEDED")
        self.assertEqual(
            first_artifact["applicable_as_of"],
            first_artifact["created_at"].date(),
        )
        self.assertEqual(
            rebuilt_artifact["applicable_as_of"],
            first_artifact["applicable_as_of"],
        )
        self.assertEqual(
            rebuilt_artifact["artifact_sha256"],
            first_artifact["artifact_sha256"],
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

    async def test_no_reliable_data_grpc_error_preserves_business_reason(self):
        from app.run_lifecycle import (
            ConfirmProfileAnswer,
            ResumeRunCommand,
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )
        from app.run_lifecycle.worker import RunLifecycleWorker

        class NoReliableDataAlgo(FakeAlgoClient):
            async def EstimateProb(self, request):
                self.calls.append(("EstimateProb", request))
                detail = any_pb2.Any()
                detail.Pack(
                    error_details_pb2.ErrorInfo(
                        reason="NO_RELIABLE_DATA",
                        domain="algo",
                    )
                )
                serialized = status_pb2.Status(
                    code=grpc.StatusCode.INTERNAL.value[0],
                    message="录取历史不足，无可靠概率",
                    details=[detail],
                ).SerializeToString()

                class FakeRpcError(grpc.RpcError):
                    def code(self):
                        return grpc.StatusCode.INTERNAL

                    def details(self):
                        return "录取历史不足，无可靠概率"

                    def trailing_metadata(self):
                        return ((
                            "grpc-status-details-bin",
                            serialized,
                        ),)

                raise FakeRpcError()

        await self.seed_release()
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="worker-no-reliable",
            algo_client=NoReliableDataAlgo(),
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
                    category="历史类",
                    batch="本科批",
                    score=600,
                    subjects=("历史", "政治", "地理"),
                ),
                idempotency_key="algorithm-preview-no-reliable-submit",
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
                idempotency_key="algorithm-preview-no-reliable-confirm",
            )
        )

        failed = await self._wait_for(lifecycle, submitted.run_id, "terminal")

        self.assertEqual(failed.state, "FAILED")
        self.assertEqual(failed.terminal_reason, "NO_RELIABLE_DATA")
        self.assertIsNone(failed.result)

    async def test_generated_grpc_algo_client_drives_the_formal_report_seam(self):
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
            completed.result.result_kind,
            "V1_FORMAL",
        )
        candidate_section = next(
            section
            for section in completed.formal_result.report["sections"]
            if section["section_key"] == "candidate:U001|01"
        )
        self.assertIn("U001", candidate_section["body"])

    @unittest.skipUnless(
        os.getenv("ORCHESTRATOR_ALGO_ADDR"),
        "ORCHESTRATOR_ALGO_ADDR 未设置",
    )
    async def test_real_svc_algo_contract_drives_the_formal_report_seam(self):
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
        self.assertEqual(completed.result.result_kind, "V1_FORMAL")
        self.assertEqual(completed.formal_result.release_id, release_id)
        self.assertTrue(completed.formal_result.report["sections"])
        self.assertEqual(
            completed.formal_result.verification_report["status"],
            "PASS",
        )

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
