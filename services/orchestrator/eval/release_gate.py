"""签字 golden set、SLO 与预算的发布门禁。"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any

from eval.e2e_eval import EndToEndEvaluationReport
from eval.eval_dataset import JointEvalDataset
from eval.retrieval_eval import RetrievalEvaluationReport


_QUALITY_REGRESSION_MAX_POINTS = 0.02
_RECALL_AT_5_MIN = 0.80
_MRR_MIN = 0.65
_SCOPE_LEAK_RATE_MAX = 0.0
_CITATION_COVERAGE_MIN = 1.0
_CITATION_ACCURACY_MIN = 1.0
_CONTRADICTION_RATE_MAX = 0.0
_CRITICAL_UNVERIFIABLE_RATE_MAX = 0.0
_HALLUCINATION_RATE_MAX = 0.0
_ILLEGAL_CANDIDATE_LEAK_RATE_MAX = 0.0
_P99_SAMPLE_COUNT_MIN = 100
_ACTIVE_EXECUTION_MAX_MS = 600_000
_P99_REGRESSION_MAX_RATIO = 0.10
_SESSION_TOKEN_COUNT_MAX = 50_000
_SESSION_COST_MICRO_USD_MAX = 150_000
_BUDGET_VIOLATION_COUNT_MAX = 0
_RUN_REPRODUCIBILITY_RATE_MIN = 1.0


@dataclass(frozen=True, slots=True)
class RunSloSample:
    run_id: str
    session_id: str
    release_id: str
    active_execution_ms: int
    provider_token_count: int
    provider_cost_micro_usd: int
    budget_violation_count: int
    reconciliation_pending: bool
    reproducible: bool


@dataclass(frozen=True, slots=True)
class PerformanceContext:
    environment: str
    hardware_profile: str
    database_data_size: str
    model_route_version: str
    cache_state: str
    concurrency: int


@dataclass(frozen=True, slots=True)
class ReleaseGateReport:
    gate_status: str
    failed_metrics: tuple[str, ...]
    metrics: dict[str, float | int | str]
    failure_details: tuple[dict, ...]


@dataclass(frozen=True, slots=True)
class SecurityGateEvidence:
    gate_id: str
    passed: bool
    actual: Any
    threshold: Any
    baseline: Any = None
    failed_case_ids: tuple[str, ...] = ()
    resource_types: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class M5bReleaseGateDecision:
    schema_version: str
    gate_status: str
    failed_gates: tuple[str, ...]
    failed_metrics: tuple[str, ...]
    failed_case_ids: tuple[str, ...]
    release_id: str
    dataset_version: str
    dataset_sha256: str
    git_sha: str
    gates: tuple[dict[str, Any], ...]
    failure_details: tuple[dict[str, Any], ...]
    artifact_sha256: str


_REQUIRED_SECURITY_GATE_IDS = (
    "CONSENT",
    "DELETION",
    "RETENTION",
    "RESTORE",
)


def evaluate_m5b_release_gate(
    *,
    dataset: JointEvalDataset,
    quality_report: ReleaseGateReport,
    run_identity: dict,
    failed_case_ids: tuple[str, ...],
    security_evidence: tuple[SecurityGateEvidence, ...],
) -> M5bReleaseGateDecision:
    """聚合质量、最终事实和安全证据，任何失败均不可被其他指标抵消。"""
    evidence_by_gate: dict[str, list[SecurityGateEvidence]] = defaultdict(list)
    for evidence in security_evidence:
        evidence_by_gate[evidence.gate_id].append(evidence)
    gate_counts = Counter(evidence.gate_id for evidence in security_evidence)
    critical_metric_names = (
        "factual_claim_count",
        "claim_citation_coverage",
        "claim_citation_accuracy",
        "claim_contradiction_rate",
        "claim_critical_unverifiable_rate",
        "claim_hallucination_rate",
    )
    critical_actual = {
        metric: quality_report.metrics.get(metric)
        for metric in critical_metric_names
    }
    critical_thresholds = {
        "factual_claim_count": 1,
        "claim_citation_coverage": _CITATION_COVERAGE_MIN,
        "claim_citation_accuracy": _CITATION_ACCURACY_MIN,
        "claim_contradiction_rate": _CONTRADICTION_RATE_MAX,
        "claim_critical_unverifiable_rate": (
            _CRITICAL_UNVERIFIABLE_RATE_MAX
        ),
        "claim_hallucination_rate": _HALLUCINATION_RATE_MAX,
    }
    critical_failed_metrics_list = []
    for metric in critical_metric_names:
        actual = critical_actual[metric]
        threshold = critical_thresholds[metric]
        failed = metric in quality_report.failed_metrics
        if metric == "factual_claim_count":
            failed = (
                failed
                or isinstance(actual, bool)
                or not isinstance(actual, int)
                or actual < 1
            )
        elif isinstance(actual, bool) or not isinstance(
            actual,
            (int, float),
        ):
            failed = True
        elif metric in {
            "claim_citation_coverage",
            "claim_citation_accuracy",
        }:
            failed = failed or actual < threshold
        else:
            failed = failed or actual > threshold
        if failed:
            critical_failed_metrics_list.append(metric)
    critical_failed_metrics = tuple(critical_failed_metrics_list)
    critical_gate_passed = (
        not critical_failed_metrics
        and "e2e_case_gate" not in quality_report.failed_metrics
        and "e2e_quality_evidence" not in quality_report.failed_metrics
    )
    quality_passed = quality_report.gate_status == "PASS"
    quality_gate = {
        "gate_id": "QUALITY_SLO_BUDGET",
        "status": "PASS" if quality_passed else "FAIL",
        "actual": quality_report.metrics,
        "threshold": {
            "recall_at_5_min": _RECALL_AT_5_MIN,
            "mrr_min": _MRR_MIN,
            "scope_leak_rate_max": _SCOPE_LEAK_RATE_MAX,
            "p99_sample_count_min": _P99_SAMPLE_COUNT_MIN,
            "active_execution_max_ms": _ACTIVE_EXECUTION_MAX_MS,
            "p99_regression_max_ratio": _P99_REGRESSION_MAX_RATIO,
            "session_token_count_max": _SESSION_TOKEN_COUNT_MAX,
            "session_cost_micro_usd_max": (
                _SESSION_COST_MICRO_USD_MAX
            ),
            "budget_violation_count_max": (
                _BUDGET_VIOLATION_COUNT_MAX
            ),
        },
        "baseline": {
            "eval_run_id": run_identity.get("baseline_eval_run_id")
        },
        "failed_metrics": list(quality_report.failed_metrics),
    }
    critical_gate = {
        "gate_id": "CRITICAL_FACTS",
        "status": "PASS" if critical_gate_passed else "FAIL",
        "actual": critical_actual,
        "threshold": critical_thresholds,
        "baseline": {
            "eval_run_id": run_identity.get("baseline_eval_run_id")
        },
        "failed_metrics": list(critical_failed_metrics),
    }
    security_gates = []
    failed_gates = []
    failed_metrics = list(quality_report.failed_metrics)
    all_failed_case_ids = set(failed_case_ids)
    failure_details = list(quality_report.failure_details)
    if not quality_passed:
        failed_gates.append("QUALITY_SLO_BUDGET")
    if not critical_gate_passed:
        failed_gates.append("CRITICAL_FACTS")
        for metric in critical_failed_metrics:
            if metric not in failed_metrics:
                failed_metrics.append(metric)
            failure_details.append(
                {
                    "metric": metric,
                    "actual": critical_actual[metric],
                    "threshold": critical_thresholds[metric],
                    "baseline": {
                        "eval_run_id": run_identity.get(
                            "baseline_eval_run_id"
                        )
                    },
                    "failed_case_ids": sorted(set(failed_case_ids)),
                    "resource_types": [],
                }
            )
    for gate_id in _REQUIRED_SECURITY_GATE_IDS:
        gate_evidence = evidence_by_gate.get(gate_id, [])
        if len(gate_evidence) > 1:
            duplicate_metric = f"security_gate_duplicate:{gate_id}"
            failed_gates.append(gate_id)
            failed_metrics.append(duplicate_metric)
            duplicate_cases = tuple(
                sorted(
                    {
                        case_id
                        for evidence in gate_evidence
                        for case_id in evidence.failed_case_ids
                    }
                )
            )
            duplicate_resources = tuple(
                sorted(
                    {
                        resource
                        for evidence in gate_evidence
                        for resource in evidence.resource_types
                    }
                )
            )
            all_failed_case_ids.update(duplicate_cases)
            failure_details.append(
                {
                    "metric": duplicate_metric,
                    "actual": [
                        evidence.actual for evidence in gate_evidence
                    ],
                    "threshold": "exactly one evidence record",
                    "baseline": [
                        evidence.baseline for evidence in gate_evidence
                    ],
                    "failed_case_ids": list(duplicate_cases),
                    "resource_types": list(duplicate_resources),
                }
            )
            security_gates.append(
                {
                    "gate_id": gate_id,
                    "status": "FAIL",
                    "actual": [
                        evidence.actual for evidence in gate_evidence
                    ],
                    "threshold": "exactly one evidence record",
                    "baseline": [
                        evidence.baseline for evidence in gate_evidence
                    ],
                    "failed_case_ids": list(duplicate_cases),
                    "resource_types": list(duplicate_resources),
                }
            )
            continue
        if not gate_evidence:
            failed_gates.append(gate_id)
            failed_metrics.append(f"security_gate_missing:{gate_id}")
            security_gates.append(
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
        evidence = gate_evidence[0]
        all_failed_case_ids.update(evidence.failed_case_ids)
        if not evidence.passed:
            failed_gates.append(gate_id)
            failed_metrics.append(f"security_gate:{gate_id}")
            failure_details.append(
                {
                    "metric": f"security_gate:{gate_id}",
                    "actual": evidence.actual,
                    "threshold": evidence.threshold,
                    "baseline": evidence.baseline,
                    "failed_case_ids": list(evidence.failed_case_ids),
                    "resource_types": list(evidence.resource_types),
                }
            )
        security_gates.append(
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
    for gate_id in sorted(set(gate_counts) - set(_REQUIRED_SECURITY_GATE_IDS)):
        failed_gates.append(f"UNKNOWN:{gate_id}")
        failed_metrics.append(f"security_gate_unknown:{gate_id}")
        failure_details.append(
            {
                "metric": f"security_gate_unknown:{gate_id}",
                "actual": [
                    evidence.actual for evidence in evidence_by_gate[gate_id]
                ],
                "threshold": "one of required security gates",
                "baseline": None,
                "failed_case_ids": [],
                "resource_types": [],
            }
        )
    gates = (quality_gate, critical_gate, *security_gates)
    payload = {
        "schema_version": "m5b-release-gate-v1",
        "gate_status": "FAIL" if failed_gates else "PASS",
        "failed_gates": sorted(set(failed_gates)),
        "failed_metrics": sorted(set(failed_metrics)),
        "failed_case_ids": sorted(all_failed_case_ids),
        "release_id": dataset.manifest["release_id"],
        "dataset_version": dataset.manifest["dataset_version"],
        "dataset_sha256": dataset.manifest["dataset_sha256"],
        "git_sha": run_identity["git_sha"],
        "gates": list(gates),
        "failure_details": sorted(
            failure_details,
            key=lambda item: (
                str(item.get("metric") or item.get("gate_id") or ""),
                str(item.get("baseline_eval_run_id") or ""),
            ),
        ),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return M5bReleaseGateDecision(
        schema_version=payload["schema_version"],
        gate_status=payload["gate_status"],
        failed_gates=tuple(payload["failed_gates"]),
        failed_metrics=tuple(payload["failed_metrics"]),
        failed_case_ids=tuple(payload["failed_case_ids"]),
        release_id=payload["release_id"],
        dataset_version=payload["dataset_version"],
        dataset_sha256=payload["dataset_sha256"],
        git_sha=payload["git_sha"],
        gates=tuple(payload["gates"]),
        failure_details=tuple(payload["failure_details"]),
        artifact_sha256=hashlib.sha256(canonical).hexdigest(),
    )


def evaluate_release_gate(
    *,
    dataset: JointEvalDataset,
    retrieval_report: RetrievalEvaluationReport,
    e2e_report: EndToEndEvaluationReport,
    run_identity: dict,
    run_samples: tuple[RunSloSample, ...],
    performance_context: PerformanceContext,
    baseline: dict,
) -> ReleaseGateReport:
    failed_metrics = []
    if not dataset.is_authoritative_quality:
        failed_metrics.append("dataset_owner_status")
    if not retrieval_report.quality_gate_applied:
        failed_metrics.append("retrieval_quality_evidence")
    if not e2e_report.quality_gate_applied:
        failed_metrics.append("e2e_quality_evidence")
    if retrieval_report.gate_status != "PASS":
        failed_metrics.append("retrieval_case_gate")
    if e2e_report.gate_status != "PASS":
        failed_metrics.append("e2e_case_gate")
    if retrieval_report.metrics.recall_at_5 < _RECALL_AT_5_MIN:
        failed_metrics.append("retrieval_recall_at_5")
    if retrieval_report.metrics.mrr < _MRR_MIN:
        failed_metrics.append("retrieval_mrr")
    if (
        retrieval_report.metrics.scope_leak_rate
        > _SCOPE_LEAK_RATE_MAX
    ):
        failed_metrics.append("retrieval_scope_leak_rate")
    if e2e_report.metrics.factual_claim_count == 0:
        failed_metrics.append("factual_claim_count")
    if (
        e2e_report.metrics.citation_coverage
        < _CITATION_COVERAGE_MIN
    ):
        failed_metrics.append("claim_citation_coverage")
    if (
        e2e_report.metrics.citation_accuracy
        < _CITATION_ACCURACY_MIN
    ):
        failed_metrics.append("claim_citation_accuracy")
    if (
        e2e_report.metrics.contradiction_rate
        > _CONTRADICTION_RATE_MAX
    ):
        failed_metrics.append("claim_contradiction_rate")
    if (
        e2e_report.metrics.critical_unverifiable_rate
        > _CRITICAL_UNVERIFIABLE_RATE_MAX
    ):
        failed_metrics.append("claim_critical_unverifiable_rate")
    if (
        e2e_report.metrics.hallucination_rate
        > _HALLUCINATION_RATE_MAX
    ):
        failed_metrics.append("claim_hallucination_rate")
    if (
        e2e_report.metrics.illegal_candidate_leak_rate
        > _ILLEGAL_CANDIDATE_LEAK_RATE_MAX
    ):
        failed_metrics.append("illegal_candidate_leak_rate")
    if len(run_samples) < _P99_SAMPLE_COUNT_MIN:
        failed_metrics.append("run_sample_count")
    if any(
        sample.release_id != dataset.manifest["release_id"]
        for sample in run_samples
    ):
        failed_metrics.append("run_release_id")
    p99_ms = _p99(
        [sample.active_execution_ms for sample in run_samples]
    )
    if any(
        sample.active_execution_ms > _ACTIVE_EXECUTION_MAX_MS
        for sample in run_samples
    ):
        failed_metrics.append("run_active_execution_timeout")
    baseline_p99 = baseline.get("metrics", {}).get(
        "run_active_execution_p99_ms"
    )
    if (
        isinstance(baseline_p99, (int, float))
        and baseline_p99 > 0
        and p99_ms > baseline_p99 * (1 + _P99_REGRESSION_MAX_RATIO)
    ):
        failed_metrics.append("run_p99_regression")
    max_tokens = max(
        (sample.provider_token_count for sample in run_samples),
        default=0,
    )
    max_cost = max(
        (sample.provider_cost_micro_usd for sample in run_samples),
        default=0,
    )
    budget_violation_count = sum(
        sample.budget_violation_count for sample in run_samples
    )
    if max_tokens > _SESSION_TOKEN_COUNT_MAX:
        failed_metrics.append("session_token_count")
    if max_cost > _SESSION_COST_MICRO_USD_MAX:
        failed_metrics.append("session_cost_micro_usd")
    if budget_violation_count > _BUDGET_VIOLATION_COUNT_MAX:
        failed_metrics.append("budget_violation_count")
    if any(sample.reconciliation_pending for sample in run_samples):
        failed_metrics.append("budget_reconciliation_pending")
    reproducibility_rate = (
        sum(int(sample.reproducible) for sample in run_samples)
        / len(run_samples)
        if run_samples
        else 0.0
    )
    if reproducibility_rate < _RUN_REPRODUCIBILITY_RATE_MIN:
        failed_metrics.append("run_reproducibility_rate")
    performance_values = (
        performance_context.environment,
        performance_context.hardware_profile,
        performance_context.database_data_size,
        performance_context.model_route_version,
        performance_context.cache_state,
    )
    performance_payload = _performance_context_payload(
        performance_context
    )
    if (
        any(not str(value).strip() for value in performance_values)
        or performance_context.concurrency < 1
        or performance_context.environment != run_identity.get("environment")
        or performance_context.hardware_profile
        != run_identity.get("hardware_profile")
        or performance_context.model_route_version
        != run_identity.get("model_route_version")
    ):
        failed_metrics.append("performance_context")
    if (
        baseline.get("performance_context")
        != performance_payload
    ):
        failed_metrics.append("baseline_performance_context")
    baseline_dataset = baseline.get("dataset", {}).get("version")
    if baseline_dataset != dataset.manifest["dataset_version"]:
        failed_metrics.append("baseline_dataset_version")
    else:
        baseline_metrics = baseline.get("metrics", {})
        required_baseline_metrics = (
            "retrieval_recall_at_5",
            "retrieval_mrr",
            "run_active_execution_p99_ms",
        )
        if any(
            not isinstance(baseline_metrics.get(metric), (int, float))
            for metric in required_baseline_metrics
        ):
            failed_metrics.append("baseline_metrics")
        if (
            baseline_metrics.get("retrieval_recall_at_5")
            is not None
            and baseline_metrics["retrieval_recall_at_5"]
            - retrieval_report.metrics.recall_at_5
            > _QUALITY_REGRESSION_MAX_POINTS
        ):
            failed_metrics.append("retrieval_quality_regression")
        if (
            baseline_metrics.get("retrieval_mrr")
            is not None
            and baseline_metrics["retrieval_mrr"]
            - retrieval_report.metrics.mrr
            > _QUALITY_REGRESSION_MAX_POINTS
        ):
            failed_metrics.append("retrieval_quality_regression")
    metrics = {
        "retrieval_recall_at_5": retrieval_report.metrics.recall_at_5,
        "retrieval_recall_at_20": retrieval_report.metrics.recall_at_20,
        "retrieval_mrr": retrieval_report.metrics.mrr,
        "retrieval_scope_leak_rate": (
            retrieval_report.metrics.scope_leak_rate
        ),
        "claim_citation_coverage": e2e_report.metrics.citation_coverage,
        "claim_citation_accuracy": e2e_report.metrics.citation_accuracy,
        "claim_contradiction_rate": e2e_report.metrics.contradiction_rate,
        "claim_critical_unverifiable_rate": (
            e2e_report.metrics.critical_unverifiable_rate
        ),
        "claim_hallucination_rate": e2e_report.metrics.hallucination_rate,
        "illegal_candidate_leak_rate": (
            e2e_report.metrics.illegal_candidate_leak_rate
        ),
        "factual_claim_count": e2e_report.metrics.factual_claim_count,
        "run_active_execution_p99_ms": p99_ms,
        "run_sample_count": len(run_samples),
        "run_active_execution_max_ms": max(
            (sample.active_execution_ms for sample in run_samples),
            default=0,
        ),
        "session_token_count_max": max_tokens,
        "session_cost_micro_usd_max": max_cost,
        "budget_violation_count": budget_violation_count,
        "budget_reconciliation_pending_count": sum(
            int(sample.reconciliation_pending)
            for sample in run_samples
        ),
        "run_reproducibility_rate": reproducibility_rate,
        "database_data_size": performance_context.database_data_size,
        "cache_state": performance_context.cache_state,
        "concurrency": performance_context.concurrency,
    }
    return ReleaseGateReport(
        gate_status="FAIL" if failed_metrics else "PASS",
        failed_metrics=tuple(failed_metrics),
        metrics=metrics,
        failure_details=_gate_failure_details(
            failed_metrics,
            metrics=metrics,
            run_identity=run_identity,
            dataset=dataset,
            retrieval_report=retrieval_report,
            e2e_report=e2e_report,
            run_samples=run_samples,
            performance_context=performance_context,
            baseline=baseline,
        ),
    )


def _p99(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    rank = max(1, math.ceil(0.99 * len(ordered)))
    return ordered[rank - 1]


def _performance_context_payload(
    performance_context: PerformanceContext,
) -> dict[str, str | int]:
    return {
        "environment": performance_context.environment,
        "hardware_profile": performance_context.hardware_profile,
        "database_data_size": performance_context.database_data_size,
        "model_route_version": performance_context.model_route_version,
        "cache_state": performance_context.cache_state,
        "concurrency": performance_context.concurrency,
    }


def _gate_failure_details(
    failed_metrics: list[str],
    *,
    metrics: dict,
    run_identity: dict,
    dataset: JointEvalDataset,
    retrieval_report: RetrievalEvaluationReport,
    e2e_report: EndToEndEvaluationReport,
    run_samples: tuple[RunSloSample, ...],
    performance_context: PerformanceContext,
    baseline: dict,
) -> tuple[dict, ...]:
    thresholds = {
        "retrieval_recall_at_5": _RECALL_AT_5_MIN,
        "retrieval_mrr": _MRR_MIN,
        "retrieval_scope_leak_rate": _SCOPE_LEAK_RATE_MAX,
        "factual_claim_count": 1,
        "claim_citation_coverage": _CITATION_COVERAGE_MIN,
        "claim_citation_accuracy": _CITATION_ACCURACY_MIN,
        "claim_contradiction_rate": _CONTRADICTION_RATE_MAX,
        "claim_critical_unverifiable_rate": (
            _CRITICAL_UNVERIFIABLE_RATE_MAX
        ),
        "claim_hallucination_rate": _HALLUCINATION_RATE_MAX,
        "illegal_candidate_leak_rate": (
            _ILLEGAL_CANDIDATE_LEAK_RATE_MAX
        ),
        "run_sample_count": _P99_SAMPLE_COUNT_MIN,
        "run_release_id": dataset.manifest["release_id"],
        "run_active_execution_timeout": _ACTIVE_EXECUTION_MAX_MS,
        "run_p99_regression": "baseline_p99 * 1.10",
        "session_token_count": _SESSION_TOKEN_COUNT_MAX,
        "session_cost_micro_usd": _SESSION_COST_MICRO_USD_MAX,
        "budget_violation_count": _BUDGET_VIOLATION_COUNT_MAX,
        "budget_reconciliation_pending": 0,
        "run_reproducibility_rate": _RUN_REPRODUCIBILITY_RATE_MIN,
        "performance_context": "complete fixed context",
        "baseline_performance_context": "same fixed performance context",
        "retrieval_case_gate": "PASS",
        "e2e_case_gate": "PASS",
        "retrieval_quality_evidence": True,
        "e2e_quality_evidence": True,
        "dataset_owner_status": "SIGNED_OFF",
        "baseline_dataset_version": dataset.manifest["dataset_version"],
        "baseline_metrics": "complete numeric baseline metrics",
        "retrieval_quality_regression": (
            _QUALITY_REGRESSION_MAX_POINTS
        ),
    }
    actual_by_metric = {
        **metrics,
        "run_active_execution_timeout": metrics[
            "run_active_execution_max_ms"
        ],
        "budget_reconciliation_pending": metrics[
            "budget_reconciliation_pending_count"
        ],
        "performance_context": _performance_context_payload(
            performance_context
        ),
        "baseline_performance_context": _performance_context_payload(
            performance_context
        ),
        "run_release_id": sorted(
            {sample.release_id for sample in run_samples}
        ),
        "dataset_owner_status": dataset.manifest["owner_status"],
        "baseline_dataset_version": baseline.get("dataset", {}).get(
            "version"
        ),
        "baseline_metrics": baseline.get("metrics", {}),
        "retrieval_quality_regression": {
            "recall_at_5": metrics["retrieval_recall_at_5"],
            "mrr": metrics["retrieval_mrr"],
        },
        "run_p99_regression": metrics["run_active_execution_p99_ms"],
        "retrieval_case_gate": retrieval_report.gate_status,
        "e2e_case_gate": e2e_report.gate_status,
        "retrieval_quality_evidence": (
            retrieval_report.quality_gate_applied
        ),
        "e2e_quality_evidence": e2e_report.quality_gate_applied,
    }
    baseline_metrics = baseline.get("metrics", {})
    baseline_by_metric = {
        **baseline_metrics,
        "retrieval_quality_regression": {
            "recall_at_5": baseline_metrics.get(
                "retrieval_recall_at_5"
            ),
            "mrr": baseline_metrics.get("retrieval_mrr"),
        },
        "run_p99_regression": baseline_metrics.get(
            "run_active_execution_p99_ms"
        ),
        "baseline_dataset_version": baseline.get("dataset", {}).get(
            "version"
        ),
        "baseline_performance_context": baseline.get(
            "performance_context"
        ),
        "run_release_id": dataset.manifest["release_id"],
    }
    return tuple(
        {
            "metric": metric,
            "actual": actual_by_metric.get(metric),
            "threshold": thresholds.get(metric),
            "baseline_eval_run_id": run_identity.get(
                "baseline_eval_run_id"
            ),
            "baseline": baseline_by_metric.get(metric),
        }
        for metric in sorted(set(failed_metrics))
    )
