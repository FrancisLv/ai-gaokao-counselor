"""端到端报告评测：消费 RunLifecycle 最终结果并生成稳定指标。"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json

from eval.eval_dataset import JointEvalDataset
from eval.eval_run import finalize_eval_run
from evidence.normalization import text_exceeds_admission_boundary


_FACTUAL_CLAIM_TYPES = frozenset(
    {"DOCUMENT_FACT", "STRUCTURED_FACT", "DERIVED_FACT"}
)
_CLAIM_STATUSES = frozenset({"VERIFIED", "CONTRADICTED", "UNVERIFIABLE"})
_CRITICALITIES = frozenset({"CRITICAL", "NORMAL"})


@dataclass(frozen=True, slots=True)
class EndToEndClaim:
    claim_id: str
    claim_type: str
    criticality: str
    status: str
    checked_against: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EndToEndCaseOutcome:
    case_id: str
    terminal_status: str
    result_kind: str | None
    result_version: str | None
    run_id: str | None
    provider_cost_micro_usd: int
    report_text: str
    candidate_keys: tuple[str, ...]
    claims: tuple[EndToEndClaim, ...]


@dataclass(frozen=True, slots=True)
class EndToEndMetrics:
    factual_claim_count: int
    citation_coverage: float
    citation_accuracy: float
    contradiction_rate: float
    critical_unverifiable_rate: float
    hallucination_rate: float
    illegal_candidate_leak_rate: float


@dataclass(frozen=True, slots=True)
class EndToEndCaseResult:
    case_id: str
    passed: bool
    terminal_status: str | None
    result_kind: str | None
    result_version: str | None
    run_id: str | None
    provider_cost_micro_usd: int
    first_diff: dict | None
    claims: tuple[EndToEndClaim, ...] = ()
    candidate_keys: tuple[str, ...] = ()
    excluded_candidate_keys: tuple[str, ...] = ()
    report_text: str = ""


@dataclass(frozen=True, slots=True)
class EndToEndEvaluationReport:
    dataset_version: str
    dataset_sha256: str
    release_id: str
    metrics: EndToEndMetrics
    gate_status: str
    release_gate_status: str
    quality_gate_applied: bool
    failed_case_ids: tuple[str, ...]
    cases: tuple[EndToEndCaseResult, ...]


def evaluate_end_to_end(
    dataset: JointEvalDataset,
    outcomes: dict[str, EndToEndCaseOutcome],
) -> EndToEndEvaluationReport:
    case_results = []
    factual_claims = []
    illegal_candidate_case_count = 0
    illegal_candidate_leak_count = 0
    for case in dataset.e2e_cases:
        case_id = case["case_id"]
        outcome = outcomes.get(case_id)
        if outcome is None:
            case_results.append(
                EndToEndCaseResult(
                    case_id=case_id,
                    passed=False,
                    terminal_status=None,
                    result_kind=None,
                    result_version=None,
                    run_id=None,
                    provider_cost_micro_usd=0,
                    first_diff={
                        "field": "outcome",
                        "expected": "RunLifecycle result",
                        "actual": "MISSING",
                    },
                )
            )
            continue
        factual_claims.extend(
            claim
            for claim in outcome.claims
            if claim.claim_type in _FACTUAL_CLAIM_TYPES
        )
        expected_exclusions = set(
            case["expected"]["excluded_candidate_keys"]
        )
        if expected_exclusions:
            illegal_candidate_case_count += 1
            if expected_exclusions & set(outcome.candidate_keys):
                illegal_candidate_leak_count += 1
        first_diff = _first_difference(case, outcome)
        case_results.append(
            EndToEndCaseResult(
                case_id=case_id,
                passed=first_diff is None,
                terminal_status=outcome.terminal_status,
                result_kind=outcome.result_kind,
                result_version=outcome.result_version,
                run_id=outcome.run_id,
                provider_cost_micro_usd=outcome.provider_cost_micro_usd,
                first_diff=first_diff,
                claims=outcome.claims,
                candidate_keys=outcome.candidate_keys,
                excluded_candidate_keys=tuple(
                    case["expected"]["excluded_candidate_keys"]
                ),
                report_text=outcome.report_text,
            )
        )

    illegal_candidate_leak_rate = (
        illegal_candidate_leak_count / illegal_candidate_case_count
        if illegal_candidate_case_count
        else 0.0
    )
    metrics = _metrics(
        factual_claims,
        illegal_candidate_leak_rate=illegal_candidate_leak_rate,
    )
    gate_status = (
        "PASS"
        if (
            all(case.passed for case in case_results)
            and metrics.factual_claim_count > 0
            and metrics.citation_coverage == 1.0
            and metrics.citation_accuracy == 1.0
            and metrics.contradiction_rate == 0.0
            and metrics.critical_unverifiable_rate == 0.0
            and metrics.hallucination_rate == 0.0
        )
        else "FAIL"
    )
    quality_gate_applied = dataset.is_authoritative_quality
    release_gate_status = (
        gate_status
        if quality_gate_applied
        else "BLOCKED_PENDING_SIGNOFF"
    )
    return EndToEndEvaluationReport(
        dataset_version=dataset.manifest["dataset_version"],
        dataset_sha256=dataset.manifest["dataset_sha256"],
        release_id=dataset.manifest["release_id"],
        metrics=metrics,
        gate_status=gate_status,
        release_gate_status=release_gate_status,
        quality_gate_applied=quality_gate_applied,
        failed_case_ids=tuple(
            case.case_id
            for case in case_results
            if not case.passed
        ),
        cases=tuple(case_results),
    )


def build_eval_run(
    dataset: JointEvalDataset,
    retrieval_report,
    e2e_report: EndToEndEvaluationReport,
    *,
    run_identity: dict,
    generated_at: str,
    run_samples=(),
    performance_context=None,
    baseline: dict | None = None,
) -> dict:
    required_identity = (
        "git_sha",
        "algorithm_config_version",
        "model_route_version",
        "price_table_version",
        "verifier_version",
        "environment",
        "hardware_profile",
        "baseline_eval_run_id",
    )
    missing = [
        field
        for field in required_identity
        if not run_identity.get(field)
    ]
    if missing:
        raise ValueError(
            "run_identity is missing required fields: " + ", ".join(missing)
        )
    if retrieval_report.dataset_version != dataset.manifest["dataset_version"]:
        raise ValueError("retrieval report dataset_version does not match")
    if e2e_report.dataset_version != dataset.manifest["dataset_version"]:
        raise ValueError("end-to-end report dataset_version does not match")
    if retrieval_report.release_id != dataset.manifest["release_id"]:
        raise ValueError("retrieval report release_id does not match")
    if e2e_report.release_id != dataset.manifest["release_id"]:
        raise ValueError("end-to-end report release_id does not match")

    diagnostic_gate = (
        "PASS"
        if (
            retrieval_report.gate_status == "PASS"
            and e2e_report.gate_status == "PASS"
        )
        else "FAIL"
    )
    from eval.release_gate import (
        PerformanceContext,
        evaluate_release_gate,
    )

    if performance_context is None:
        performance_context = PerformanceContext(
            environment="",
            hardware_profile="",
            database_data_size="",
            model_route_version="",
            cache_state="",
            concurrency=0,
        )
    gate_report = evaluate_release_gate(
        dataset=dataset,
        retrieval_report=retrieval_report,
        e2e_report=e2e_report,
        run_identity=run_identity,
        run_samples=tuple(run_samples),
        performance_context=performance_context,
        baseline=baseline or {},
    )
    release_gate = gate_report.gate_status
    if (
        release_gate == "FAIL"
        and not dataset.is_authoritative_quality
    ):
        release_gate = "BLOCKED_PENDING_SIGNOFF"

    failed_case_ids = tuple(
        sorted(
            set(
                (
                    case.case_id
                    for case in retrieval_report.cases
                    if case.first_diff is not None
                )
            )
            | set(e2e_report.failed_case_ids)
        )
    )
    failure_details = []
    for case in e2e_report.cases:
        if case.first_diff is None:
            continue
        first_diff = case.first_diff
        failure_details.append(
            {
                "case_id": case.case_id,
                "kind": "end_to_end",
                "actual": first_diff.get("actual"),
                "threshold": first_diff.get(
                    "expected",
                    first_diff.get("expected_max"),
                ),
                "baseline_eval_run_id": run_identity[
                    "baseline_eval_run_id"
                ],
                "first_diff": first_diff,
            }
        )
    for case in retrieval_report.cases:
        if case.first_diff is None:
            continue
        failure_details.append(
            {
                "case_id": case.case_id,
                "kind": "retrieval",
                "actual": "FAIL",
                "threshold": "retrieval_gate",
                "baseline_eval_run_id": run_identity[
                    "baseline_eval_run_id"
                ],
                "first_diff": case.first_diff,
            }
        )
    for detail in gate_report.failure_details:
        failure_details.append(
            {
                "case_id": None,
                "kind": "metric",
                **detail,
            }
        )
    payload = {
        "schema_version": "eval-run-v1",
        "generated_at": generated_at,
        "dataset": {
            "version": dataset.manifest["dataset_version"],
            "sha256": dataset.manifest["dataset_sha256"],
            "retrieval_case_count": len(dataset.retrieval_cases),
            "e2e_case_count": len(dataset.e2e_cases),
            "owner": dataset.manifest.get("owner"),
            "owner_status": dataset.manifest["owner_status"],
        },
        "release_id": dataset.manifest["release_id"],
        "code_identity": {
            "git_sha": run_identity["git_sha"],
        },
        **{
            key: run_identity[key]
            for key in required_identity
            if key != "git_sha"
        },
        "metrics": {
            "retrieval_recall_at_5": retrieval_report.metrics.recall_at_5,
            "retrieval_recall_at_20": retrieval_report.metrics.recall_at_20,
            "retrieval_mrr": retrieval_report.metrics.mrr,
            "retrieval_scope_leak_rate": (
                retrieval_report.metrics.scope_leak_rate
            ),
            "retrieval_evidence_precision_at_5": (
                retrieval_report.metrics.evidence_precision_at_5
            ),
            "claim_citation_coverage": e2e_report.metrics.citation_coverage,
            "claim_citation_accuracy": e2e_report.metrics.citation_accuracy,
            "claim_contradiction_rate": (
                e2e_report.metrics.contradiction_rate
            ),
            "claim_critical_unverifiable_rate": (
                e2e_report.metrics.critical_unverifiable_rate
            ),
            "claim_hallucination_rate": e2e_report.metrics.hallucination_rate,
            "illegal_candidate_leak_rate": (
                e2e_report.metrics.illegal_candidate_leak_rate
            ),
            "factual_claim_count": e2e_report.metrics.factual_claim_count,
            **gate_report.metrics,
        },
        "retrieval_eligible_case_count": retrieval_report.eligible_case_count,
        "retrieval_no_evidence_passed": retrieval_report.no_evidence_passed,
        "retrieval_no_evidence_failed": retrieval_report.no_evidence_failed,
        "retrieval_quality_gate_applied": retrieval_report.quality_gate_applied,
        "retrieval_skipped_case_count": retrieval_report.skipped_case_count,
        "retrieval_skipped_case_reasons": (
            retrieval_report.skipped_case_reasons or {}
        ),
        "e2e_quality_gate_applied": e2e_report.quality_gate_applied,
        "quality_case_gate_status": diagnostic_gate,
        "gate_status": gate_report.gate_status,
        "release_gate_status": release_gate,
        "failed_metrics": list(gate_report.failed_metrics),
        "performance_context": {
            "environment": performance_context.environment,
            "hardware_profile": performance_context.hardware_profile,
            "database_data_size": performance_context.database_data_size,
            "model_route_version": performance_context.model_route_version,
            "cache_state": performance_context.cache_state,
            "concurrency": performance_context.concurrency,
        },
        "failed_case_ids": list(failed_case_ids),
        "failure_details": sorted(
            failure_details,
            key=lambda item: (
                item.get("case_id") or "",
                item["kind"],
                item.get("metric") or "",
            ),
        ),
        "retrieval_results": [
            {
                "case_id": case.case_id,
                "status": case.status,
                "hit_at_5": case.hit_at_5,
                "hit_at_20": case.hit_at_20,
                "reciprocal_rank": case.reciprocal_rank,
                "expected_chunk_ids": list(case.expected_chunk_ids),
                "expected_doc_ids": list(case.expected_doc_ids),
                "returned_evidence_count": len(case.actual_evidence),
                "scope_leaks": list(case.scope_leaks),
                "hard_negative_chunk_leaks": list(
                    case.hard_negative_chunk_leaks
                ),
                "first_diff": case.first_diff,
            }
            for case in retrieval_report.cases
        ],
        "e2e_results": [
            asdict(case)
            for case in e2e_report.cases
        ],
        "run_samples": [asdict(sample) for sample in run_samples],
    }
    return finalize_eval_run(payload)


def _first_difference(
    case: dict,
    outcome: EndToEndCaseOutcome,
) -> dict | None:
    expected = case["expected"]
    terminal_status = expected["terminal_status"]
    terminal_match = (
        outcome.terminal_status in {"CANCELLED", "SUCCEEDED"}
        if terminal_status == "CANCELLED_OR_SUCCEEDED"
        else outcome.terminal_status == terminal_status
    )
    if not terminal_match:
        return {
            "field": "terminal_status",
            "expected": terminal_status,
            "actual": outcome.terminal_status,
        }
    expected_result_kinds = expected["result_kind"]
    if not isinstance(expected_result_kinds, list):
        expected_result_kinds = [expected_result_kinds]
    if outcome.result_kind not in expected_result_kinds:
        return {
            "field": "result_kind",
            "expected": expected_result_kinds,
            "actual": outcome.result_kind,
        }
    excluded = sorted(
        set(expected["excluded_candidate_keys"])
        & set(outcome.candidate_keys)
    )
    if excluded:
        return {
            "field": "excluded_candidate_keys",
            "expected": [],
            "actual": excluded,
        }
    factual_claims = tuple(
        claim
        for claim in outcome.claims
        if claim.claim_type in _FACTUAL_CLAIM_TYPES
    )
    claim_types = {claim.claim_type for claim in factual_claims}
    missing_claim_types = sorted(
        set(expected["required_claim_types"]) - claim_types
    )
    if expected["terminal_status"] == "SUCCEEDED" and missing_claim_types:
        return {
            "field": "required_claim_types",
            "expected": expected["required_claim_types"],
            "actual": sorted(claim_types),
        }
    for forbidden in expected["forbidden_text_patterns"]:
        forbidden_present = (
            text_exceeds_admission_boundary(outcome.report_text)
            if forbidden in {"保证录取", "包录取", "100%录取"}
            else forbidden in outcome.report_text
        )
        if forbidden_present:
            return {
                "field": "forbidden_text",
                "expected": None,
                "actual": forbidden,
            }
    if (
        outcome.provider_cost_micro_usd
        > expected["max_provider_cost_micro_usd"]
    ):
        return {
            "field": "provider_cost_micro_usd",
            "expected_max": expected["max_provider_cost_micro_usd"],
            "actual": outcome.provider_cost_micro_usd,
        }
    if expected["terminal_status"] in {
        "SUCCEEDED",
        "CANCELLED_OR_SUCCEEDED",
    } and outcome.terminal_status == "SUCCEEDED":
        if not factual_claims:
            return {
                "field": "factual_claims",
                "expected": "at least one final factual Claim",
                "actual": 0,
            }
        for claim in factual_claims:
            if claim.status not in _CLAIM_STATUSES:
                return {
                    "field": f"claim:{claim.claim_id}",
                    "expected": sorted(_CLAIM_STATUSES),
                    "actual": claim.status,
                }
            if claim.status != "VERIFIED":
                return {
                    "field": f"claim:{claim.claim_id}",
                    "expected": "VERIFIED",
                    "actual": claim.status,
                }
            if not claim.checked_against:
                return {
                    "field": f"claim:{claim.claim_id}",
                    "expected": "at least one EvidenceRef",
                    "actual": "NONE",
                }
    return None


def _metrics(
    claims: list[EndToEndClaim],
    *,
    illegal_candidate_leak_rate: float,
) -> EndToEndMetrics:
    total = len(claims)
    if total == 0:
        return EndToEndMetrics(
            factual_claim_count=0,
            citation_coverage=0.0,
            citation_accuracy=0.0,
            contradiction_rate=0.0,
            critical_unverifiable_rate=0.0,
            hallucination_rate=0.0,
            illegal_candidate_leak_rate=illegal_candidate_leak_rate,
        )
    with_evidence = [claim for claim in claims if claim.checked_against]
    verified = [
        claim
        for claim in with_evidence
        if claim.status == "VERIFIED"
    ]
    contradicted = [
        claim for claim in claims if claim.status == "CONTRADICTED"
    ]
    unverifiable = [
        claim for claim in claims if claim.status == "UNVERIFIABLE"
    ]
    critical = [
        claim for claim in claims if claim.criticality == "CRITICAL"
    ]
    critical_unverifiable = [
        claim
        for claim in critical
        if claim.status == "UNVERIFIABLE"
    ]
    return EndToEndMetrics(
        factual_claim_count=total,
        citation_coverage=len(with_evidence) / total,
        citation_accuracy=(
            len(verified) / len(with_evidence)
            if with_evidence
            else 0.0
        ),
        contradiction_rate=len(contradicted) / total,
        critical_unverifiable_rate=(
            len(critical_unverifiable) / len(critical)
            if critical
            else 0.0
        ),
        hallucination_rate=(
            (len(contradicted) + len(unverifiable)) / total
        ),
        illegal_candidate_leak_rate=illegal_candidate_leak_rate,
    )
