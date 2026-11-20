"""M5b-03：联合端到端报告指标与 EvalRun 身份。"""
from __future__ import annotations

import unittest


class EndToEndEvaluationTests(unittest.TestCase):
    def test_final_claims_drive_citation_and_hallucination_metrics(self):
        from eval.e2e_eval import (
            EndToEndCaseOutcome,
            EndToEndClaim,
            evaluate_end_to_end,
        )
        from eval.eval_dataset import JointEvalDataset

        dataset = JointEvalDataset(
            manifest={
                "dataset_version": "eval-test-v1",
                "dataset_sha256": "a" * 64,
                "release_id": "rel-test",
                "owner_status": "SIGNED_OFF",
            },
            retrieval_cases=(),
            e2e_cases=(
                {
                    "case_id": "e2e-001",
                    "expected": {
                        "result_kind": "V1_FORMAL",
                        "terminal_status": "SUCCEEDED",
                        "excluded_candidate_keys": [],
                        "required_claim_types": ["DERIVED_FACT"],
                        "forbidden_text_patterns": ["保证录取"],
                        "max_provider_cost_micro_usd": 150000,
                    },
                },
            ),
        )
        outcome = EndToEndCaseOutcome(
            case_id="e2e-001",
            terminal_status="SUCCEEDED",
            result_kind="V1_FORMAL",
            result_version="11111111-1111-1111-1111-111111111111",
            run_id="22222222-2222-2222-2222-222222222222",
            provider_cost_micro_usd=100,
            report_text="已通过事实校验。",
            candidate_keys=("U001|01",),
            claims=(
                EndToEndClaim(
                    claim_id="rank",
                    claim_type="DERIVED_FACT",
                    criticality="CRITICAL",
                    status="VERIFIED",
                    checked_against=("ev-rank",),
                ),
                EndToEndClaim(
                    claim_id="unknown-normal",
                    claim_type="DERIVED_FACT",
                    criticality="NORMAL",
                    status="UNVERIFIABLE",
                    checked_against=(),
                ),
            ),
        )

        report = evaluate_end_to_end(dataset, {"e2e-001": outcome})

        self.assertEqual(report.gate_status, "FAIL")
        self.assertEqual(report.release_gate_status, "FAIL")
        self.assertEqual(report.metrics.citation_coverage, 0.5)
        self.assertEqual(report.metrics.citation_accuracy, 1.0)
        self.assertEqual(report.metrics.contradiction_rate, 0.0)
        self.assertEqual(report.metrics.critical_unverifiable_rate, 0.0)
        self.assertEqual(report.metrics.hallucination_rate, 0.5)
        self.assertEqual(report.failed_case_ids, ("e2e-001",))
        self.assertEqual(
            report.cases[0].first_diff["field"],
            "claim:unknown-normal",
        )

    def test_pending_signoff_cannot_produce_a_releasable_pass(self):
        from eval.e2e_eval import (
            EndToEndCaseOutcome,
            EndToEndClaim,
            evaluate_end_to_end,
        )
        from eval.eval_dataset import JointEvalDataset

        dataset = JointEvalDataset(
            manifest={
                "dataset_version": "eval-test-v1",
                "dataset_sha256": "a" * 64,
                "release_id": "rel-test",
                "owner_status": "PENDING_SIGNOFF",
            },
            retrieval_cases=(),
            e2e_cases=(
                {
                    "case_id": "e2e-001",
                    "expected": {
                        "result_kind": "V1_FORMAL",
                        "terminal_status": "SUCCEEDED",
                        "excluded_candidate_keys": [],
                        "required_claim_types": ["DERIVED_FACT"],
                        "forbidden_text_patterns": ["保证录取"],
                        "max_provider_cost_micro_usd": 150000,
                    },
                },
            ),
        )
        outcome = EndToEndCaseOutcome(
            case_id="e2e-001",
            terminal_status="SUCCEEDED",
            result_kind="V1_FORMAL",
            result_version="11111111-1111-1111-1111-111111111111",
            run_id="22222222-2222-2222-2222-222222222222",
            provider_cost_micro_usd=100,
            report_text="已通过事实校验。",
            candidate_keys=("U001|01",),
            claims=(
                EndToEndClaim(
                    claim_id="rank",
                    claim_type="DERIVED_FACT",
                    criticality="CRITICAL",
                    status="VERIFIED",
                    checked_against=("ev-rank",),
                ),
            ),
        )

        report = evaluate_end_to_end(dataset, {"e2e-001": outcome})

        self.assertEqual(report.gate_status, "PASS")
        self.assertEqual(report.release_gate_status, "BLOCKED_PENDING_SIGNOFF")
        self.assertFalse(report.quality_gate_applied)

    def test_eval_run_records_retrieval_report_metrics_and_result_identity(self):
        from eval.e2e_eval import (
            EndToEndCaseOutcome,
            EndToEndClaim,
            build_eval_run,
            evaluate_end_to_end,
        )
        from eval.eval_dataset import JointEvalDataset
        from eval.retrieval_eval import (
            RetrievalEvaluationReport,
            RetrievalMetrics,
        )

        dataset = JointEvalDataset(
            manifest={
                "dataset_version": "eval-test-v1",
                "dataset_sha256": "a" * 64,
                "release_id": "rel-test",
                "owner_status": "SIGNED_OFF",
            },
            retrieval_cases=(),
            e2e_cases=(
                {
                    "case_id": "e2e-001",
                    "expected": {
                        "result_kind": "V1_FORMAL",
                        "terminal_status": "SUCCEEDED",
                        "excluded_candidate_keys": [],
                        "required_claim_types": ["DERIVED_FACT"],
                        "forbidden_text_patterns": ["保证录取"],
                        "max_provider_cost_micro_usd": 150000,
                    },
                },
            ),
        )
        outcome = EndToEndCaseOutcome(
            case_id="e2e-001",
            terminal_status="SUCCEEDED",
            result_kind="V1_FORMAL",
            result_version="11111111-1111-1111-1111-111111111111",
            run_id="22222222-2222-2222-2222-222222222222",
            provider_cost_micro_usd=100,
            report_text="已通过事实校验。",
            candidate_keys=("U001|01",),
            claims=(
                EndToEndClaim(
                    claim_id="rank",
                    claim_type="DERIVED_FACT",
                    criticality="CRITICAL",
                    status="VERIFIED",
                    checked_against=("ev-rank",),
                ),
            ),
        )
        e2e_report = evaluate_end_to_end(dataset, {"e2e-001": outcome})
        retrieval_report = RetrievalEvaluationReport(
            dataset_version="eval-test-v1",
            release_id="rel-test",
            metrics=RetrievalMetrics(
                recall_at_5=0.9,
                recall_at_20=1.0,
                mrr=0.8,
                scope_leak_rate=0.0,
                evidence_precision_at_5=0.7,
                irrelevant_evidence_rate=0.3,
            ),
            eligible_case_count=10,
            no_evidence_passed=2,
            no_evidence_failed=0,
            cases=(),
            gate_status="PASS",
            quality_gate_applied=True,
        )
        identity = {
            "git_sha": "b" * 40,
            "algorithm_config_version": "algo-v1",
            "model_route_version": "route-v1",
            "price_table_version": "price-v1",
            "verifier_version": "verifier-v1",
            "environment": "ci",
            "hardware_profile": "linux-x86",
            "baseline_eval_run_id": "eval_baseline",
        }

        first = build_eval_run(
            dataset,
            retrieval_report,
            e2e_report,
            run_identity=identity,
            generated_at="2026-11-20T00:00:00Z",
        )
        replay = build_eval_run(
            dataset,
            retrieval_report,
            e2e_report,
            run_identity=identity,
            generated_at="2026-11-20T01:00:00Z",
        )

        self.assertEqual(first["eval_run_id"], replay["eval_run_id"])
        self.assertEqual(len(first["result_sha256"]), 64)
        self.assertEqual(first["dataset"]["version"], "eval-test-v1")
        self.assertEqual(first["metrics"]["retrieval_recall_at_5"], 0.9)
        self.assertEqual(first["metrics"]["claim_citation_coverage"], 1.0)
        self.assertEqual(first["metrics"]["claim_hallucination_rate"], 0.0)
        self.assertEqual(
            first["e2e_results"][0]["result_version"],
            "11111111-1111-1111-1111-111111111111",
        )
        self.assertEqual(first["quality_case_gate_status"], "PASS")
        self.assertEqual(first["gate_status"], "FAIL")
        self.assertEqual(first["release_gate_status"], "FAIL")
        self.assertIn("run_sample_count", first["failed_metrics"])
        self.assertIn("performance_context", first["failed_metrics"])

    def test_failed_case_exposes_actual_threshold_and_baseline_context(self):
        from eval.e2e_eval import (
            EndToEndCaseOutcome,
            build_eval_run,
            evaluate_end_to_end,
        )
        from eval.eval_dataset import JointEvalDataset
        from eval.retrieval_eval import (
            RetrievalEvaluationReport,
            RetrievalMetrics,
        )

        dataset = JointEvalDataset(
            manifest={
                "dataset_version": "eval-test-v1",
                "dataset_sha256": "a" * 64,
                "release_id": "rel-test",
                "owner_status": "SIGNED_OFF",
            },
            retrieval_cases=(),
            e2e_cases=(
                {
                    "case_id": "e2e-001",
                    "expected": {
                        "result_kind": "V1_FORMAL",
                        "terminal_status": "SUCCEEDED",
                        "excluded_candidate_keys": [],
                        "required_claim_types": [],
                        "forbidden_text_patterns": [],
                        "max_provider_cost_micro_usd": 100,
                    },
                },
            ),
        )
        e2e_report = evaluate_end_to_end(
            dataset,
            {
                "e2e-001": EndToEndCaseOutcome(
                    case_id="e2e-001",
                    terminal_status="FAILED",
                    result_kind=None,
                    result_version=None,
                    run_id="22222222-2222-2222-2222-222222222222",
                    provider_cost_micro_usd=120,
                    report_text="",
                    candidate_keys=(),
                    claims=(),
                )
            },
        )
        retrieval_report = RetrievalEvaluationReport(
            dataset_version="eval-test-v1",
            release_id="rel-test",
            metrics=RetrievalMetrics(
                recall_at_5=1.0,
                recall_at_20=1.0,
                mrr=1.0,
                scope_leak_rate=0.0,
                evidence_precision_at_5=1.0,
                irrelevant_evidence_rate=0.0,
            ),
            eligible_case_count=1,
            no_evidence_passed=0,
            no_evidence_failed=0,
            cases=(),
            gate_status="PASS",
            quality_gate_applied=True,
        )

        result = build_eval_run(
            dataset,
            retrieval_report,
            e2e_report,
            run_identity={
                "git_sha": "b" * 40,
                "algorithm_config_version": "algo-v1",
                "model_route_version": "route-v1",
                "price_table_version": "price-v1",
                "verifier_version": "verifier-v1",
                "environment": "ci",
                "hardware_profile": "linux-x86",
                "baseline_eval_run_id": "eval_baseline",
            },
            generated_at="2026-11-20T00:00:00Z",
        )

        detail = next(
            item
            for item in result["failure_details"]
            if item["case_id"] == "e2e-001"
        )
        self.assertEqual(detail["case_id"], "e2e-001")
        self.assertEqual(detail["actual"], "FAILED")
        self.assertEqual(detail["threshold"], "SUCCEEDED")
        self.assertEqual(detail["baseline_eval_run_id"], "eval_baseline")
        self.assertEqual(detail["first_diff"]["field"], "terminal_status")


if __name__ == "__main__":
    unittest.main()
