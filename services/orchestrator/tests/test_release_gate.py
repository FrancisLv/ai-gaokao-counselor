"""M5b-04：签字数据、SLO 与预算的发布门禁。"""
from __future__ import annotations

import unittest
from dataclasses import replace


def _datasets(*, owner_status: str):
    from eval.eval_dataset import JointEvalDataset

    manifest = {
        "dataset_version": "eval-gate-v1",
        "dataset_sha256": "a" * 64,
        "release_id": "rel-gate",
        "owner_status": owner_status,
    }
    case = {
        "case_id": "e2e-001",
        "expected": {
            "result_kind": "V1_FORMAL",
            "terminal_status": "SUCCEEDED",
            "excluded_candidate_keys": [],
            "required_claim_types": ["DERIVED_FACT"],
            "forbidden_text_patterns": ["保证录取"],
            "max_provider_cost_micro_usd": 150000,
        },
    }
    return (
        JointEvalDataset(
            manifest=manifest,
            retrieval_cases=(),
            e2e_cases=(case,),
        ),
        case,
    )


def _reports(
    dataset,
    case,
    *,
    recall_at_5: float = 0.9,
    mrr: float = 0.8,
    scope_leak_rate: float = 0.0,
):
    from eval.e2e_eval import (
        EndToEndCaseOutcome,
        EndToEndClaim,
        evaluate_end_to_end,
    )
    from eval.retrieval_eval import (
        RetrievalEvaluationReport,
        RetrievalMetrics,
    )

    outcome = EndToEndCaseOutcome(
        case_id=case["case_id"],
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
    return (
        RetrievalEvaluationReport(
            dataset_version=dataset.manifest["dataset_version"],
            release_id=dataset.manifest["release_id"],
            metrics=RetrievalMetrics(
                recall_at_5=recall_at_5,
                recall_at_20=1.0,
                mrr=mrr,
                scope_leak_rate=scope_leak_rate,
                evidence_precision_at_5=0.8,
                irrelevant_evidence_rate=0.2,
            ),
            eligible_case_count=30,
            no_evidence_passed=0,
            no_evidence_failed=0,
            cases=(),
            gate_status="PASS",
            quality_gate_applied=True,
        ),
        evaluate_end_to_end(
            dataset,
            {case["case_id"]: outcome},
        ),
    )


def _identity():
    return {
        "git_sha": "b" * 40,
        "algorithm_config_version": "algo-v1",
        "model_route_version": "route-v1",
        "price_table_version": "price-v1",
        "verifier_version": "verifier-v1",
        "environment": "ci",
        "hardware_profile": "linux-x86",
        "baseline_eval_run_id": "eval_baseline",
    }


def _samples(*, release_id: str = "rel-gate"):
    from eval.release_gate import RunSloSample

    return tuple(
        RunSloSample(
            run_id=f"run-{index:03d}",
            session_id=f"session-{index:03d}",
            release_id=release_id,
            active_execution_ms=1000,
            provider_token_count=100,
            provider_cost_micro_usd=100,
            budget_violation_count=0,
            reconciliation_pending=False,
            reproducible=True,
        )
        for index in range(100)
    )


def _performance_context():
    from eval.release_gate import PerformanceContext

    return PerformanceContext(
        environment="ci",
        hardware_profile="linux-x86",
        database_data_size="fixture-small",
        model_route_version="route-v1",
        cache_state="warm",
        concurrency=1,
    )


def _baseline(
    *,
    dataset_version: str = "eval-gate-v1",
    recall_at_5: float = 0.9,
    mrr: float = 0.8,
    p99_ms: int = 1000,
):
    return {
        "dataset": {"version": dataset_version},
        "performance_context": {
            "environment": "ci",
            "hardware_profile": "linux-x86",
            "database_data_size": "fixture-small",
            "model_route_version": "route-v1",
            "cache_state": "warm",
            "concurrency": 1,
        },
        "metrics": {
            "retrieval_recall_at_5": recall_at_5,
            "retrieval_mrr": mrr,
            "run_active_execution_p99_ms": p99_ms,
        },
    }


class ReleaseGateTests(unittest.TestCase):
    def test_signed_quality_with_complete_slo_evidence_passes(self):
        from eval.release_gate import evaluate_release_gate

        dataset, case = _datasets(owner_status="SIGNED_OFF")
        retrieval, e2e = _reports(dataset, case)

        report = evaluate_release_gate(
            dataset=dataset,
            retrieval_report=retrieval,
            e2e_report=e2e,
            run_identity=_identity(),
            run_samples=_samples(),
            performance_context=_performance_context(),
            baseline=_baseline(),
        )

        self.assertEqual(report.gate_status, "PASS")
        self.assertEqual(report.failed_metrics, ())
        self.assertEqual(report.metrics["run_active_execution_p99_ms"], 1000)
        self.assertEqual(report.metrics["session_token_count_max"], 100)
        self.assertEqual(report.metrics["session_cost_micro_usd_max"], 100)

    def test_pending_signoff_cannot_produce_a_release_pass(self):
        from eval.release_gate import evaluate_release_gate

        dataset, case = _datasets(owner_status="PENDING_SIGNOFF")
        retrieval, e2e = _reports(dataset, case)

        report = evaluate_release_gate(
            dataset=dataset,
            retrieval_report=retrieval,
            e2e_report=e2e,
            run_identity=_identity(),
            run_samples=_samples(),
            performance_context=_performance_context(),
            baseline=_baseline(),
        )

        self.assertEqual(report.gate_status, "FAIL")
        self.assertIn("dataset_owner_status", report.failed_metrics)

    def test_quality_regression_over_two_points_fails_even_when_safe(self):
        from eval.release_gate import evaluate_release_gate

        dataset, case = _datasets(owner_status="SIGNED_OFF")
        retrieval, e2e = _reports(dataset, case, recall_at_5=0.87)

        report = evaluate_release_gate(
            dataset=dataset,
            retrieval_report=retrieval,
            e2e_report=e2e,
            run_identity=_identity(),
            run_samples=_samples(),
            performance_context=_performance_context(),
            baseline=_baseline(),
        )

        self.assertEqual(report.gate_status, "FAIL")
        self.assertIn("retrieval_quality_regression", report.failed_metrics)

    def test_absolute_retrieval_safety_thresholds_fail_even_without_regression(self):
        from eval.release_gate import evaluate_release_gate

        dataset, case = _datasets(owner_status="SIGNED_OFF")
        retrieval, e2e = _reports(
            dataset,
            case,
            recall_at_5=0.75,
            mrr=0.60,
            scope_leak_rate=0.1,
        )

        report = evaluate_release_gate(
            dataset=dataset,
            retrieval_report=retrieval,
            e2e_report=e2e,
            run_identity=_identity(),
            run_samples=_samples(),
            performance_context=_performance_context(),
            baseline=_baseline(recall_at_5=0.75, mrr=0.60),
        )

        self.assertEqual(report.gate_status, "FAIL")
        self.assertIn("retrieval_recall_at_5", report.failed_metrics)
        self.assertIn("retrieval_mrr", report.failed_metrics)
        self.assertIn("retrieval_scope_leak_rate", report.failed_metrics)

    def test_p99_requires_at_least_one_hundred_active_execution_samples(self):
        from eval.release_gate import evaluate_release_gate

        dataset, case = _datasets(owner_status="SIGNED_OFF")
        retrieval, e2e = _reports(dataset, case)

        report = evaluate_release_gate(
            dataset=dataset,
            retrieval_report=retrieval,
            e2e_report=e2e,
            run_identity=_identity(),
            run_samples=_samples()[:99],
            performance_context=_performance_context(),
            baseline=_baseline(),
        )

        self.assertEqual(report.gate_status, "FAIL")
        self.assertIn("run_sample_count", report.failed_metrics)

    def test_active_execution_timeout_and_p99_regression_fail(self):
        from eval.release_gate import evaluate_release_gate

        dataset, case = _datasets(owner_status="SIGNED_OFF")
        retrieval, e2e = _reports(dataset, case)
        samples = list(_samples())
        samples[-1] = replace(samples[-1], active_execution_ms=600001)
        samples[-2] = replace(samples[-2], active_execution_ms=600001)

        report = evaluate_release_gate(
            dataset=dataset,
            retrieval_report=retrieval,
            e2e_report=e2e,
            run_identity=_identity(),
            run_samples=tuple(samples),
            performance_context=_performance_context(),
            baseline=_baseline(p99_ms=540000),
        )

        self.assertEqual(report.gate_status, "FAIL")
        self.assertIn(
            "run_active_execution_timeout",
            report.failed_metrics,
        )
        self.assertIn("run_p99_regression", report.failed_metrics)

    def test_budget_overrun_and_unresolved_charge_fail_release(self):
        from eval.release_gate import evaluate_release_gate

        dataset, case = _datasets(owner_status="SIGNED_OFF")
        retrieval, e2e = _reports(dataset, case)
        samples = list(_samples())
        samples[-1] = replace(
            samples[-1],
            provider_token_count=50001,
            provider_cost_micro_usd=150001,
            budget_violation_count=1,
            reconciliation_pending=True,
        )

        report = evaluate_release_gate(
            dataset=dataset,
            retrieval_report=retrieval,
            e2e_report=e2e,
            run_identity=_identity(),
            run_samples=tuple(samples),
            performance_context=_performance_context(),
            baseline=_baseline(),
        )

        self.assertEqual(report.gate_status, "FAIL")
        self.assertIn("session_token_count", report.failed_metrics)
        self.assertIn("session_cost_micro_usd", report.failed_metrics)
        self.assertIn("budget_violation_count", report.failed_metrics)
        self.assertIn("budget_reconciliation_pending", report.failed_metrics)

    def test_p99_requires_complete_fixed_performance_context(self):
        from eval.release_gate import evaluate_release_gate

        dataset, case = _datasets(owner_status="SIGNED_OFF")
        retrieval, e2e = _reports(dataset, case)
        context = replace(
            _performance_context(),
            database_data_size="",
            model_route_version="route-old",
            cache_state="",
            concurrency=0,
        )

        report = evaluate_release_gate(
            dataset=dataset,
            retrieval_report=retrieval,
            e2e_report=e2e,
            run_identity=_identity(),
            run_samples=_samples(),
            performance_context=context,
            baseline=_baseline(),
        )

        self.assertEqual(report.gate_status, "FAIL")
        self.assertIn("performance_context", report.failed_metrics)

    def test_slo_samples_from_another_release_cannot_enter_the_gate(self):
        from eval.release_gate import evaluate_release_gate

        dataset, case = _datasets(owner_status="SIGNED_OFF")
        retrieval, e2e = _reports(dataset, case)
        samples = list(_samples())
        samples[-1] = replace(
            samples[-1],
            release_id="rel-other",
        )

        report = evaluate_release_gate(
            dataset=dataset,
            retrieval_report=retrieval,
            e2e_report=e2e,
            run_identity=_identity(),
            run_samples=tuple(samples),
            performance_context=_performance_context(),
            baseline=_baseline(),
        )

        self.assertEqual(report.gate_status, "FAIL")
        self.assertIn("run_release_id", report.failed_metrics)
        detail = next(
            item
            for item in report.failure_details
            if item["metric"] == "run_release_id"
        )
        self.assertEqual(detail["actual"], ["rel-gate", "rel-other"])
        self.assertEqual(detail["threshold"], "rel-gate")

    def test_p99_baseline_requires_the_same_fixed_performance_context(self):
        from eval.release_gate import evaluate_release_gate

        dataset, case = _datasets(owner_status="SIGNED_OFF")
        retrieval, e2e = _reports(dataset, case)
        baseline = {
            "dataset": _baseline()["dataset"],
            "metrics": _baseline()["metrics"],
        }

        report = evaluate_release_gate(
            dataset=dataset,
            retrieval_report=retrieval,
            e2e_report=e2e,
            run_identity=_identity(),
            run_samples=_samples(),
            performance_context=_performance_context(),
            baseline=baseline,
        )

        self.assertEqual(report.gate_status, "FAIL")
        self.assertIn(
            "baseline_performance_context",
            report.failed_metrics,
        )
        detail = next(
            item
            for item in report.failure_details
            if item["metric"] == "baseline_performance_context"
        )
        self.assertIsNone(detail["actual"])
        self.assertEqual(
            detail["baseline"],
            {
                "environment": "ci",
                "hardware_profile": "linux-x86",
                "database_data_size": "fixture-small",
                "model_route_version": "route-v1",
                "cache_state": "warm",
                "concurrency": 1,
            },
        )

    def test_p99_baseline_with_a_different_cache_state_cannot_be_compared(self):
        from eval.release_gate import evaluate_release_gate

        dataset, case = _datasets(owner_status="SIGNED_OFF")
        retrieval, e2e = _reports(dataset, case)
        baseline = _baseline()
        baseline["performance_context"]["cache_state"] = "cold"

        report = evaluate_release_gate(
            dataset=dataset,
            retrieval_report=retrieval,
            e2e_report=e2e,
            run_identity=_identity(),
            run_samples=_samples(),
            performance_context=_performance_context(),
            baseline=baseline,
        )

        self.assertEqual(report.gate_status, "FAIL")
        self.assertIn(
            "baseline_performance_context",
            report.failed_metrics,
        )

    def test_nonreproducible_sample_fails_release_gate(self):
        from eval.release_gate import evaluate_release_gate

        dataset, case = _datasets(owner_status="SIGNED_OFF")
        retrieval, e2e = _reports(dataset, case)
        samples = list(_samples())
        samples[-1] = replace(samples[-1], reproducible=False)

        report = evaluate_release_gate(
            dataset=dataset,
            retrieval_report=retrieval,
            e2e_report=e2e,
            run_identity=_identity(),
            run_samples=tuple(samples),
            performance_context=_performance_context(),
            baseline=_baseline(),
        )

        self.assertEqual(report.gate_status, "FAIL")
        self.assertIn("run_reproducibility_rate", report.failed_metrics)
        self.assertEqual(
            report.metrics["run_reproducibility_rate"],
            0.99,
        )

    def test_illegal_candidate_leak_is_an_absolute_release_failure(self):
        from eval.release_gate import evaluate_release_gate

        dataset, case = _datasets(owner_status="SIGNED_OFF")
        case["expected"]["excluded_candidate_keys"] = ["U001|01"]
        retrieval, e2e = _reports(dataset, case)

        report = evaluate_release_gate(
            dataset=dataset,
            retrieval_report=retrieval,
            e2e_report=e2e,
            run_identity=_identity(),
            run_samples=_samples(),
            performance_context=_performance_context(),
            baseline=_baseline(),
        )

        self.assertEqual(report.gate_status, "FAIL")
        self.assertIn(
            "illegal_candidate_leak_rate",
            report.failed_metrics,
        )
        self.assertEqual(
            report.metrics["illegal_candidate_leak_rate"],
            1.0,
        )

    def test_failed_gate_exposes_machine_readable_threshold_context(self):
        from eval.release_gate import evaluate_release_gate

        dataset, case = _datasets(owner_status="SIGNED_OFF")
        retrieval, e2e = _reports(dataset, case)

        report = evaluate_release_gate(
            dataset=dataset,
            retrieval_report=retrieval,
            e2e_report=e2e,
            run_identity=_identity(),
            run_samples=(),
            performance_context=_performance_context(),
            baseline=_baseline(),
        )

        detail = next(
            item
            for item in report.failure_details
            if item["metric"] == "run_sample_count"
        )
        self.assertEqual(detail["actual"], 0)
        self.assertEqual(detail["threshold"], 100)
        self.assertEqual(detail["baseline_eval_run_id"], "eval_baseline")

    def test_eval_run_carries_gated_metrics_and_failure_details(self):
        from eval.e2e_eval import build_eval_run

        dataset, case = _datasets(owner_status="SIGNED_OFF")
        retrieval, e2e = _reports(dataset, case)

        result = build_eval_run(
            dataset,
            retrieval,
            e2e,
            run_identity=_identity(),
            generated_at="2026-11-20T00:00:00Z",
            run_samples=_samples(),
            performance_context=_performance_context(),
            baseline=_baseline(),
        )

        self.assertEqual(result["gate_status"], "PASS")
        self.assertEqual(result["failed_metrics"], [])
        self.assertEqual(
            result["metrics"]["run_active_execution_p99_ms"],
            1000,
        )
        self.assertEqual(
            result["performance_context"]["concurrency"],
            1,
        )

    def test_case_level_quality_failure_cannot_be_hidden_by_aggregate_metrics(self):
        from dataclasses import replace

        from eval.release_gate import evaluate_release_gate

        dataset, case = _datasets(owner_status="SIGNED_OFF")
        retrieval, e2e = _reports(dataset, case)
        e2e = replace(e2e, gate_status="FAIL")

        report = evaluate_release_gate(
            dataset=dataset,
            retrieval_report=retrieval,
            e2e_report=e2e,
            run_identity=_identity(),
            run_samples=_samples(),
            performance_context=_performance_context(),
            baseline=_baseline(),
        )

        self.assertEqual(report.gate_status, "FAIL")
        self.assertIn("e2e_case_gate", report.failed_metrics)


if __name__ == "__main__":
    unittest.main()
