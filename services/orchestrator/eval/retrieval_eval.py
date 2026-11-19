"""版本化检索 golden set 的装载与评测执行。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path

from rag.policy_retrieval import (
    ApplicabilityScope,
    PolicyRetrievalError,
    PolicyRetrievalQuery,
    PolicyRetrievalResult,
)


@dataclass(frozen=True, slots=True)
class RetrievalDataset:
    manifest: dict
    cases: tuple[dict, ...]


@dataclass(frozen=True, slots=True)
class RetrievalMetrics:
    recall_at_5: float
    recall_at_20: float
    mrr: float
    scope_leak_rate: float
    evidence_precision_at_5: float
    irrelevant_evidence_rate: float


@dataclass(frozen=True, slots=True)
class RetrievalCaseResult:
    case_id: str
    status: str
    hit_at_5: bool
    hit_at_20: bool
    reciprocal_rank: float
    expected_chunk_ids: tuple[str, ...]
    expected_doc_ids: tuple[str, ...]
    actual_evidence: tuple
    scope_leaks: tuple[dict, ...]
    hard_negative_chunk_leaks: tuple[str, ...]
    first_diff: dict | None


@dataclass(frozen=True, slots=True)
class RetrievalEvaluationReport:
    dataset_version: str
    release_id: str
    metrics: RetrievalMetrics
    eligible_case_count: int
    no_evidence_passed: int
    no_evidence_failed: int
    cases: tuple[RetrievalCaseResult, ...]
    gate_status: str
    quality_gate_applied: bool = False


_SUPPORTED_PROVINCES = frozenset({"湖南"})
_SUPPORTED_CATEGORIES = frozenset({"物理类", "历史类", "不限"})
_SUPPORTED_BATCHES = frozenset({"本科批", "不限"})
_SUPPORTED_DOCUMENT_TYPES = frozenset(
    {"charter", "policy", "physical_exam", "university_notice"}
)
_CRITICALITIES = frozenset({"CRITICAL", "NORMAL"})
_OWNER_STATUSES = frozenset({"DRAFT", "PENDING_SIGNOFF", "SIGNED_OFF"})
_RECALL_AT_5_MIN = 0.80
_MRR_MIN = 0.65
_EVIDENCE_PRECISION_AT_5_MIN = 0.20
_REQUIRED_MANIFEST_FIELDS = (
    "dataset_version",
    "dataset_sha256",
    "retrieval_cases_file",
    "retrieval_cases_sha256",
    "case_count",
    "created_at",
    "release_id",
    "owner",
    "owner_status",
    "immutable",
)
_REQUIRED_CASE_FIELDS = (
    "case_id",
    "query",
    "scope",
    "expected_chunk_ids",
    "expected_doc_ids",
    "must_exclude_sources",
    "tags",
    "criticality",
    "interference",
)


def is_authoritative_quality(manifest: dict, *, fixture: bool) -> bool:
    """只有真实 RetrievePolicy 且 owner 已签字时才是质量结论。"""
    return not fixture and manifest["owner_status"] == "SIGNED_OFF"


def load_retrieval_dataset(dataset_dir: str | Path) -> RetrievalDataset:
    root = Path(dataset_dir)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    _validate_manifest(manifest)
    cases_path = root / manifest["retrieval_cases_file"]
    cases_bytes = cases_path.read_bytes()
    actual_sha256 = hashlib.sha256(cases_bytes).hexdigest()
    if actual_sha256 != manifest["retrieval_cases_sha256"]:
        raise ValueError(
            "retrieval cases sha256 does not match the immutable manifest"
        )
    if manifest["dataset_sha256"] != actual_sha256:
        raise ValueError(
            "dataset_sha256 does not match the retrieval-only dataset"
        )
    cases = tuple(
        json.loads(line)
        for line in cases_bytes.decode("utf-8").splitlines()
        if line.strip()
    )
    if len(cases) != manifest["case_count"]:
        raise ValueError(
            "case_count does not match the retrieval cases file"
        )
    seen_case_ids = set()
    for case in cases:
        _validate_case(case)
        if case["case_id"] in seen_case_ids:
            raise ValueError(f"duplicate case_id: {case['case_id']}")
        seen_case_ids.add(case["case_id"])
    return RetrievalDataset(manifest=manifest, cases=cases)


def _validate_manifest(manifest: dict) -> None:
    for field in _REQUIRED_MANIFEST_FIELDS:
        if field not in manifest:
            raise ValueError(f"manifest is missing required field: {field}")
    if manifest["immutable"] is not True:
        raise ValueError("manifest.immutable must be true")
    if manifest["owner_status"] not in _OWNER_STATUSES:
        raise ValueError("manifest.owner_status is unsupported")
    _validate_sha256(
        manifest["retrieval_cases_sha256"],
        "manifest.retrieval_cases_sha256",
    )
    _validate_sha256(
        manifest["dataset_sha256"],
        "manifest.dataset_sha256",
    )
    try:
        datetime.fromisoformat(manifest["created_at"])
    except (TypeError, ValueError) as exc:
        raise ValueError("manifest.created_at is invalid") from exc
    if (
        isinstance(manifest["case_count"], bool)
        or not isinstance(manifest["case_count"], int)
        or manifest["case_count"] < 1
    ):
        raise ValueError("manifest.case_count is invalid")


def _validate_sha256(value, field: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError(f"{field} is invalid")


def _validate_case(case: dict) -> None:
    for field in _REQUIRED_CASE_FIELDS:
        if field not in case:
            raise ValueError(
                f"{case.get('case_id', '<unknown>')}: missing field {field}"
            )
    case_id = case["case_id"]
    if not isinstance(case_id, str) or not case_id.strip():
        raise ValueError("case_id must be a non-empty string")
    if not isinstance(case["query"], str) or not case["query"].strip():
        raise ValueError(f"{case_id}: query must be a non-empty string")
    if case["criticality"] not in _CRITICALITIES:
        raise ValueError(f"{case_id}: criticality is unsupported")
    if (
        not isinstance(case["tags"], list)
        or not case["tags"]
        or not all(isinstance(tag, str) and tag for tag in case["tags"])
    ):
        raise ValueError(f"{case_id}: tags must be a non-empty string list")
    for field in (
        "expected_chunk_ids",
        "expected_doc_ids",
        "must_exclude_sources",
        "must_exclude_chunk_ids",
    ):
        if field not in case and field == "must_exclude_chunk_ids":
            continue
        if (
            not isinstance(case[field], list)
            or not all(
                isinstance(value, str) and value for value in case[field]
            )
        ):
            raise ValueError(f"{case_id}: {field} must be a string list")
    if set(case["expected_chunk_ids"]) & set(
        case.get("must_exclude_chunk_ids", ())
    ):
        raise ValueError(
            f"{case_id}: expected chunk ids overlap "
            "must_exclude_chunk_ids"
        )
    if (
        "no_evidence" in case["tags"]
        and (case["expected_chunk_ids"] or case["expected_doc_ids"])
    ):
        raise ValueError(
            f"{case_id}: NO_EVIDENCE cases cannot declare expected evidence"
        )
    if (
        "no_evidence" not in case["tags"]
        and not case["expected_chunk_ids"]
        and not case["expected_doc_ids"]
    ):
        raise ValueError(
            f"{case_id}: non-NO_EVIDENCE cases require expected evidence"
        )
    if (
        "same_document_hard_negative" in case["tags"]
        and not case.get("must_exclude_chunk_ids")
    ):
        raise ValueError(
            f"{case_id}: same_document_hard_negative requires "
            "must_exclude_chunk_ids"
        )
    document_types = case.get("document_types", [])
    if (
        not isinstance(document_types, list)
        or any(
            document_type not in _SUPPORTED_DOCUMENT_TYPES
            for document_type in document_types
        )
    ):
        raise ValueError(f"{case_id}: document_types is unsupported")
    school_ids = case.get("school_ids", [])
    if (
        not isinstance(school_ids, list)
        or not all(
            isinstance(school_id, str) and school_id
            for school_id in school_ids
        )
    ):
        raise ValueError(f"{case_id}: school_ids must be a string list")
    interference = case.get("interference")
    if not isinstance(interference, list):
        raise ValueError(f"{case_id}: interference must be a list")
    for item in interference:
        if (
            not isinstance(item, dict)
            or item.get("kind")
            not in {
                "province",
                "year",
                "category",
                "batch",
                "school_id",
                "document_type",
            }
            or item.get("value") in (None, "")
        ):
            raise ValueError(
                f"{case_id}: interference entries require kind and value"
            )

    scope = case["scope"]
    if not isinstance(scope, dict):
        raise ValueError(f"{case_id}: scope must be an object")
    for field in ("province", "application_year", "category", "batch"):
        if field not in scope:
            raise ValueError(f"{case_id}: missing scope.{field}")
    if scope.get("province") not in _SUPPORTED_PROVINCES:
        raise ValueError(
            f"{case_id}: scope.province is unsupported"
        )
    application_year = scope.get("application_year")
    if (
        isinstance(application_year, bool)
        or not isinstance(application_year, int)
        or application_year < 1
    ):
        raise ValueError(
            f"{case_id}: scope.application_year is invalid"
        )
    if scope.get("category") not in _SUPPORTED_CATEGORIES:
        raise ValueError(
            f"{case_id}: scope.category is unsupported"
        )
    if scope.get("batch") not in _SUPPORTED_BATCHES:
        raise ValueError(
            f"{case_id}: scope.batch is unsupported"
        )
    for item in interference:
        kind = item["kind"]
        value = item["value"]
        if kind == "province" and value in {"全国", scope["province"]}:
            raise ValueError(
                f"{case_id}: province interference must conflict with scope"
            )
        if kind == "year" and (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value == scope["application_year"]
        ):
            raise ValueError(
                f"{case_id}: year interference must conflict with scope"
            )
        if kind == "category" and value in {"不限", scope["category"]}:
            raise ValueError(
                f"{case_id}: category interference must conflict with scope"
            )
        if kind == "batch" and value in {"不限", scope["batch"]}:
            raise ValueError(
                f"{case_id}: batch interference must conflict with scope"
            )
        if kind == "school_id" and value in set(case.get("school_ids", ())):
            raise ValueError(
                f"{case_id}: school interference must conflict with school_ids"
            )
    required_interference = {
        "cross_province": "province",
        "cross_year": "year",
        "cross_category": "category",
        "wrong_school": "school_id",
    }
    for tag, kind in required_interference.items():
        if tag in case["tags"] and not any(
            item["kind"] == kind for item in interference
        ):
            raise ValueError(
                f"{case_id}: {tag} requires {kind} interference metadata"
            )
    expected = set(case["expected_chunk_ids"]) | set(
        case["expected_doc_ids"]
    ) | set(case.get("school_ids", ()))
    overlap = expected & set(case["must_exclude_sources"])
    if overlap:
        raise ValueError(
            f"{case_id}: expected evidence overlaps "
            f"must_exclude_sources: {sorted(overlap)}"
        )


def evaluate_retrieval(
    dataset: RetrievalDataset,
    retriever,
    *,
    as_of: date,
    mode: str = "hybrid",
    rerank_enabled: bool = False,
    authoritative_quality: bool = False,
) -> RetrievalEvaluationReport:
    if rerank_enabled:
        raise ValueError("rerank_enabled=true is not accepted by this baseline")

    release_id = dataset.manifest["release_id"]
    case_results = []
    eligible_case_count = 0
    recall_at_5_hits = 0
    recall_at_20_hits = 0
    reciprocal_rank_sum = 0.0
    no_evidence_passed = 0
    no_evidence_failed = 0
    cases_with_evidence = 0
    cases_with_scope_leak = 0
    relevant_evidence_at_5 = 0
    returned_evidence_at_5 = 0

    for case in dataset.cases:
        scope = case["scope"]
        query = PolicyRetrievalQuery(
            principal_id="eval:retrieval-golden",
            query_text=case["query"],
            scope=ApplicabilityScope(
                province=scope["province"],
                application_year=scope["application_year"],
                category=scope["category"],
                batch=scope["batch"],
            ),
            release_id=release_id,
            mode=mode,
            school_ids=tuple(case.get("school_ids", ())),
            document_types=tuple(case.get("document_types", ())),
            top_k=20,
            pool_k=20,
        )
        retrieval_error_reason = None
        try:
            result = retriever.retrieve(query)
        except PolicyRetrievalError as exc:
            retrieval_error_reason = exc.reason
            result = PolicyRetrievalResult(
                status=exc.reason,
                release_id=release_id,
            )
        diagnostics = getattr(result, "diagnostics", None)
        if (
            not rerank_enabled
            and diagnostics is not None
            and diagnostics.rerank_enabled
        ):
            retrieval_error_reason = "RERANK_MODE_MISMATCH"
            result = PolicyRetrievalResult(
                status="RERANK_MODE_MISMATCH",
                release_id=release_id,
            )
        expected_ids = set(case["expected_chunk_ids"]) | set(
            case["expected_doc_ids"]
        )
        top_5_evidence = result.evidence[:5]
        acceptable_chunks = set(case["expected_chunk_ids"])
        acceptable_docs = set(case["expected_doc_ids"])
        if acceptable_chunks:
            relevant_evidence_at_5 += sum(
                item.document.chunk_id in acceptable_chunks
                for item in top_5_evidence
            )
        else:
            relevant_evidence_at_5 += sum(
                item.document.doc_id in acceptable_docs
                for item in top_5_evidence
            )
        returned_evidence_at_5 += len(top_5_evidence)
        expected_ranks = [
            rank
            for rank, item in enumerate(result.evidence, start=1)
            if (
                item.document.chunk_id in expected_ids
                or item.document.doc_id in expected_ids
            )
        ]
        scope_leaks = _scope_leaks(
            case,
            result.evidence,
            expected_release_id=release_id,
            result_release_id=result.release_id,
            as_of=as_of,
        )
        excluded_chunks = set(case.get("must_exclude_chunk_ids", ()))
        hard_negative_chunk_leaks = tuple(
            item.document.chunk_id
            for item in result.evidence
            if item.document.chunk_id in excluded_chunks
        )
        if result.evidence:
            cases_with_evidence += 1
        if scope_leaks:
            cases_with_scope_leak += 1

        if "no_evidence" in case["tags"]:
            passed = result.status == "NO_EVIDENCE"
            if passed:
                no_evidence_passed += 1
            else:
                no_evidence_failed += 1
            hit_at_5 = False
            hit_at_20 = False
            reciprocal_rank = 0.0
        else:
            eligible_case_count += 1
            hit_at_5 = any(rank <= 5 for rank in expected_ranks)
            hit_at_20 = any(rank <= 20 for rank in expected_ranks)
            reciprocal_rank = (
                1.0 / min(expected_ranks) if expected_ranks else 0.0
            )
            recall_at_5_hits += int(hit_at_5)
            recall_at_20_hits += int(hit_at_20)
            reciprocal_rank_sum += reciprocal_rank

        first_diff = None
        if retrieval_error_reason is not None:
            first_diff = {
                "field": "retrieval_error",
                "expected": {
                    "status": (
                        "NO_EVIDENCE"
                        if "no_evidence" in case["tags"]
                        else "FOUND"
                    )
                },
                "actual": {
                    "status": result.status,
                    "reason": retrieval_error_reason,
                    "evidence": [],
                },
            }
        elif scope_leaks:
            first_diff = {
                "field": "scope_leaks",
                "expected": [],
                "actual": list(scope_leaks),
            }
        elif hard_negative_chunk_leaks:
            first_diff = {
                "field": "hard_negative_chunk",
                "expected": [],
                "actual": list(hard_negative_chunk_leaks),
            }
        elif (
            ("no_evidence" in case["tags"] and result.status != "NO_EVIDENCE")
            or (
                "no_evidence" not in case["tags"]
                and result.status != "FOUND"
            )
            or (
                "no_evidence" not in case["tags"]
                and expected_ranks == []
            )
        ):
            first_diff = {
                "field": "evidence",
                "expected": {
                    "status": (
                        "NO_EVIDENCE"
                        if "no_evidence" in case["tags"]
                        else "FOUND"
                    ),
                    "chunk_ids": list(case["expected_chunk_ids"]),
                    "doc_ids": list(case["expected_doc_ids"]),
                },
                "actual": {
                    "status": result.status,
                    "evidence": [
                        {
                            "chunk_id": item.document.chunk_id,
                            "doc_id": item.document.doc_id,
                        }
                        for item in result.evidence
                    ],
                },
            }

        case_results.append(
            RetrievalCaseResult(
                case_id=case["case_id"],
                status=result.status,
                hit_at_5=hit_at_5,
                hit_at_20=hit_at_20,
                reciprocal_rank=reciprocal_rank,
                expected_chunk_ids=tuple(case["expected_chunk_ids"]),
                expected_doc_ids=tuple(case["expected_doc_ids"]),
                actual_evidence=tuple(result.evidence),
                scope_leaks=scope_leaks,
                hard_negative_chunk_leaks=hard_negative_chunk_leaks,
                first_diff=first_diff,
            )
        )

    if eligible_case_count == 0:
        recall_at_5 = 0.0
        recall_at_20 = 0.0
        mrr = 0.0
    else:
        recall_at_5 = recall_at_5_hits / eligible_case_count
        recall_at_20 = recall_at_20_hits / eligible_case_count
        mrr = reciprocal_rank_sum / eligible_case_count
    evidence_precision_at_5 = (
        relevant_evidence_at_5 / returned_evidence_at_5
        if returned_evidence_at_5
        else 0.0
    )
    metrics = RetrievalMetrics(
        recall_at_5=recall_at_5,
        recall_at_20=recall_at_20,
        mrr=mrr,
        scope_leak_rate=(
            cases_with_scope_leak / cases_with_evidence
            if cases_with_evidence
            else 0.0
        ),
        evidence_precision_at_5=evidence_precision_at_5,
        irrelevant_evidence_rate=1.0 - evidence_precision_at_5,
    )
    gate_status = (
        "PASS"
        if (
            eligible_case_count > 0
            and recall_at_5 >= _RECALL_AT_5_MIN
            and (not authoritative_quality or mrr >= _MRR_MIN)
            and (
                not authoritative_quality
                or evidence_precision_at_5 >= _EVIDENCE_PRECISION_AT_5_MIN
            )
            and no_evidence_failed == 0
            and metrics.scope_leak_rate == 0
            and all(case.first_diff is None for case in case_results)
        )
        else "FAIL"
    )
    return RetrievalEvaluationReport(
        dataset_version=dataset.manifest["dataset_version"],
        release_id=release_id,
        metrics=metrics,
        eligible_case_count=eligible_case_count,
        no_evidence_passed=no_evidence_passed,
        no_evidence_failed=no_evidence_failed,
        cases=tuple(case_results),
        gate_status=gate_status,
        quality_gate_applied=authoritative_quality,
    )


def _scope_leaks(
    case: dict,
    evidence_items,
    *,
    expected_release_id: str,
    result_release_id: str,
    as_of: date,
) -> tuple[dict, ...]:
    scope = case["scope"]
    allowed_schools = set(case.get("school_ids", ()))
    excluded_sources = set(case["must_exclude_sources"])
    leaks = []
    if result_release_id != expected_release_id:
        leaks.append(
            {
                "kind": "result_release",
                "chunk_id": None,
                "doc_id": None,
            }
        )
    for item in evidence_items:
        document = item.document
        applicability = document.applicability
        common = {
            "chunk_id": document.chunk_id,
            "doc_id": document.doc_id,
        }
        if (
            item.release_id != expected_release_id
            or document.release_id != expected_release_id
        ):
            leaks.append({"kind": "release", **common})
        if (
            "全国" not in applicability.regions
            and scope["province"] not in applicability.regions
        ):
            leaks.append({"kind": "province", **common})
        if (
            applicability.valid_from > as_of
            or (
                applicability.valid_to is not None
                and as_of > applicability.valid_to
            )
        ):
            leaks.append({"kind": "year", **common})
        if (
            "不限" not in applicability.categories
            and scope["category"] not in applicability.categories
        ):
            leaks.append({"kind": "category", **common})
        if (
            "不限" not in applicability.batches
            and scope["batch"] not in applicability.batches
        ):
            leaks.append({"kind": "batch", **common})
        expected_types = set(case.get("document_types", ()))
        if expected_types and document.document_type not in expected_types:
            leaks.append({"kind": "document_type", **common})
        if (
            allowed_schools
            and document.school_id is not None
            and document.school_id not in allowed_schools
        ):
            leaks.append({"kind": "school", **common})
        school_specific = (
            document.document_type in {"charter", "university_notice"}
            or document.issuer_type == "university"
        )
        if allowed_schools and school_specific and document.school_id is None:
            leaks.append({"kind": "missing_school_identity", **common})
        if (
            document.doc_id in excluded_sources
            or document.chunk_id in excluded_sources
            or (
                document.school_id is not None
                and document.school_id in excluded_sources
            )
        ):
            leaks.append({"kind": "must_exclude_source", **common})
    return tuple(leaks)


def build_baseline(
    report: RetrievalEvaluationReport,
    dataset: RetrievalDataset,
    *,
    code_identity: dict,
    retrieval_config: dict,
    run_identity: dict,
    generated_at: str,
) -> dict:
    required_run_identity = (
        "algorithm_config_version",
        "model_route_version",
        "price_table_version",
        "verifier_version",
        "environment",
        "hardware_profile",
        "baseline_eval_run_id",
    )
    missing_run_identity = [
        key for key in required_run_identity if key not in run_identity
    ]
    if missing_run_identity:
        raise ValueError(
            "run_identity is missing required fields: "
            + ", ".join(missing_run_identity)
        )
    cases = []
    for case_result in report.cases:
        cases.append(
            {
                "case_id": case_result.case_id,
                "status": case_result.status,
                "hit_at_5": case_result.hit_at_5,
                "hit_at_20": case_result.hit_at_20,
                "reciprocal_rank": case_result.reciprocal_rank,
                "expected_chunk_ids": list(
                    case_result.expected_chunk_ids
                ),
                "expected_doc_ids": list(case_result.expected_doc_ids),
                "actual_evidence": [
                    {
                        "evidence_id": item.evidence_id,
                        "release_id": item.release_id,
                        "chunk_id": item.document.chunk_id,
                        "doc_id": item.document.doc_id,
                        "school_id": item.document.school_id,
                        "title": item.document.title,
                        "document_type": item.document.document_type,
                        "section_path": item.document.section_path,
                        "excerpt": item.document.excerpt,
                        "regions": list(item.document.applicability.regions),
                        "categories": list(
                            item.document.applicability.categories
                        ),
                        "batches": list(item.document.applicability.batches),
                        "valid_from": (
                            item.document.applicability.valid_from.isoformat()
                        ),
                        "valid_to": (
                            item.document.applicability.valid_to.isoformat()
                            if item.document.applicability.valid_to is not None
                            else None
                        ),
                    }
                    for item in case_result.actual_evidence
                ],
                "scope_leaks": list(case_result.scope_leaks),
                "hard_negative_chunk_leaks": list(
                    case_result.hard_negative_chunk_leaks
                ),
                "first_diff": case_result.first_diff,
            }
        )

    payload = {
        "schema_version": "retrieval-eval-baseline-v3",
        "generated_at": generated_at,
        "dataset": {
            "version": dataset.manifest["dataset_version"],
            "sha256": dataset.manifest["dataset_sha256"],
            "case_count": dataset.manifest["case_count"],
            "owner": dataset.manifest["owner"],
            "owner_status": dataset.manifest["owner_status"],
        },
        "release_id": report.release_id,
        "code_identity": code_identity,
        "retrieval": retrieval_config,
        "algorithm_config_version": run_identity[
            "algorithm_config_version"
        ],
        "model_route_version": run_identity["model_route_version"],
        "price_table_version": run_identity["price_table_version"],
        "verifier_version": run_identity["verifier_version"],
        "environment": run_identity["environment"],
        "hardware_profile": run_identity["hardware_profile"],
        "baseline_eval_run_id": run_identity["baseline_eval_run_id"],
        "thresholds": {
            "recall_at_5_min": _RECALL_AT_5_MIN,
            "mrr_min": _MRR_MIN,
            "evidence_precision_at_5_min": _EVIDENCE_PRECISION_AT_5_MIN,
            "scope_leak_rate_max": 0.0,
        },
        "metrics": {
            "retrieval_recall_at_5": report.metrics.recall_at_5,
            "retrieval_recall_at_20": report.metrics.recall_at_20,
            "retrieval_mrr": report.metrics.mrr,
            "retrieval_scope_leak_rate": report.metrics.scope_leak_rate,
            "retrieval_evidence_precision_at_5": (
                report.metrics.evidence_precision_at_5
            ),
            "retrieval_irrelevant_evidence_rate": (
                report.metrics.irrelevant_evidence_rate
            ),
        },
        "eligible_case_count": report.eligible_case_count,
        "no_evidence_passed": report.no_evidence_passed,
        "no_evidence_failed": report.no_evidence_failed,
        "gate_status": report.gate_status,
        "quality_gate_applied": report.quality_gate_applied,
        "failed_case_ids": [
            case_result.case_id
            for case_result in report.cases
            if case_result.first_diff is not None
        ],
        "cases": cases,
    }
    identity_payload = {
        key: value for key, value in payload.items() if key != "generated_at"
    }
    canonical = json.dumps(
        identity_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    result_sha256 = hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()
    return {
        **payload,
        "result_sha256": result_sha256,
        "eval_run_id": "eval_" + result_sha256,
    }
