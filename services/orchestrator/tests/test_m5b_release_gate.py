"""#49：关键事实与安全证据的 M5b 聚合发布门禁。"""
from __future__ import annotations

from dataclasses import replace
import unittest


def _quality_report(*, gate_status="PASS", failed_metrics=(), metrics=None):
    from eval.release_gate import ReleaseGateReport

    metric_values = {
        "retrieval_recall_at_5": 1.0,
        "retrieval_recall_at_20": 1.0,
        "retrieval_mrr": 1.0,
        "retrieval_scope_leak_rate": 0.0,
        "factual_claim_count": 10,
        "claim_citation_coverage": 1.0,
        "claim_citation_accuracy": 1.0,
        "claim_contradiction_rate": 0.0,
        "claim_critical_unverifiable_rate": 0.0,
        "claim_hallucination_rate": 0.0,
        "illegal_candidate_leak_rate": 0.0,
        "run_active_execution_p99_ms": 1000,
        "run_active_execution_max_ms": 1000,
        "run_sample_count": 100,
        "run_reproducibility_rate": 1.0,
        "budget_reconciliation_pending_count": 0,
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


def _dataset(owner_status="SIGNED_OFF"):
    from eval.eval_dataset import JointEvalDataset

    return JointEvalDataset(
        manifest={
            "dataset_version": "eval-v1",
            "dataset_sha256": "a" * 64,
            "release_id": "rel-v1",
            "owner_status": owner_status,
        },
        retrieval_cases=(),
        e2e_cases=(),
    )


def _evaluate(dataset, quality_report, security_evidence, *, identity=None):
    from eval.eval_run import EvalRunRecord, finalize_eval_run
    from eval.release_gate import _evaluate_authoritative_records

    identity = {
        "git_sha": "b" * 40,
        "baseline_eval_run_id": "eval_baseline",
        "environment": "ci",
        "hardware_profile": "linux-x86",
        "model_route_version": "route-v1",
        **(identity or {}),
    }
    metrics = quality_report.metrics
    claims = []
    contradiction_count = round(metrics["claim_contradiction_rate"] * 10)
    unverifiable_count = max(
        contradiction_count,
        round(metrics["claim_hallucination_rate"] * 10),
    )
    for index in range(10):
        status = (
            "CONTRADICTED"
            if index < contradiction_count
            else "UNVERIFIABLE"
            if index < unverifiable_count
            else "VERIFIED"
        )
        claims.append(
            {
                "claim_id": f"claim-{index}",
                "claim_type": "DERIVED_FACT",
                "criticality": "CRITICAL",
                "status": status,
                "checked_against": [f"evidence-{index}"],
            }
        )
    hit_count = round(metrics["retrieval_recall_at_5"] * 10)
    run_samples = [
        {
            "run_id": f"run-{index}",
            "session_id": "session-1",
            "release_id": dataset.manifest["release_id"],
            "active_execution_ms": int(
                metrics["run_active_execution_p99_ms"]
            ),
            "provider_token_count": int(metrics["session_token_count_max"]),
            "provider_cost_micro_usd": int(
                metrics["session_cost_micro_usd_max"]
            ),
            "budget_violation_count": int(
                metrics["budget_violation_count"]
            ),
            "reconciliation_pending": False,
            "reproducible": True,
        }
        for index in range(100)
    ]
    payload = finalize_eval_run(
        {
            "schema_version": "eval-run-v1",
            "generated_at": "2026-11-20T00:00:00+00:00",
            "dataset": {
                "version": dataset.manifest["dataset_version"],
                "sha256": dataset.manifest["dataset_sha256"],
                "owner_status": dataset.manifest["owner_status"],
            },
            "release_id": dataset.manifest["release_id"],
            "code_identity": {"git_sha": identity["git_sha"]},
            **{
                key: value
                for key, value in identity.items()
                if key != "git_sha"
            },
            "metrics": quality_report.metrics,
            "gate_status": quality_report.gate_status,
            "release_gate_status": quality_report.gate_status,
            "performance_context": {
                "environment": identity["environment"],
                "hardware_profile": identity["hardware_profile"],
                "database_data_size": "fixture-small",
                "model_route_version": identity["model_route_version"],
                "cache_state": "warm",
                "concurrency": 1,
            },
            "failed_metrics": list(quality_report.failed_metrics),
            "failed_case_ids": [],
            "failure_details": list(quality_report.failure_details),
            "retrieval_results": [
                {
                    "case_id": f"retrieval-{index}",
                    "status": "FOUND",
                    "hit_at_5": index < hit_count,
                    "hit_at_20": index < hit_count,
                    "reciprocal_rank": 1.0 if index < hit_count else 0.0,
                    "expected_chunk_ids": [f"chunk-{index}"],
                    "expected_doc_ids": [f"doc-{index}"],
                    "returned_evidence_count": 1,
                    "scope_leaks": [],
                }
                for index in range(10)
            ],
            "e2e_results": [
                {
                    "case_id": "e2e-1",
                    "terminal_status": "SUCCEEDED",
                    "result_kind": "V1_FORMAL",
                    "result_version": "result-1",
                    "run_id": "run-1",
                    "provider_cost_micro_usd": 100,
                    "report_text": "report",
                    "candidate_keys": ["candidate-1"],
                    "excluded_candidate_keys": [],
                    "claims": claims,
                    "passed": True,
                    "first_diff": None,
                }
            ],
            "run_samples": run_samples,
        }
    )
    record = EvalRunRecord(
        eval_run_id=payload["eval_run_id"],
        result_sha256=payload["result_sha256"],
        dataset_version=dataset.manifest["dataset_version"],
        dataset_sha256=dataset.manifest["dataset_sha256"],
        release_id=dataset.manifest["release_id"],
        git_sha=identity["git_sha"],
        gate_status=quality_report.gate_status,
        payload=payload,
    )
    return _evaluate_authoritative_records(
        dataset=dataset,
        eval_run=record,
        security_evidence=security_evidence,
    )


class M5bReleaseGateTests(unittest.TestCase):
    def test_all_quality_fact_and_security_gates_produce_a_publishable_decision(self):
        from eval.release_gate import _evaluate_authoritative_records

        dataset = _dataset()
        decision = _evaluate(
            dataset=dataset,
            quality_report=_quality_report(),
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
        from eval.release_gate import _evaluate_authoritative_records

        dataset = _dataset()
        decision = _evaluate(
            dataset=dataset,
            quality_report=_quality_report(
                gate_status="FAIL",
                failed_metrics=("session_cost_micro_usd",),
            ),
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
        from eval.release_gate import _evaluate_authoritative_records

        dataset = _dataset()
        decision = _evaluate(
            dataset=dataset,
            quality_report=_quality_report(
                metrics={"claim_contradiction_rate": 0.1},
            ),
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
            _evaluate_authoritative_records,
        )

        dataset = _dataset()
        duplicate = SecurityGateEvidence(
            gate_id="CONSENT",
            passed=False,
            actual={"failed_cases": 1},
            threshold={"failed_cases": 0},
            failed_case_ids=("consent-case",),
        )
        decision = _evaluate(
            dataset=dataset,
            quality_report=_quality_report(),
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
        from eval.release_gate import _evaluate_authoritative_records

        dataset = _dataset()
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
                decision = _evaluate(
                    dataset=dataset,
                    quality_report=_quality_report(),
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
        from eval.release_gate import _evaluate_authoritative_records

        dataset = _dataset()
        decision = _evaluate(
            dataset=dataset,
            quality_report=_quality_report(),
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
            _evaluate_authoritative_records,
        )

        dataset = _dataset()
        decision = _evaluate(
            dataset=dataset,
            quality_report=_quality_report(),
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
        from eval.release_gate import _evaluate_authoritative_records

        dataset = _dataset("PENDING_SIGNOFF")
        decision = _evaluate(
            dataset=dataset,
            quality_report=_quality_report(),
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

    def test_tampered_eval_run_content_cannot_supply_a_publishable_pass(self):
        from eval.eval_run import EvalRunRecord
        from eval.release_gate import _evaluate_authoritative_records

        dataset = _dataset()
        record = EvalRunRecord(
            eval_run_id="eval_" + "c" * 64,
            result_sha256="c" * 64,
            dataset_version="eval-v1",
            dataset_sha256="a" * 64,
            release_id="rel-v1",
            git_sha="b" * 40,
            gate_status="PASS",
            payload={
                **{
                    "schema_version": "eval-run-v1",
                    "generated_at": "2026-11-20T00:00:00+00:00",
                    "dataset": {
                        "version": "eval-v1",
                        "sha256": "a" * 64,
                    },
                    "release_id": "rel-v1",
                    "code_identity": {"git_sha": "b" * 40},
                    "metrics": {
                        **_quality_report().metrics,
                        "claim_contradiction_rate": 1.0,
                    },
                    "gate_status": "PASS",
                    "release_gate_status": "PASS",
                    "failed_metrics": [],
                    "failed_case_ids": [],
                    "failure_details": [],
                },
                "result_sha256": "c" * 64,
                "eval_run_id": "eval_" + "c" * 64,
            },
        )

        decision = _evaluate_authoritative_records(
            dataset=dataset,
            eval_run=record,
            security_evidence=_passing_security_evidence(),
        )

        self.assertEqual(decision.gate_status, "FAIL")
        self.assertIn("eval_run_integrity", decision.failed_metrics)

    def test_reordered_security_evidence_keeps_a_stable_artifact_identity(self):
        from eval.release_gate import _evaluate_authoritative_records

        dataset = _dataset()
        identity = {
            "git_sha": "b" * 40,
            "baseline_eval_run_id": "eval_baseline",
        }
        first = _evaluate(
            dataset=dataset,
            quality_report=_quality_report(),
            security_evidence=_passing_security_evidence(),
            identity=identity,
        )
        second = _evaluate(
            dataset=dataset,
            quality_report=_quality_report(),
            security_evidence=tuple(
                reversed(_passing_security_evidence())
            ),
            identity=identity,
        )

        self.assertEqual(first.artifact_sha256, second.artifact_sha256)
