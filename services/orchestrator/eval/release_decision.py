"""聚合已持久化 EvalRun、关键事实与安全证据的发布结论。"""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
from typing import Any

from eval.eval_dataset import JointEvalDataset
from eval.eval_run import EvalRunRecord, load_eval_run, verify_eval_run
from eval.release_models import (
    ReleaseGateDecision,
    ReleaseGateReport,
    SecurityGateEvidence,
)
from eval.security_evidence import load_security_gate_evidence


_REQUIRED_SECURITY_GATE_IDS = (
    "CONSENT",
    "DELETION",
    "RETENTION",
    "RESTORE",
)
_CRITICAL_METRICS = (
    "factual_claim_count",
    "claim_citation_coverage",
    "claim_citation_accuracy",
    "claim_contradiction_rate",
    "claim_critical_unverifiable_rate",
    "claim_hallucination_rate",
)
_CRITICAL_THRESHOLDS = {
    "factual_claim_count": 1,
    "claim_citation_coverage": 1.0,
    "claim_citation_accuracy": 1.0,
    "claim_contradiction_rate": 0.0,
    "claim_critical_unverifiable_rate": 0.0,
    "claim_hallucination_rate": 0.0,
}


class ReleaseGateAuthorityError(ValueError):
    """权威 EvalRun 或安全 attestation 的身份不满足发布要求。"""


async def evaluate_m5b_release_gate(
    *,
    dsn: str,
    dataset: JointEvalDataset,
    eval_run_id: str,
    security_artifact_ids: tuple[str, ...],
    expected_git_sha: str,
) -> ReleaseGateDecision:
    """只从 PostgreSQL 权威记录构造发布结论。"""
    eval_run = await load_eval_run(dsn, eval_run_id)
    if eval_run.git_sha != expected_git_sha:
        raise ReleaseGateAuthorityError(
            "EvalRun git_sha does not match the release candidate"
        )
    security_evidence = await load_security_gate_evidence(
        dsn,
        security_artifact_ids,
    )
    if len(security_evidence) != len(_REQUIRED_SECURITY_GATE_IDS):
        raise ReleaseGateAuthorityError(
            "exactly one record per security gate is required"
        )
    for evidence in security_evidence:
        if (
            evidence.eval_run_id != eval_run.eval_run_id
            or evidence.dataset_version
            != dataset.manifest.get("dataset_version")
            or evidence.dataset_sha256
            != dataset.manifest.get("dataset_sha256")
            or evidence.release_id != dataset.manifest.get("release_id")
            or evidence.git_sha != expected_git_sha
        ):
            raise ReleaseGateAuthorityError(
                "security evidence identity does not match the EvalRun"
            )
    return _evaluate_authoritative_records(
        dataset=dataset,
        eval_run=eval_run,
        security_evidence=security_evidence,
    )


def _evaluate_authoritative_records(
    *,
    dataset: JointEvalDataset,
    eval_run: EvalRunRecord,
    security_evidence: tuple[SecurityGateEvidence, ...],
) -> ReleaseGateDecision:
    """任何事实、质量或安全失败均阻断发布，不允许跨指标抵消。"""
    authority_failed_metrics = []
    try:
        verified_eval_run = verify_eval_run(eval_run.payload)
    except ValueError:
        verified_eval_run = eval_run
        authority_failed_metrics.append("eval_run_integrity")
    payload = verified_eval_run.payload
    if verified_eval_run.dataset_version != dataset.manifest.get(
        "dataset_version"
    ):
        authority_failed_metrics.append("eval_run_dataset_version")
    if verified_eval_run.dataset_sha256 != dataset.manifest.get(
        "dataset_sha256"
    ):
        authority_failed_metrics.append("eval_run_dataset_sha256")
    if verified_eval_run.release_id != dataset.manifest.get("release_id"):
        authority_failed_metrics.append("eval_run_release_id")
    if not dataset.is_authoritative_quality:
        authority_failed_metrics.append("dataset_owner_status")
    if verified_eval_run.gate_status != "PASS":
        authority_failed_metrics.append("eval_run_gate_status")
    if payload.get("release_gate_status") != "PASS":
        authority_failed_metrics.append("release_gate_status")

    quality_report = _quality_report_from_eval_run(payload)
    if authority_failed_metrics:
        quality_report = ReleaseGateReport(
            gate_status="FAIL",
            failed_metrics=tuple(
                dict.fromkeys(
                    (
                        *quality_report.failed_metrics,
                        *authority_failed_metrics,
                    )
                )
            ),
            metrics=quality_report.metrics,
            failure_details=quality_report.failure_details,
        )
    run_identity = {
        **payload.get("code_identity", {}),
        "baseline_eval_run_id": payload.get("baseline_eval_run_id"),
        "environment": payload.get("environment"),
        "hardware_profile": payload.get("hardware_profile"),
        "model_route_version": payload.get("model_route_version"),
    }
    failed_case_ids = tuple(payload.get("failed_case_ids", ()))
    gate_counts = Counter(
        evidence.gate_id for evidence in security_evidence
    )
    evidence_by_gate: dict[str, list[SecurityGateEvidence]] = defaultdict(
        list
    )
    for evidence in security_evidence:
        evidence_by_gate[evidence.gate_id].append(evidence)

    critical_actual = {
        metric: quality_report.metrics.get(metric)
        for metric in _CRITICAL_METRICS
    }
    critical_failed_metrics = [
        metric for metric in _CRITICAL_METRICS
        if _critical_metric_failed(
            metric,
            critical_actual[metric],
            quality_report.failed_metrics,
        )
    ]
    critical_passed = (
        not critical_failed_metrics
        and "e2e_case_gate" not in quality_report.failed_metrics
        and "e2e_quality_evidence" not in quality_report.failed_metrics
    )
    quality_gate = {
        "gate_id": "QUALITY_SLO_BUDGET",
        "status": (
            "PASS" if quality_report.gate_status == "PASS" else "FAIL"
        ),
        "actual": quality_report.metrics,
        "threshold": _quality_thresholds(),
        "baseline": {
            "eval_run_id": run_identity.get("baseline_eval_run_id")
        },
        "failed_metrics": list(quality_report.failed_metrics),
    }
    critical_gate = {
        "gate_id": "CRITICAL_FACTS",
        "status": "PASS" if critical_passed else "FAIL",
        "actual": critical_actual,
        "threshold": _CRITICAL_THRESHOLDS,
        "baseline": {
            "eval_run_id": run_identity.get("baseline_eval_run_id")
        },
        "failed_metrics": list(critical_failed_metrics),
    }
    security_gates, failed_gates, failed_metrics, details, security_cases = (
        _security_gate_results(
            evidence_by_gate=evidence_by_gate,
            gate_counts=gate_counts,
            run_identity=run_identity,
        )
    )
    if quality_report.gate_status != "PASS":
        failed_gates.append("QUALITY_SLO_BUDGET")
    failed_metrics = [
        *quality_report.failed_metrics,
        *failed_metrics,
    ]
    if not critical_passed:
        failed_gates.append("CRITICAL_FACTS")
        for metric in critical_failed_metrics:
            failed_metrics.append(metric)
            details.append(
                {
                    "metric": metric,
                    "actual": critical_actual[metric],
                    "threshold": _CRITICAL_THRESHOLDS[metric],
                    "baseline": {
                        "eval_run_id": run_identity.get(
                            "baseline_eval_run_id"
                        )
                    },
                    "failed_case_ids": sorted(
                        set((*failed_case_ids, *security_cases))
                    ),
                    "resource_types": [],
                }
            )
    all_failed_case_ids = set((*failed_case_ids, *security_cases))
    gates = (quality_gate, critical_gate, *security_gates)
    payload_out = {
        "schema_version": "m5b-release-gate-v1",
        "gate_status": "FAIL" if failed_gates else "PASS",
        "failed_gates": sorted(set(failed_gates)),
        "failed_metrics": sorted(set(failed_metrics)),
        "failed_case_ids": sorted(all_failed_case_ids),
        "release_id": dataset.manifest["release_id"],
        "eval_run_id": verified_eval_run.eval_run_id,
        "dataset_version": dataset.manifest["dataset_version"],
        "dataset_sha256": dataset.manifest["dataset_sha256"],
        "git_sha": verified_eval_run.git_sha,
        "gates": list(gates),
        "failure_details": sorted(
            [*quality_report.failure_details, *details],
            key=lambda item: (
                str(item.get("metric") or item.get("gate_id") or ""),
                str(item.get("baseline_eval_run_id") or ""),
            ),
        ),
    }
    canonical = json.dumps(
        payload_out,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return ReleaseGateDecision(
        schema_version=payload_out["schema_version"],
        gate_status=payload_out["gate_status"],
        failed_gates=tuple(payload_out["failed_gates"]),
        failed_metrics=tuple(payload_out["failed_metrics"]),
        failed_case_ids=tuple(payload_out["failed_case_ids"]),
        release_id=payload_out["release_id"],
        eval_run_id=payload_out["eval_run_id"],
        dataset_version=payload_out["dataset_version"],
        dataset_sha256=payload_out["dataset_sha256"],
        git_sha=payload_out["git_sha"],
        gates=tuple(payload_out["gates"]),
        failure_details=tuple(payload_out["failure_details"]),
        artifact_sha256=hashlib.sha256(canonical).hexdigest(),
    )


def _quality_report_from_eval_run(payload: dict) -> ReleaseGateReport:
    metrics = payload.get("metrics")
    failed_metrics = payload.get("failed_metrics")
    failure_details = payload.get("failure_details")
    if not isinstance(metrics, dict):
        metrics = {}
        failed_metrics = (*tuple(failed_metrics or ()), "eval_run_metrics")
    return ReleaseGateReport(
        gate_status=(
            payload.get("gate_status")
            if payload.get("gate_status") in {"PASS", "FAIL"}
            else "FAIL"
        ),
        failed_metrics=tuple(failed_metrics or ()),
        metrics=metrics,
        failure_details=tuple(
            item
            for item in (failure_details or ())
            if isinstance(item, dict)
        ),
    )


def _critical_metric_failed(
    metric: str,
    actual: Any,
    failed_metrics: tuple[str, ...],
) -> bool:
    if metric in failed_metrics:
        return True
    if metric == "factual_claim_count":
        return (
            isinstance(actual, bool)
            or not isinstance(actual, int)
            or actual < 1
        )
    if isinstance(actual, bool) or not isinstance(actual, (int, float)):
        return True
    if metric in {
        "claim_citation_coverage",
        "claim_citation_accuracy",
    }:
        return actual < _CRITICAL_THRESHOLDS[metric]
    return actual > _CRITICAL_THRESHOLDS[metric]


def _quality_thresholds() -> dict[str, float | int]:
    return {
        "recall_at_5_min": 0.80,
        "mrr_min": 0.65,
        "scope_leak_rate_max": 0.0,
        "p99_sample_count_min": 100,
        "active_execution_max_ms": 600_000,
        "p99_regression_max_ratio": 0.10,
        "session_token_count_max": 50_000,
        "session_cost_micro_usd_max": 150_000,
        "budget_violation_count_max": 0,
    }


def _security_gate_results(
    *,
    evidence_by_gate,
    gate_counts,
    run_identity,
) -> tuple[list[dict], list[str], list[str], list[dict], set[str]]:
    gates = []
    failed_gates = []
    failed_metrics = []
    details = []
    failed_cases = set()
    for gate_id in _REQUIRED_SECURITY_GATE_IDS:
        records = evidence_by_gate.get(gate_id, [])
        if len(records) > 1:
            cases = tuple(
                sorted(
                    {
                        case_id
                        for item in records
                        for case_id in item.failed_case_ids
                    }
                )
            )
            resources = tuple(
                sorted(
                    {
                        resource
                        for item in records
                        for resource in item.resource_types
                    }
                )
            )
            failed_gates.append(gate_id)
            failed_metrics.append(f"security_gate_duplicate:{gate_id}")
            failed_cases.update(cases)
            details.append(
                {
                    "metric": f"security_gate_duplicate:{gate_id}",
                    "actual": [item.actual for item in records],
                    "threshold": "exactly one evidence record",
                    "baseline": [item.baseline for item in records],
                    "failed_case_ids": list(cases),
                    "resource_types": list(resources),
                }
            )
            gates.append(
                {
                    "gate_id": gate_id,
                    "status": "FAIL",
                    "actual": [item.actual for item in records],
                    "threshold": "exactly one evidence record",
                    "baseline": [item.baseline for item in records],
                    "failed_case_ids": list(cases),
                    "resource_types": list(resources),
                }
            )
            continue
        if not records:
            failed_gates.append(gate_id)
            failed_metrics.append(f"security_gate_missing:{gate_id}")
            gates.append(
                {
                    "gate_id": gate_id,
                    "status": "FAIL",
                    "actual": "MISSING",
                    "threshold": "required",
                    "baseline": {
                        "eval_run_id": run_identity.get(
                            "baseline_eval_run_id"
                        )
                    },
                    "failed_case_ids": [],
                    "resource_types": [],
                }
            )
            continue
        evidence = records[0]
        failed_cases.update(evidence.failed_case_ids)
        if not evidence.passed:
            failed_gates.append(gate_id)
            failed_metrics.append(f"security_gate:{gate_id}")
            details.append(
                {
                    "metric": f"security_gate:{gate_id}",
                    "actual": evidence.actual,
                    "threshold": evidence.threshold,
                    "baseline": evidence.baseline,
                    "failed_case_ids": list(evidence.failed_case_ids),
                    "resource_types": list(evidence.resource_types),
                }
            )
        gates.append(
            {
                "gate_id": gate_id,
                "status": "PASS" if evidence.passed else "FAIL",
                "actual": evidence.actual,
                "threshold": evidence.threshold,
                "baseline": evidence.baseline,
                "failed_case_ids": list(evidence.failed_case_ids),
                "resource_types": list(evidence.resource_types),
            }
        )
    for gate_id in sorted(
        set(gate_counts) - set(_REQUIRED_SECURITY_GATE_IDS)
    ):
        failed_gates.append(f"UNKNOWN:{gate_id}")
        failed_metrics.append(f"security_gate_unknown:{gate_id}")
        details.append(
            {
                "metric": f"security_gate_unknown:{gate_id}",
                "actual": [
                    item.actual for item in evidence_by_gate[gate_id]
                ],
                "threshold": "one of required security gates",
                "baseline": None,
                "failed_case_ids": [],
                "resource_types": [],
            }
        )
    return gates, failed_gates, failed_metrics, details, failed_cases
