"""M5b-03：端到端 case 从 RunLifecycle 主 seam 获得最终结果。"""
from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID

from api.algo.v1 import algo_pb2
from run_lifecycle_support import RunLifecyclePostgresTest
from test_run_lifecycle_algorithm_preview import FakeAlgoClient


ROOT = Path(__file__).resolve().parents[3]
DATASET = ROOT / "data" / "golden" / "evaluation-hunan-2025-v0.2.0-draft"


class ScenarioAlgoClient(FakeAlgoClient):
    def __init__(self, scenario: str) -> None:
        super().__init__()
        self.scenario = scenario
        self.eligibility_request = None

    async def EstimateProb(self, request):
        if self.scenario != "no_reliable_data":
            return await super().EstimateProb(request)
        self.calls.append(("EstimateProb", request))
        return algo_pb2.EstimateProbReply(
            basis=algo_pb2.AuditBasis(
                user_rank=15860,
                user_percentile=0.1234,
                sample_years=[2022, 2023, 2024],
                method="weighted_hit",
                province="湖南",
                category=str(request.category),
            )
        )

    async def EvaluateEligibility(self, request):
        self.eligibility_request = request
        if self.scenario not in {
            "eligibility_rejected",
            "conditional_unknown",
        }:
            return await super().EvaluateEligibility(request)
        self.calls.append(("EvaluateEligibility", request))
        candidate = request.candidate_groups[0]
        group_status = (
            algo_pb2.GroupEligibilityStatus
            .GROUP_ELIGIBILITY_STATUS_FAIL
            if self.scenario == "eligibility_rejected"
            else algo_pb2.GroupEligibilityStatus
            .GROUP_ELIGIBILITY_STATUS_UNKNOWN
        )
        major_status = (
            algo_pb2.MajorEligibilityStatus
            .MAJOR_ELIGIBILITY_STATUS_FAIL
            if self.scenario == "eligibility_rejected"
            else algo_pb2.MajorEligibilityStatus
            .MAJOR_ELIGIBILITY_STATUS_UNKNOWN
        )
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
                    status=group_status,
                    majors=[
                        algo_pb2.MajorEligibility(
                            major_code=major.major_code,
                            status=major_status,
                        )
                        for major in candidate.majors
                    ],
                )
            ],
            coverage=[
                algo_pb2.EligibilityRuleCoverage(
                    rule_type="PHYSICAL_EXAM",
                    status=(
                        algo_pb2.RuleCoverageStatus
                        .RULE_COVERAGE_STATUS_FULL
                    ),
                    covered_data_sources=["test eligibility rules"],
                )
            ],
            evidence_bundle_id="eligibility:scenario",
            diagnostics=algo_pb2.EligibilityDiagnostics(
                evaluated_rule_count=1,
                unknown_count=(
                    1 if self.scenario == "conditional_unknown" else 0
                ),
            ),
        )


class PausingFormalPublishGuard:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def before_publish(self, run_id):
        self.entered.set()
        await self.release.wait()


class RunLifecycleCaseExecutorTests(RunLifecyclePostgresTest):
    async def test_all_end_to_end_cases_execute_through_run_lifecycle(self):
        from app.run_lifecycle import (
            CancelRunCommand,
            ConfirmProfileAnswer,
            ResumeRunCommand,
        )
        from app.run_lifecycle.worker import RunLifecycleWorker
        from eval.e2e_eval import evaluate_end_to_end
        from eval.eval_dataset import load_eval_dataset
        from eval.run_lifecycle_runner import (
            RunLifecycleCaseExecutor,
            ScriptedReportNarrativeWriter,
            build_case_outcome,
        )

        dataset = load_eval_dataset(DATASET)
        cases = {
            case["case_id"]: case
            for case in dataset.e2e_cases
        }
        await self.seed_release(
            release_id=dataset.manifest["release_id"]
        )
        await self._seed_history_scope(dataset.manifest["release_id"])
        await self.seed_consent(principal_id="eval:e2e-004")
        outcomes = {}
        self._scenario_clients = {}

        for case_id in (
            "e2e-001",
            "e2e-002",
            "e2e-003",
            "e2e-004",
            "e2e-005",
            "e2e-006",
            "e2e-007",
            "e2e-008",
        ):
            case = cases[case_id]
            lifecycle, worker = await self._start_case(
                case,
                worker_owner=f"worker-{case_id}",
            )
            try:
                outcomes[case_id] = await RunLifecycleCaseExecutor(
                    lifecycle
                ).execute(case)
            finally:
                await worker.stop()
                await lifecycle.close()

        conditional_request = (
            self._scenario_clients["e2e-004"].eligibility_request
        )
        self.assertIsNotNone(conditional_request)
        conditional_facts = next(
            fact
            for fact in conditional_request.profile_facts
            if fact.field_key == "physical_exam.color_vision"
        )
        self.assertEqual(
            conditional_facts.state,
            algo_pb2.EligibilityProfileFieldState
            .ELIGIBILITY_PROFILE_FIELD_STATE_UNKNOWN,
        )

        recovered = cases["e2e-009"]
        first_lifecycle, first_worker = await self._start_case(
            recovered,
            worker_owner="worker-e2e-009-first",
        )
        submitted = await first_lifecycle.SubmitRun(
            self._submit_command(recovered)
        )
        waiting = await self._wait_for_interrupt(
            first_lifecycle,
            principal_id="eval:e2e-009",
            run_id=submitted.run_id,
        )
        await first_worker.stop()
        await first_lifecycle.ResumeRun(
            ResumeRunCommand(
                principal_id="eval:e2e-009",
                run_id=submitted.run_id,
                interrupt_id=waiting.current_interrupt.interrupt_id,
                expected_state_version=waiting.state_version,
                profile_version=UUID(
                    waiting.current_interrupt.payload["profile_version"]
                ),
                answer=ConfirmProfileAnswer(),
                idempotency_key="e2e-009-confirm",
            )
        )
        await first_lifecycle.close()
        recovered_lifecycle, recovered_worker = await self._new_case_runtime(
            recovered,
            worker_owner="worker-e2e-009-recovered",
        )
        try:
            await recovered_worker.start()
            recovered_view = await self._wait_for_terminal(
                recovered_lifecycle,
                principal_id="eval:e2e-009",
                run_id=submitted.run_id,
            )
            outcomes["e2e-009"] = build_case_outcome(
                case_id="e2e-009",
                principal_id="eval:e2e-009",
                view=recovered_view,
            )
        finally:
            await recovered_worker.stop()
            await recovered_lifecycle.close()

        raced = cases["e2e-010"]
        guard = PausingFormalPublishGuard()
        race_lifecycle, race_worker = await self._new_case_runtime(
            raced,
            worker_owner="worker-e2e-010",
            formal_publish_guard=guard,
        )
        try:
            await race_worker.start()
            submitted = await race_lifecycle.SubmitRun(
                self._submit_command(raced)
            )
            waiting = await self._wait_for_interrupt(
                race_lifecycle,
                principal_id="eval:e2e-010",
                run_id=submitted.run_id,
            )
            await race_lifecycle.ResumeRun(
                ResumeRunCommand(
                    principal_id="eval:e2e-010",
                    run_id=submitted.run_id,
                    interrupt_id=waiting.current_interrupt.interrupt_id,
                    expected_state_version=waiting.state_version,
                    profile_version=UUID(
                        waiting.current_interrupt.payload["profile_version"]
                    ),
                    answer=ConfirmProfileAnswer(),
                    idempotency_key="e2e-010-confirm",
                )
            )
            await asyncio.wait_for(guard.entered.wait(), timeout=2)
            await race_lifecycle.CancelRun(
                CancelRunCommand(
                    principal_id="eval:e2e-010",
                    run_id=submitted.run_id,
                    idempotency_key="e2e-010-cancel",
                )
            )
            guard.release.set()
            race_view = await self._wait_for_terminal(
                race_lifecycle,
                principal_id="eval:e2e-010",
                run_id=submitted.run_id,
            )
            outcomes["e2e-010"] = build_case_outcome(
                case_id="e2e-010",
                principal_id="eval:e2e-010",
                view=race_view,
            )
        finally:
            guard.release.set()
            await race_worker.stop()
            await race_lifecycle.close()

        report = evaluate_end_to_end(dataset, outcomes)

        self.assertEqual(
            report.failed_case_ids,
            (),
            [case.first_diff for case in report.cases],
        )
        self.assertEqual(report.gate_status, "PASS")
        self.assertEqual(report.metrics.citation_coverage, 1.0)
        self.assertEqual(report.metrics.citation_accuracy, 1.0)
        self.assertEqual(report.metrics.hallucination_rate, 0.0)

    async def _seed_history_scope(self, release_id: str) -> None:
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

    async def _start_case(self, case: dict, *, worker_owner: str):
        lifecycle, worker = await self._new_case_runtime(
            case,
            worker_owner=worker_owner,
        )
        await worker.start()
        return lifecycle, worker

    async def _new_case_runtime(
        self,
        case: dict,
        *,
        worker_owner: str,
        formal_publish_guard=None,
    ):
        from app.run_lifecycle import RunLifecycle
        from app.run_lifecycle.worker import RunLifecycleWorker
        from eval.run_lifecycle_runner import (
            ScriptedReportNarrativeWriter,
        )

        algo_client = ScenarioAlgoClient(case["driver"]["scenario"])
        self._scenario_clients[case["case_id"]] = algo_client
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=False,
            worker_owner=worker_owner,
            algo_client=algo_client,
            report_narrative_writer=ScriptedReportNarrativeWriter(
                case["scripted_model_responses"]
            ),
            formal_publish_guard=formal_publish_guard,
        )
        worker = RunLifecycleWorker(lifecycle, poll_interval=0.01)
        return lifecycle, worker

    @staticmethod
    def _submit_command(case: dict, *, suffix: str = "submit"):
        from app.run_lifecycle import (
            StructuredProfileInput,
            SubmitRunCommand,
        )

        profile = case["input_profile"]
        return SubmitRunCommand(
            principal_id=f"eval:{case['case_id']}",
            session_id=None,
            task_kind="GENERATE_PLAN",
            input=StructuredProfileInput(
                province=profile["province"],
                application_year=profile["application_year"],
                category=profile["category"],
                batch=profile["batch"],
                score=profile["score"],
                subjects=tuple(profile["subjects"]),
                conditional_fields=profile.get("conditional_fields"),
            ),
            idempotency_key=f"{case['case_id']}-{suffix}",
        )

    async def _wait_for_interrupt(self, lifecycle, *, principal_id, run_id):
        for _ in range(300):
            view = await lifecycle.GetRun(
                principal_id=principal_id,
                run_id=run_id,
            )
            if view.state == "WAITING_HITL" and view.current_interrupt:
                return view
            await asyncio.sleep(0.01)
        self.fail("run did not expose an interrupt")

    async def _wait_for_terminal(self, lifecycle, *, principal_id, run_id):
        for _ in range(500):
            view = await lifecycle.GetRun(
                principal_id=principal_id,
                run_id=run_id,
            )
            if view.state in {
                "SUCCEEDED",
                "FAILED",
                "EXPIRED",
                "CANCELLED",
            }:
                return view
            await asyncio.sleep(0.01)
        self.fail("run did not reach a terminal state")

    async def test_confirmed_case_returns_the_final_formal_claims(self):
        from app.run_lifecycle import RunLifecycle
        from app.run_lifecycle.worker import RunLifecycleWorker
        from eval.e2e_eval import evaluate_end_to_end
        from eval.eval_dataset import (
            JointEvalDataset,
            load_eval_dataset,
        )
        from eval.run_lifecycle_runner import (
            RunLifecycleCaseExecutor,
            ScriptedReportNarrativeWriter,
        )

        await self.seed_release()
        dataset = load_eval_dataset(DATASET)
        case = dataset.e2e_cases[0]
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=False,
            worker_owner="worker-e2e-eval",
            algo_client=FakeAlgoClient(),
            report_narrative_writer=ScriptedReportNarrativeWriter(
                case["scripted_model_responses"]
            ),
        )
        self.addAsyncCleanup(lifecycle.close)
        worker = RunLifecycleWorker(lifecycle, poll_interval=0.01)
        await worker.start()
        self.addAsyncCleanup(worker.stop)

        outcome = await RunLifecycleCaseExecutor(lifecycle).execute(case)
        single_case_dataset = JointEvalDataset(
            manifest=dataset.manifest,
            retrieval_cases=(),
            e2e_cases=(case,),
        )
        report = evaluate_end_to_end(
            single_case_dataset,
            {case["case_id"]: outcome},
        )

        self.assertEqual(outcome.terminal_status, "SUCCEEDED")
        self.assertEqual(outcome.result_kind, "V1_FORMAL")
        self.assertIn("LLM_ASSISTED", outcome.report_text)
        self.assertIn("U001|01", outcome.candidate_keys)
        self.assertGreater(len(outcome.claims), 0)
        self.assertEqual(
            report.gate_status,
            "PASS",
            report.cases[0].first_diff,
        )
        self.assertEqual(report.metrics.citation_coverage, 1.0)
        self.assertEqual(report.metrics.hallucination_rate, 0.0)


if __name__ == "__main__":
    import unittest

    unittest.main()
