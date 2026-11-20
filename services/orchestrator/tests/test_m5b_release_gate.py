"""#49：关键事实与安全证据的 M5b 聚合发布门禁。"""
from __future__ import annotations

from dataclasses import replace
import unittest


def _quality_report(*, gate_status="PASS", failed_metrics=(), metrics=None):
    from eval.release_gate import ReleaseGateReport

    metric_values = {
        "retrieval_recall_at_5": 0.9,
        "retrieval_mrr": 0.8,
        "retrieval_scope_leak_rate": 0.0,
        "factual_claim_count": 10,
        "claim_citation_coverage": 1.0,
        "claim_citation_accuracy": 1.0,
        "claim_contradiction_rate": 0.0,
        "claim_critical_unverifiable_rate": 0.0,
        "claim_hallucination_rate": 0.0,
        "run_active_execution_p99_ms": 1000,
        "session_token_count_max": 100,
        "session_cost_micro_usd_max": 100,
        "budget_violation_count": 0,
    }
    metric_values.update(metrics or {})
    return ReleaseGateReport(
        gate_status=gate_status,
        failed_metrics=tuple(failed_metrics),
        metrics=metric_values,
        failure_details=(),
    )


def _passing_security_evidence():
    from eval.release_gate import SecurityGateEvidence

    return tuple(
        SecurityGateEvidence(
            gate_id=gate_id,
            passed=True,
            actual={"failed_cases": 0},
            threshold={"failed_cases": 0},
            baseline={"eval_run_id": "eval_baseline"},
        )
        for gate_id in ("CONSENT", "DELETION", "RETENTION", "RESTORE")
    )


class M5bReleaseGateTests(unittest.TestCase):
    def test_all_quality_fact_and_security_gates_produce_a_publishable_decision(self):
        from eval.release_gate import evaluate_m5b_release_gate

        dataset = type(
            "Dataset",
            (),
            {
                "manifest": {
                    "dataset_version": "eval-v1",
                    "dataset_sha256": "a" * 64,
                    "release_id": "rel-v1",
                }
            },
        )()
        decision = evaluate_m5b_release_gate(
            dataset=dataset,
            quality_report=_quality_report(),
            run_identity={
                "git_sha": "b" * 40,
                "baseline_eval_run_id": "eval_baseline",
            },
            failed_case_ids=(),
            security_evidence=_passing_security_evidence(),
        )

        self.assertEqual(decision.gate_status, "PASS")
        self.assertEqual(decision.failed_gates, ())
        self.assertEqual(
            tuple(gate["gate_id"] for gate in decision.gates),
            (
                "QUALITY_SLO_BUDGET",
                "CRITICAL_FACTS",
                "CONSENT",
                "DELETION",
                "RETENTION",
                "RESTORE",
            ),
        )
        self.assertEqual(decision.release_id, "rel-v1")
        self.assertEqual(decision.git_sha, "b" * 40)
        self.assertEqual(len(decision.artifact_sha256), 64)
        for gate in decision.gates:
            self.assertIn("actual", gate)
            self.assertIn("threshold", gate)
            self.assertIn("baseline", gate)

    def test_budget_failure_blocks_release_without_misreporting_fact_failure(self):
        from eval.release_gate import evaluate_m5b_release_gate

        dataset = type(
            "Dataset",
            (),
            {
                "manifest": {
                    "dataset_version": "eval-v1",
                    "dataset_sha256": "a" * 64,
                    "release_id": "rel-v1",
                }
            },
        )()
        decision = evaluate_m5b_release_gate(
            dataset=dataset,
            quality_report=_quality_report(
                gate_status="FAIL",
                failed_metrics=("session_cost_micro_usd",),
            ),
            run_identity={
                "git_sha": "b" * 40,
                "baseline_eval_run_id": "eval_baseline",
            },
            failed_case_ids=(),
            security_evidence=_passing_security_evidence(),
        )

        self.assertEqual(decision.gate_status, "FAIL")
        self.assertEqual(
            decision.failed_gates,
            ("QUALITY_SLO_BUDGET",),
        )
        critical = next(
            gate
            for gate in decision.gates
            if gate["gate_id"] == "CRITICAL_FACTS"
        )
        self.assertEqual(critical["status"], "PASS")

    def test_final_fact_contradiction_blocks_even_when_quality_gate_claims_pass(self):
        from eval.release_gate import evaluate_m5b_release_gate

        dataset = type(
            "Dataset",
            (),
            {
                "manifest": {
                    "dataset_version": "eval-v1",
                    "dataset_sha256": "a" * 64,
                    "release_id": "rel-v1",
                }
            },
        )()
        decision = evaluate_m5b_release_gate(
            dataset=dataset,
            quality_report=_quality_report(
                metrics={"claim_contradiction_rate": 0.1},
            ),
            run_identity={
                "git_sha": "b" * 40,
                "baseline_eval_run_id": "eval_baseline",
            },
            failed_case_ids=("e2e-fact",),
            security_evidence=_passing_security_evidence(),
        )

        self.assertEqual(decision.gate_status, "FAIL")
        self.assertIn("CRITICAL_FACTS", decision.failed_gates)
        self.assertIn(
            "claim_contradiction_rate",
            decision.failed_metrics,
        )
        detail = next(
            item
            for item in decision.failure_details
            if item["metric"] == "claim_contradiction_rate"
        )
        self.assertEqual(detail["actual"], 0.1)
        self.assertEqual(detail["threshold"], 0.0)

    def test_duplicate_security_gate_evidence_fails_closed(self):
        from eval.release_gate import (
            SecurityGateEvidence,
            evaluate_m5b_release_gate,
        )

        dataset = type(
            "Dataset",
            (),
            {
                "manifest": {
                    "dataset_version": "eval-v1",
                    "dataset_sha256": "a" * 64,
                    "release_id": "rel-v1",
                }
            },
        )()
        duplicate = SecurityGateEvidence(
            gate_id="CONSENT",
            passed=False,
            actual={"failed_cases": 1},
            threshold={"failed_cases": 0},
            failed_case_ids=("consent-case",),
        )
        decision = evaluate_m5b_release_gate(
            dataset=dataset,
            quality_report=_quality_report(),
            run_identity={
                "git_sha": "b" * 40,
                "baseline_eval_run_id": "eval_baseline",
            },
            failed_case_ids=(),
            security_evidence=(
                *_passing_security_evidence(),
                duplicate,
            ),
        )

        self.assertEqual(decision.gate_status, "FAIL")
        self.assertIn("CONSENT", decision.failed_gates)
        self.assertIn(
            "security_gate_duplicate:CONSENT",
            decision.failed_metrics,
        )

    def test_each_security_failure_blocks_release_and_preserves_context(self):
        from eval.release_gate import evaluate_m5b_release_gate

        dataset = type(
            "Dataset",
            (),
            {
                "manifest": {
                    "dataset_version": "eval-v1",
                    "dataset_sha256": "a" * 64,
                    "release_id": "rel-v1",
                }
            },
        )()
        for gate_id in ("CONSENT", "DELETION", "RETENTION", "RESTORE"):
            with self.subTest(gate_id=gate_id):
                security = tuple(
                    replace(
                        evidence,
                        passed=False,
                        actual={"failed_cases": 1, "gate": gate_id},
                        failed_case_ids=(f"{gate_id.lower()}-case",),
                        resource_types=("PROFILE",),
                    )
                    if evidence.gate_id == gate_id
                    else evidence
                    for evidence in _passing_security_evidence()
                )
                decision = evaluate_m5b_release_gate(
                    dataset=dataset,
                    quality_report=_quality_report(),
                    run_identity={
                        "git_sha": "b" * 40,
                        "baseline_eval_run_id": "eval_baseline",
                    },
                    failed_case_ids=(),
                    security_evidence=security,
                )

                self.assertEqual(decision.gate_status, "FAIL")
                self.assertIn(gate_id, decision.failed_gates)
                self.assertIn(
                    f"security_gate:{gate_id}",
                    decision.failed_metrics,
                )
                self.assertIn(
                    f"{gate_id.lower()}-case",
                    decision.failed_case_ids,
                )
                detail = next(
                    item
                    for item in decision.failure_details
                    if item["metric"] == f"security_gate:{gate_id}"
                )
                self.assertEqual(
                    detail["failed_case_ids"],
                    [f"{gate_id.lower()}-case"],
                )
                self.assertEqual(detail["resource_types"], ["PROFILE"])

    def test_missing_security_evidence_fails_closed(self):
        from eval.release_gate import evaluate_m5b_release_gate

        dataset = type(
            "Dataset",
            (),
            {
                "manifest": {
                    "dataset_version": "eval-v1",
                    "dataset_sha256": "a" * 64,
                    "release_id": "rel-v1",
                }
            },
        )()
        decision = evaluate_m5b_release_gate(
            dataset=dataset,
            quality_report=_quality_report(),
            run_identity={
                "git_sha": "b" * 40,
                "baseline_eval_run_id": "eval_baseline",
            },
            failed_case_ids=(),
            security_evidence=tuple(
                evidence
                for evidence in _passing_security_evidence()
                if evidence.gate_id != "RESTORE"
            ),
        )

        self.assertEqual(decision.gate_status, "FAIL")
        self.assertIn("RESTORE", decision.failed_gates)
        self.assertIn(
            "security_gate_missing:RESTORE",
            decision.failed_metrics,
        )

    def test_unexpected_security_evidence_fails_closed(self):
        from eval.release_gate import (
            SecurityGateEvidence,
            evaluate_m5b_release_gate,
        )

        dataset = type(
            "Dataset",
            (),
            {
                "manifest": {
                    "dataset_version": "eval-v1",
                    "dataset_sha256": "a" * 64,
                    "release_id": "rel-v1",
                }
            },
        )()
        decision = evaluate_m5b_release_gate(
            dataset=dataset,
            quality_report=_quality_report(),
            run_identity={
                "git_sha": "b" * 40,
                "baseline_eval_run_id": "eval_baseline",
            },
            failed_case_ids=(),
            security_evidence=(
                *_passing_security_evidence(),
                SecurityGateEvidence(
                    gate_id="EVAL_SMOKE",
                    passed=True,
                    actual="PASS",
                    threshold="PASS",
                ),
            ),
        )

        self.assertEqual(decision.gate_status, "FAIL")
        self.assertIn(
            "security_gate_unknown:EVAL_SMOKE",
            decision.failed_metrics,
        )

    def test_pending_signoff_blocks_quality_gate_without_claiming_fact_failure(self):
        from eval.release_gate import evaluate_m5b_release_gate

        dataset = type(
            "Dataset",
            (),
            {
                "manifest": {
                    "dataset_version": "eval-v1",
                    "dataset_sha256": "a" * 64,
                    "release_id": "rel-v1",
                }
            },
        )()
        decision = evaluate_m5b_release_gate(
            dataset=dataset,
            quality_report=_quality_report(
                gate_status="FAIL",
                failed_metrics=("dataset_owner_status",),
            ),
            run_identity={
                "git_sha": "b" * 40,
                "baseline_eval_run_id": "eval_baseline",
            },
            failed_case_ids=(),
            security_evidence=_passing_security_evidence(),
        )

        self.assertEqual(decision.gate_status, "FAIL")
        self.assertEqual(
            decision.failed_gates,
            ("QUALITY_SLO_BUDGET",),
        )
        critical = next(
            gate
            for gate in decision.gates
            if gate["gate_id"] == "CRITICAL_FACTS"
        )
        self.assertEqual(critical["status"], "PASS")

    def test_reordered_security_evidence_keeps_a_stable_artifact_identity(self):
        from eval.release_gate import evaluate_m5b_release_gate

        dataset = type(
            "Dataset",
            (),
            {
                "manifest": {
                    "dataset_version": "eval-v1",
                    "dataset_sha256": "a" * 64,
                    "release_id": "rel-v1",
                }
            },
        )()
        identity = {
            "git_sha": "b" * 40,
            "baseline_eval_run_id": "eval_baseline",
        }
        first = evaluate_m5b_release_gate(
            dataset=dataset,
            quality_report=_quality_report(),
            run_identity=identity,
            failed_case_ids=(),
            security_evidence=_passing_security_evidence(),
        )
        second = evaluate_m5b_release_gate(
            dataset=dataset,
            quality_report=_quality_report(),
            run_identity=identity,
            failed_case_ids=(),
            security_evidence=tuple(
                reversed(_passing_security_evidence())
            ),
        )

        self.assertEqual(first.artifact_sha256, second.artifact_sha256)
