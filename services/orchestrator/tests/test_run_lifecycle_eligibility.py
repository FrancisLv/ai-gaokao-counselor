"""Ticket #22: RunLifecycle uses EvaluateEligibility as the formal eligibility gate."""
from __future__ import annotations

from uuid import UUID

from api.algo.v1 import algo_pb2

from run_lifecycle_support import RunLifecyclePostgresTest
from test_run_lifecycle_algorithm_preview import FakeAlgoClient


def _candidate(
    university_code: str,
    *,
    probability: float = 0.67,
    major_codes: tuple[str, ...] = ("M01",),
) -> algo_pb2.CandidateWithProb:
    return algo_pb2.CandidateWithProb(
        candidate=algo_pb2.Candidate(
            university_id=int(university_code.removeprefix("U") or "0"),
            university_code=university_code,
            university_name=f"{university_code}大学",
            major_group="01",
            majors=[
                algo_pb2.MajorDetail(
                    major_code=major_code,
                    major_name=f"{major_code}专业",
                    subject_req="首选物理，再选不限",
                )
                for major_code in major_codes
            ],
        ),
        prob=algo_pb2.ProbEstimate(
            probability=probability,
            method="weighted_hit",
            data_years=3,
            category="稳",
        ),
    )


def _group(
    university_code: str,
    status: algo_pb2.GroupEligibilityStatus,
    majors: list[algo_pb2.MajorEligibility],
) -> algo_pb2.GroupEligibility:
    return algo_pb2.GroupEligibility(
        group_key=f"{university_code}|01",
        status=status,
        majors=majors,
        evidence_refs=[
            algo_pb2.EligibilityEvidenceRef(
                evidence_id=f"rule:{university_code}",
                evidence_type="STRUCTURED",
                release_id="rel-legacy-v1",
                content_sha256="a" * 64,
            )
        ],
    )


class EligibilityAlgo(FakeAlgoClient):
    def __init__(self, candidates: list[algo_pb2.CandidateWithProb], reply):
        super().__init__()
        self.candidates = candidates
        self.reply = reply
        self.candidate = candidates[0]

    async def EstimateProb(self, request):
        self.calls.append(("EstimateProb", request))
        return algo_pb2.EstimateProbReply(
            stable_candidates=self.candidates,
            stable_total=len(self.candidates),
            basis=algo_pb2.AuditBasis(
                user_rank=15860,
                user_percentile=0.1234,
                sample_years=[2022, 2023, 2024],
                method="weighted_hit",
                province="湖南",
                category="物理类",
            ),
        )

    async def EvaluateEligibility(self, request):
        self.calls.append(("EvaluateEligibility", request))
        return self.reply

    async def OptimizeWishlist(self, request):
        self.calls.append(("OptimizeWishlist", request))
        return algo_pb2.OptimizeWishlistReply(
            wishlist=algo_pb2.WishlistResult(
                candidates=[
                    algo_pb2.CandidateWithScore(
                        cwp=item,
                        utility=algo_pb2.UtilityScore(score=0.8),
                        final_rank=index,
                    )
                    for index, item in enumerate(request.candidates, start=1)
                ],
                report=algo_pb2.OptimizationReport(
                    stable_count=len(request.candidates),
                    safe_guard_ok=False,
                ),
            )
        )


class EligibilityRunLifecycleTests(RunLifecyclePostgresTest):
    async def _run_preview(self, algo, suffix: str):
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
            worker_owner=f"worker-{suffix}",
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
                idempotency_key=f"{suffix}-submit",
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
                idempotency_key=f"{suffix}-confirm",
            )
        )
        completed = await self._wait_for(lifecycle, submitted.run_id, "terminal")
        return lifecycle, submitted.run_id, release_id, completed

    async def test_complete_eligibility_gates_optimization_and_exposes_formal_facts(self):
        source = _candidate("U001", major_codes=("M01", "M02"))
        reply = algo_pb2.EvaluateEligibilityReply(
            status=algo_pb2.EligibilityResultStatus.ELIGIBILITY_RESULT_STATUS_COMPLETE,
            groups=[
                _group(
                    "U001",
                    algo_pb2.GroupEligibilityStatus.GROUP_ELIGIBILITY_STATUS_PASS,
                    [
                        algo_pb2.MajorEligibility(
                            major_code="M01",
                            status=algo_pb2.MajorEligibilityStatus.MAJOR_ELIGIBILITY_STATUS_PASS,
                        ),
                        algo_pb2.MajorEligibility(
                            major_code="M02",
                            status=algo_pb2.MajorEligibilityStatus.MAJOR_ELIGIBILITY_STATUS_PASS,
                        ),
                    ],
                )
            ],
            coverage=[
                algo_pb2.EligibilityRuleCoverage(
                    rule_type="SUBJECT_REQUIREMENT",
                    status=algo_pb2.RuleCoverageStatus.RULE_COVERAGE_STATUS_FULL,
                    covered_data_sources=["2025 admissions plan"],
                )
            ],
            evidence_bundle_id="eligibility:test",
            diagnostics=algo_pb2.EligibilityDiagnostics(
                evaluated_rule_count=1,
                unknown_count=0,
            ),
        )
        algo = EligibilityAlgo([source], reply)

        _, _, release_id, completed = await self._run_preview(algo, "eligible")

        self.assertEqual(completed.state, "SUCCEEDED", completed.terminal_reason)
        self.assertEqual(
            [name for name, _ in algo.calls],
            [
                "RankConvert",
                "EstimateProb",
                "EvaluateEligibility",
                "OptimizeWishlist",
            ],
        )
        request = algo.calls[2][1]
        self.assertEqual(request.release_id, release_id)
        self.assertEqual(
            request.profile_lifecycle,
            algo_pb2.EligibilityProfileLifecycle.ELIGIBILITY_PROFILE_LIFECYCLE_CONFIRMED,
        )
        self.assertEqual(request.scope.province, "湖南")
        self.assertEqual(request.scope.application_year, 2025)
        self.assertEqual(request.candidate_groups[0].university_code, "U001")
        self.assertEqual(
            [major.major_code for major in request.candidate_groups[0].majors],
            ["M01", "M02"],
        )
        self.assertIn(
            "subjects",
            {fact.field_key for fact in request.profile_facts},
        )
        self.assertEqual(
            [item.candidate.university_code for item in algo.calls[3][1].candidates],
            ["U001"],
        )
        preview = completed.result_payload
        self.assertEqual(preview["eligibility"]["status"], 1)
        self.assertTrue(preview["eligibility_gate"]["formal_eligibility_ready"])
        self.assertEqual(
            preview["eligibility_gate"]["eligible_group_keys"],
            ["U001|01"],
        )
        self.assertFalse(preview["formal_recommendation"])
        self.assertEqual(
            preview["wishlist"]["candidates"][0]["cwp"]["prob"]["probability"],
            0.67,
        )
        self.assertNotIn(
            "FULL_ELIGIBILITY_RULES",
            {item["code"] for item in preview["unverified_conditions"]},
        )

    async def test_partial_eligibility_excludes_unknown_from_formal_and_optimization(self):
        eligible = _candidate("U001")
        unknown = _candidate("U002")
        reply = algo_pb2.EvaluateEligibilityReply(
            status=algo_pb2.EligibilityResultStatus.ELIGIBILITY_RESULT_STATUS_PARTIAL,
            groups=[
                _group(
                    "U001",
                    algo_pb2.GroupEligibilityStatus.GROUP_ELIGIBILITY_STATUS_PASS,
                    [
                        algo_pb2.MajorEligibility(
                            major_code="M01",
                            status=algo_pb2.MajorEligibilityStatus.MAJOR_ELIGIBILITY_STATUS_PASS,
                        )
                    ],
                ),
                _group(
                    "U002",
                    algo_pb2.GroupEligibilityStatus.GROUP_ELIGIBILITY_STATUS_UNKNOWN,
                    [
                        algo_pb2.MajorEligibility(
                            major_code="M01",
                            status=algo_pb2.MajorEligibilityStatus.MAJOR_ELIGIBILITY_STATUS_MISSING_REQUIRED_FACT,
                            missing_fields=["single_subject_scores.数学"],
                            unknown_reasons=[
                                "PROFILE_FACT_MISSING",
                                "RULE_COVERAGE_MISSING",
                            ],
                        )
                    ],
                ),
            ],
            diagnostics=algo_pb2.EligibilityDiagnostics(unknown_count=1),
        )
        algo = EligibilityAlgo([eligible, unknown], reply)

        _, _, _, completed = await self._run_preview(algo, "partial")

        self.assertEqual(completed.state, "SUCCEEDED", completed.terminal_reason)
        self.assertEqual(
            [item.candidate.university_code for item in algo.calls[3][1].candidates],
            ["U001"],
        )
        preview = completed.result_payload
        self.assertEqual(preview["eligibility"]["status"], 2)
        self.assertFalse(preview["eligibility_gate"]["formal_eligibility_ready"])
        self.assertEqual(
            preview["eligibility_gate"]["eligible_group_keys"],
            ["U001|01"],
        )
        self.assertEqual(
            preview["eligibility_gate"]["unknown_group_keys"],
            ["U002|01"],
        )
        self.assertNotIn("U002|01", preview["eligibility_gate"]["eligible_group_keys"])
        self.assertFalse(preview["formal_recommendation"])

    async def test_pass_partial_group_remains_eligible_with_restricted_majors(self):
        source = _candidate("U001", major_codes=("M01", "M02"))
        reply = algo_pb2.EvaluateEligibilityReply(
            status=algo_pb2.EligibilityResultStatus.ELIGIBILITY_RESULT_STATUS_COMPLETE,
            groups=[
                _group(
                    "U001",
                    algo_pb2.GroupEligibilityStatus.GROUP_ELIGIBILITY_STATUS_PASS_PARTIAL,
                    [
                        algo_pb2.MajorEligibility(
                            major_code="M01",
                            status=algo_pb2.MajorEligibilityStatus.MAJOR_ELIGIBILITY_STATUS_PASS,
                        ),
                        algo_pb2.MajorEligibility(
                            major_code="M02",
                            status=algo_pb2.MajorEligibilityStatus.MAJOR_ELIGIBILITY_STATUS_FAIL,
                            failure_rule_keys=["single_subject.math"],
                        ),
                    ],
                )
            ],
        )
        algo = EligibilityAlgo([source], reply)

        _, _, _, completed = await self._run_preview(algo, "pass-partial")

        self.assertEqual(completed.state, "SUCCEEDED", completed.terminal_reason)
        self.assertEqual(len(algo.calls[3][1].candidates), 1)
        gate = completed.result_payload["eligibility_gate"]
        self.assertTrue(gate["formal_eligibility_ready"])
        self.assertEqual(gate["eligible_group_keys"], ["U001|01"])
        self.assertEqual(gate["restricted_group_keys"], ["U001|01"])
        self.assertEqual(
            completed.result_payload["eligibility"]["groups"][0]["status"],
            2,
        )

    async def test_blocked_eligibility_release_is_terminal_and_never_optimized(self):
        source = _candidate("U001")
        reply = algo_pb2.EvaluateEligibilityReply(
            status=algo_pb2.EligibilityResultStatus.ELIGIBILITY_RESULT_STATUS_BLOCKED,
            groups=[
                _group(
                    "U001",
                    algo_pb2.GroupEligibilityStatus.GROUP_ELIGIBILITY_STATUS_UNKNOWN,
                    [
                        algo_pb2.MajorEligibility(
                            major_code="M01",
                            status=algo_pb2.MajorEligibilityStatus.MAJOR_ELIGIBILITY_STATUS_UNKNOWN,
                            unknown_reasons=["RELEASE_REVOKED"],
                        )
                    ],
                )
            ],
        )
        algo = EligibilityAlgo([source], reply)

        _, _, _, completed = await self._run_preview(algo, "blocked")

        self.assertEqual(completed.state, "FAILED")
        self.assertEqual(completed.terminal_reason, "RELEASE_REVOKED")
        self.assertIsNone(completed.result)
        self.assertNotIn("OptimizeWishlist", [name for name, _ in algo.calls])

    async def test_blocked_rule_conflict_is_machine_observable(self):
        source = _candidate("U001")
        reply = algo_pb2.EvaluateEligibilityReply(
            status=algo_pb2.EligibilityResultStatus.ELIGIBILITY_RESULT_STATUS_BLOCKED,
            groups=[
                _group(
                    "U001",
                    algo_pb2.GroupEligibilityStatus.GROUP_ELIGIBILITY_STATUS_UNKNOWN,
                    [
                        algo_pb2.MajorEligibility(
                            major_code="M01",
                            status=algo_pb2.MajorEligibilityStatus.MAJOR_ELIGIBILITY_STATUS_UNKNOWN,
                            unknown_reasons=["RULE_CONFLICT"],
                        )
                    ],
                )
            ],
        )
        algo = EligibilityAlgo([source], reply)

        lifecycle, run_id, _, completed = await self._run_preview(
            algo,
            "rule-conflict",
        )

        self.assertEqual(completed.state, "FAILED")
        self.assertEqual(completed.terminal_reason, "VALIDATION_FAILED")
        error = await self._error_payload(lifecycle, run_id)
        self.assertEqual(error["terminal_reason"], "VALIDATION_FAILED")
        self.assertIn("RULE_CONFLICT", error["message"])
        self.assertNotIn("OptimizeWishlist", [name for name, _ in algo.calls])

    async def test_eligibility_cannot_inject_an_unestimated_group(self):
        source = _candidate("U001")
        reply = algo_pb2.EvaluateEligibilityReply(
            status=algo_pb2.EligibilityResultStatus.ELIGIBILITY_RESULT_STATUS_COMPLETE,
            groups=[
                _group(
                    "U999",
                    algo_pb2.GroupEligibilityStatus.GROUP_ELIGIBILITY_STATUS_PASS,
                    [
                        algo_pb2.MajorEligibility(
                            major_code="M01",
                            status=algo_pb2.MajorEligibilityStatus.MAJOR_ELIGIBILITY_STATUS_PASS,
                        )
                    ],
                )
            ],
        )
        algo = EligibilityAlgo([source], reply)

        _, _, _, completed = await self._run_preview(algo, "injected")

        self.assertEqual(completed.state, "FAILED")
        self.assertEqual(completed.terminal_reason, "VALIDATION_FAILED")
        self.assertNotIn("OptimizeWishlist", [name for name, _ in algo.calls])

    async def test_eligibility_cannot_inject_or_hide_a_major(self):
        source = _candidate("U001", major_codes=("M01", "M02"))
        reply = algo_pb2.EvaluateEligibilityReply(
            status=algo_pb2.EligibilityResultStatus.ELIGIBILITY_RESULT_STATUS_COMPLETE,
            groups=[
                _group(
                    "U001",
                    algo_pb2.GroupEligibilityStatus.GROUP_ELIGIBILITY_STATUS_PASS,
                    [
                        algo_pb2.MajorEligibility(
                            major_code="M99",
                            status=algo_pb2.MajorEligibilityStatus.MAJOR_ELIGIBILITY_STATUS_PASS,
                        )
                    ],
                )
            ],
        )
        algo = EligibilityAlgo([source], reply)

        _, _, _, completed = await self._run_preview(algo, "injected-major")

        self.assertEqual(completed.state, "FAILED")
        self.assertEqual(completed.terminal_reason, "VALIDATION_FAILED")
        self.assertNotIn("OptimizeWishlist", [name for name, _ in algo.calls])

    async def _wait_for(self, lifecycle, run_id, target: str, *, attempts: int = 300):
        import asyncio

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
            if target == "terminal" and view.state in {
                "EXPIRED",
                "SUCCEEDED",
                "FAILED",
                "CANCELLED",
            }:
                return view
            await asyncio.sleep(0.01)
        self.fail(f"run did not reach {target}")

    async def _error_payload(self, lifecycle, run_id):
        events = lifecycle.WatchRun(
            principal_id="principal-a",
            run_id=run_id,
            after_event_seq=0,
            poll_interval=0.01,
        )
        try:
            async for event in events:
                if event.type == "ERROR":
                    return event.payload
        finally:
            await events.aclose()
        self.fail("run 缺少 ERROR 事件")

