"""不可变 EvalRun 结果身份与 PostgreSQL 权威存储。"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


class EvalRunConflict(ValueError):
    """同一 eval_run_id 被绑定到不同结果内容。"""


class EvalRunIntegrityError(ValueError):
    """EvalRun 内容、身份或版本无法自证。"""


@dataclass(frozen=True, slots=True)
class EvalRunRecord:
    eval_run_id: str
    result_sha256: str
    dataset_version: str
    dataset_sha256: str
    release_id: str
    git_sha: str
    gate_status: str
    payload: dict[str, Any]


def eval_run_result_sha256(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        {
            key: value
            for key, value in payload.items()
            if key not in {"generated_at", "result_sha256", "eval_run_id"}
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def finalize_eval_run(payload: dict[str, Any]) -> dict[str, Any]:
    if "result_sha256" in payload or "eval_run_id" in payload:
        verify_eval_run(payload)
        return payload
    result_sha256 = eval_run_result_sha256(payload)
    return {
        **payload,
        "result_sha256": result_sha256,
        "eval_run_id": f"eval_{result_sha256}",
    }


def verify_eval_run(payload: dict[str, Any]) -> EvalRunRecord:
    if not isinstance(payload, dict):
        raise EvalRunIntegrityError("EvalRun must be an object")
    result_sha256 = payload.get("result_sha256")
    eval_run_id = payload.get("eval_run_id")
    if (
        not isinstance(result_sha256, str)
        or len(result_sha256) != 64
        or any(character not in "0123456789abcdef" for character in result_sha256)
    ):
        raise EvalRunIntegrityError("EvalRun result_sha256 is invalid")
    if eval_run_id != f"eval_{result_sha256}":
        raise EvalRunIntegrityError("EvalRun id does not match result hash")
    if eval_run_result_sha256(payload) != result_sha256:
        raise EvalRunIntegrityError("EvalRun result hash does not match payload")

    dataset = payload.get("dataset")
    code_identity = payload.get("code_identity")
    if not isinstance(dataset, dict) or not isinstance(code_identity, dict):
        raise EvalRunIntegrityError("EvalRun identity is incomplete")
    required = {
        "dataset_version": dataset.get("version"),
        "dataset_sha256": dataset.get("sha256"),
        "release_id": payload.get("release_id"),
        "git_sha": code_identity.get("git_sha"),
        "gate_status": payload.get("gate_status"),
    }
    if any(not isinstance(value, str) or not value.strip() for value in required.values()):
        raise EvalRunIntegrityError("EvalRun identity values are required")
    if required["gate_status"] not in {
        "PASS",
        "FAIL",
        "BLOCKED_PENDING_SIGNOFF",
    }:
        raise EvalRunIntegrityError("EvalRun gate_status is unsupported")
    if len(required["dataset_sha256"]) != 64:
        raise EvalRunIntegrityError("EvalRun dataset_sha256 is invalid")
    if len(required["git_sha"]) != 40:
        raise EvalRunIntegrityError("EvalRun git_sha is invalid")
    if required["gate_status"] == "PASS":
        _validate_pass_payload(payload)
    return EvalRunRecord(
        eval_run_id=eval_run_id,
        result_sha256=result_sha256,
        **required,
        payload=payload,
    )


def _validate_pass_payload(payload: dict[str, Any]) -> None:
    dataset = payload.get("dataset", {})
    metrics = payload.get("metrics")
    if dataset.get("owner_status") != "SIGNED_OFF":
        raise EvalRunIntegrityError(
            "PASS EvalRun requires a SIGNED_OFF dataset"
        )
    if payload.get("release_gate_status") != "PASS":
        raise EvalRunIntegrityError(
            "PASS EvalRun requires release_gate_status=PASS"
        )
    if payload.get("failed_metrics") or payload.get("failed_case_ids"):
        raise EvalRunIntegrityError("PASS EvalRun cannot contain failures")
    if not isinstance(metrics, dict):
        raise EvalRunIntegrityError("PASS EvalRun metrics are required")
    context = payload.get("performance_context")
    if not isinstance(context, dict):
        raise EvalRunIntegrityError(
            "PASS EvalRun performance_context is required"
        )
    context_fields = (
        "environment",
        "hardware_profile",
        "database_data_size",
        "model_route_version",
        "cache_state",
    )
    if any(
        not isinstance(context.get(field), str) or not context[field].strip()
        for field in context_fields
    ) or (
        not isinstance(context.get("concurrency"), int)
        or isinstance(context.get("concurrency"), bool)
        or context["concurrency"] < 1
    ):
        raise EvalRunIntegrityError(
            "PASS EvalRun performance_context is incomplete"
        )
    if not payload.get("baseline_eval_run_id"):
        raise EvalRunIntegrityError(
            "PASS EvalRun requires a baseline_eval_run_id"
        )
    minimums = {
        "retrieval_recall_at_5": 0.80,
        "retrieval_mrr": 0.65,
        "claim_citation_coverage": 1.0,
        "claim_citation_accuracy": 1.0,
        "run_reproducibility_rate": 1.0,
    }
    maximums = {
        "retrieval_scope_leak_rate": 0.0,
        "claim_contradiction_rate": 0.0,
        "claim_critical_unverifiable_rate": 0.0,
        "claim_hallucination_rate": 0.0,
        "illegal_candidate_leak_rate": 0.0,
        "budget_violation_count": 0,
        "budget_reconciliation_pending_count": 0,
    }
    required = {
        "factual_claim_count",
        "run_sample_count",
        "run_active_execution_p99_ms",
        "run_active_execution_max_ms",
        "session_token_count_max",
        "session_cost_micro_usd_max",
    }
    missing = required - set(metrics)
    if missing:
        raise EvalRunIntegrityError(
            "PASS EvalRun metrics missing: " + ", ".join(sorted(missing))
        )
    if metrics["factual_claim_count"] < 1:
        raise EvalRunIntegrityError("PASS EvalRun requires factual Claims")
    if metrics["run_sample_count"] < 100:
        raise EvalRunIntegrityError(
            "PASS EvalRun requires at least 100 samples"
        )
    for metric, minimum in minimums.items():
        value = metrics.get(metric)
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or value < minimum
        ):
            raise EvalRunIntegrityError(
                f"PASS EvalRun fails minimum metric: {metric}"
            )
    for metric, maximum in maximums.items():
        value = metrics.get(metric)
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or value > maximum
        ):
            raise EvalRunIntegrityError(
                f"PASS EvalRun exceeds maximum metric: {metric}"
            )
    if metrics["run_active_execution_max_ms"] > 600_000:
        raise EvalRunIntegrityError("PASS EvalRun exceeded execution timeout")
    if metrics["session_token_count_max"] > 50_000:
        raise EvalRunIntegrityError("PASS EvalRun exceeded token budget")
    if metrics["session_cost_micro_usd_max"] > 150_000:
        raise EvalRunIntegrityError("PASS EvalRun exceeded cost budget")
    recomputed = recompute_eval_metrics(payload)
    comparable = (
        "retrieval_recall_at_5",
        "retrieval_recall_at_20",
        "retrieval_mrr",
        "retrieval_scope_leak_rate",
        "factual_claim_count",
        "claim_citation_coverage",
        "claim_citation_accuracy",
        "claim_contradiction_rate",
        "claim_critical_unverifiable_rate",
        "claim_hallucination_rate",
        "illegal_candidate_leak_rate",
        "run_sample_count",
        "run_active_execution_p99_ms",
        "run_active_execution_max_ms",
        "session_token_count_max",
        "session_cost_micro_usd_max",
        "budget_violation_count",
        "budget_reconciliation_pending_count",
        "run_reproducibility_rate",
    )
    for metric in comparable:
        if not _same_metric(metrics.get(metric), recomputed.get(metric)):
            raise EvalRunIntegrityError(
                f"PASS EvalRun metric does not match raw results: {metric}"
            )


def recompute_eval_metrics(payload: dict[str, Any]) -> dict[str, int | float]:
    """从持久化原始结果重算发布门禁使用的汇总指标。"""
    retrieval_results = payload.get("retrieval_results")
    e2e_results = payload.get("e2e_results")
    run_samples = payload.get("run_samples")
    if not isinstance(retrieval_results, list):
        raise EvalRunIntegrityError(
            "PASS EvalRun requires retrieval_results"
        )
    if not isinstance(e2e_results, list):
        raise EvalRunIntegrityError("PASS EvalRun requires e2e_results")
    if not isinstance(run_samples, list):
        raise EvalRunIntegrityError("PASS EvalRun requires run_samples")

    eligible_retrieval = []
    returned_retrieval = []
    for result in retrieval_results:
        if not isinstance(result, dict):
            raise EvalRunIntegrityError(
                "retrieval_results entries must be objects"
            )
        expected = bool(result.get("expected_chunk_ids")) or bool(
            result.get("expected_doc_ids")
        )
        if expected and result.get("status") != "NO_EVIDENCE":
            eligible_retrieval.append(result)
        if (
            isinstance(result.get("returned_evidence_count"), int)
            and result["returned_evidence_count"] > 0
        ):
            returned_retrieval.append(result)
    eligible_count = len(eligible_retrieval)
    recall_at_5 = (
        sum(int(bool(item.get("hit_at_5"))) for item in eligible_retrieval)
        / eligible_count
        if eligible_count
        else 0.0
    )
    recall_at_20 = (
        sum(int(bool(item.get("hit_at_20"))) for item in eligible_retrieval)
        / eligible_count
        if eligible_count
        else 0.0
    )
    mrr = (
        sum(float(item.get("reciprocal_rank") or 0.0) for item in eligible_retrieval)
        / eligible_count
        if eligible_count
        else 0.0
    )
    scope_leak_rate = (
        sum(int(bool(item.get("scope_leaks"))) for item in returned_retrieval)
        / len(returned_retrieval)
        if returned_retrieval
        else 0.0
    )

    factual_claims = []
    excluded_case_count = 0
    illegal_candidate_leak_count = 0
    for result in e2e_results:
        if not isinstance(result, dict):
            raise EvalRunIntegrityError("e2e_results entries must be objects")
        claims = result.get("claims", ())
        if not isinstance(claims, list):
            raise EvalRunIntegrityError("e2e case claims must be an array")
        for claim in claims:
            if (
                isinstance(claim, dict)
                and claim.get("claim_type")
                in {"DOCUMENT_FACT", "STRUCTURED_FACT", "DERIVED_FACT"}
            ):
                factual_claims.append(claim)
        excluded = set(result.get("excluded_candidate_keys") or ())
        if excluded:
            excluded_case_count += 1
            if excluded & set(result.get("candidate_keys") or ()):
                illegal_candidate_leak_count += 1
    factual_count = len(factual_claims)
    with_evidence = [
        claim for claim in factual_claims if claim.get("checked_against")
    ]
    verified = [
        claim
        for claim in with_evidence
        if claim.get("status") == "VERIFIED"
    ]
    contradicted = [
        claim
        for claim in factual_claims
        if claim.get("status") == "CONTRADICTED"
    ]
    unverifiable = [
        claim
        for claim in factual_claims
        if claim.get("status") == "UNVERIFIABLE"
    ]
    critical = [
        claim
        for claim in factual_claims
        if claim.get("criticality") == "CRITICAL"
    ]
    critical_unverifiable = [
        claim
        for claim in critical
        if claim.get("status") == "UNVERIFIABLE"
    ]

    sample_durations = []
    max_tokens = 0
    max_cost = 0
    budget_violations = 0
    reconciliation_pending = 0
    reproducible_count = 0
    for sample in run_samples:
        if not isinstance(sample, dict):
            raise EvalRunIntegrityError("run_samples entries must be objects")
        duration = sample.get("active_execution_ms")
        if (
            not isinstance(duration, int)
            or isinstance(duration, bool)
            or duration < 0
        ):
            raise EvalRunIntegrityError("run sample duration is invalid")
        sample_durations.append(duration)
        max_tokens = max(max_tokens, int(sample.get("provider_token_count") or 0))
        max_cost = max(max_cost, int(sample.get("provider_cost_micro_usd") or 0))
        budget_violations += int(sample.get("budget_violation_count") or 0)
        reconciliation_pending += int(
            bool(sample.get("reconciliation_pending"))
        )
        reproducible_count += int(bool(sample.get("reproducible")))
    ordered = sorted(sample_durations)
    p99 = (
        ordered[max(1, math.ceil(0.99 * len(ordered))) - 1]
        if ordered
        else 0
    )
    return {
        "retrieval_recall_at_5": recall_at_5,
        "retrieval_recall_at_20": recall_at_20,
        "retrieval_mrr": mrr,
        "retrieval_scope_leak_rate": scope_leak_rate,
        "factual_claim_count": factual_count,
        "claim_citation_coverage": (
            len(with_evidence) / factual_count if factual_count else 0.0
        ),
        "claim_citation_accuracy": (
            len(verified) / len(with_evidence) if with_evidence else 0.0
        ),
        "claim_contradiction_rate": (
            len(contradicted) / factual_count if factual_count else 0.0
        ),
        "claim_critical_unverifiable_rate": (
            len(critical_unverifiable) / len(critical) if critical else 0.0
        ),
        "claim_hallucination_rate": (
            (len(contradicted) + len(unverifiable)) / factual_count
            if factual_count
            else 0.0
        ),
        "illegal_candidate_leak_rate": (
            illegal_candidate_leak_count / excluded_case_count
            if excluded_case_count
            else 0.0
        ),
        "run_sample_count": len(run_samples),
        "run_active_execution_p99_ms": p99,
        "run_active_execution_max_ms": (
            max(sample_durations) if sample_durations else 0
        ),
        "session_token_count_max": max_tokens,
        "session_cost_micro_usd_max": max_cost,
        "budget_violation_count": budget_violations,
        "budget_reconciliation_pending_count": reconciliation_pending,
        "run_reproducibility_rate": (
            reproducible_count / len(run_samples) if run_samples else 0.0
        ),
    }


def _same_metric(actual: Any, expected: Any) -> bool:
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return math.isclose(
            float(actual),
            float(expected),
            rel_tol=0.0,
            abs_tol=1e-12,
        )
    return actual == expected


async def persist_eval_run(
    dsn: str,
    payload: dict[str, Any],
) -> tuple[EvalRunRecord, bool]:
    record = verify_eval_run(finalize_eval_run(payload))
    conn = await psycopg.AsyncConnection.connect(dsn, row_factory=dict_row)
    try:
        async with conn.transaction():
            existing = await (
                await conn.execute(
                    """
                    SELECT eval_run_id, result_sha256
                    FROM eval_run_record
                    WHERE eval_run_id = %s
                    FOR UPDATE
                    """,
                    (record.eval_run_id,),
                )
            ).fetchone()
            if existing is not None:
                if existing["result_sha256"] != record.result_sha256:
                    raise EvalRunConflict(
                        "eval_run_id is bound to different content"
                    )
                return record, True
            await conn.execute(
                """
                INSERT INTO eval_run_record (
                    eval_run_id, result_sha256, dataset_version,
                    dataset_sha256, release_id, git_sha, gate_status, payload
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    record.eval_run_id,
                    record.result_sha256,
                    record.dataset_version,
                    record.dataset_sha256,
                    record.release_id,
                    record.git_sha,
                    record.gate_status,
                    Jsonb(record.payload),
                ),
            )
            return record, False
    finally:
        await conn.close()


async def load_eval_run(dsn: str, eval_run_id: str) -> EvalRunRecord:
    conn = await psycopg.AsyncConnection.connect(dsn, row_factory=dict_row)
    try:
        row = await (
            await conn.execute(
                """
                SELECT eval_run_id, result_sha256, dataset_version,
                       dataset_sha256, release_id, git_sha, gate_status,
                       payload
                FROM eval_run_record
                WHERE eval_run_id = %s
                """,
                (eval_run_id,),
            )
        ).fetchone()
    finally:
        await conn.close()
    if row is None:
        raise EvalRunIntegrityError("EvalRun was not found")
    record = verify_eval_run(row["payload"])
    for field in (
        "eval_run_id",
        "result_sha256",
        "dataset_version",
        "dataset_sha256",
        "release_id",
        "git_sha",
        "gate_status",
    ):
        if row[field] != getattr(record, field):
            raise EvalRunIntegrityError(
                f"EvalRun stored column mismatch: {field}"
            )
    return record
