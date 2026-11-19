"""证据包的事实校验实现，公开入口仅为 VerifyPackage。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from collections import Counter
from decimal import Decimal, InvalidOperation
import hashlib
import math
import re
from typing import Protocol
from zoneinfo import ZoneInfo

from rag.policy_retrieval import DocumentEvidence


@dataclass(frozen=True, slots=True)
class DerivedEvidence:
    operation: str
    algorithm: str
    algorithm_version: str
    config_sha256: str
    input_snapshot_sha256: str
    profile_version: str
    release_id: str
    sample_years: tuple[int, ...]
    output_field: str
    output_value: str | int | float | Decimal | bool | None
    unit: str | None
    precision: int | None
    assumptions: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    evidence_id: str
    evidence_type: str
    release_id: str
    content_sha256: str
    created_at: datetime
    producer_version: str
    document: DocumentEvidence | None = None
    derived: DerivedEvidence | None = None


@dataclass(frozen=True, slots=True)
class NormalizedFact:
    subject: str
    predicate: str
    object: str | int | float | Decimal | bool | None
    unit: str | None
    sample_years: tuple[int, ...] = ()
    precision: int | None = None


@dataclass(frozen=True, slots=True)
class RenderedBinding:
    fact_path: str
    rendered_text: str
    evidence_id: str


@dataclass(frozen=True, slots=True)
class Claim:
    claim_id: str
    claim_type: str
    criticality: str
    normalized: NormalizedFact
    rendered_text: str
    evidence_refs: tuple[EvidenceRef, ...]
    rendered_bindings: tuple[RenderedBinding, ...] = ()


@dataclass(frozen=True, slots=True)
class VerificationScope:
    province: str
    application_year: int | None
    category: str
    batch: str
    school_id: str | None = None


@dataclass(frozen=True, slots=True)
class EvidencePackage:
    release_id: str
    profile_version: str
    result_version: str
    verifier_version: str
    scope: VerificationScope
    claims: tuple[Claim, ...]
    evidence_registry: tuple[EvidenceRef, ...]


@dataclass(frozen=True, slots=True)
class ClaimVerification:
    claim_id: str
    status: str
    reason: str
    checked_against: tuple[str, ...]
    deterministic: bool


@dataclass(frozen=True, slots=True)
class VerificationReport:
    status: str
    claims: tuple[ClaimVerification, ...]
    blocking_reasons: tuple[str, ...]
    stripped_claim_ids: tuple[str, ...] = ()


class EntailmentVerifier(Protocol):
    verifier_version: str

    def verify(
        self,
        claim: Claim,
        document: DocumentEvidence,
    ) -> bool | None: ...


class DerivedEvidenceRecomputer(Protocol):
    def recompute(
        self,
        evidence: DerivedEvidence,
    ) -> DerivedEvidence | None: ...


def VerifyPackage(
    package: EvidencePackage,
    *,
    today: date | None = None,
    entailment_verifier: EntailmentVerifier | None = None,
    derived_recomputer: DerivedEvidenceRecomputer | None = None,
) -> VerificationReport:
    checked_today = today or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    package_reason = _package_preflight(package)
    grouped_evidence = _group_evidence(package.evidence_registry)
    registry = {
        evidence_id: refs[-1]
        for evidence_id, refs in grouped_evidence.items()
    }
    duplicate_evidence_ids = {
        evidence_id
        for evidence_id, refs in grouped_evidence.items()
        if len(refs) > 1
    }
    duplicated_claim_ids = {
        claim_id
        for claim_id, count in Counter(
            claim.claim_id for claim in package.claims
        ).items()
        if count > 1
    }
    report_claims: list[ClaimVerification] = []
    blocking_reasons: set[str] = set()

    for claim in sorted(package.claims, key=lambda item: item.claim_id):
        refs = sorted(claim.evidence_refs, key=lambda item: item.evidence_id)
        checked_against = tuple(ref.evidence_id for ref in refs)
        if package_reason is not None:
            decision, reason, deterministic = (
                "UNVERIFIABLE",
                package_reason,
                True,
            )
        elif _claim_preflight_reason(claim) is not None:
            decision, reason, deterministic = (
                "UNVERIFIABLE",
                _claim_preflight_reason(claim) or "CLAIM_UNSUPPORTED",
                True,
            )
        elif claim.claim_id in duplicated_claim_ids:
            decision, reason, deterministic = (
                "UNVERIFIABLE",
                "CLAIM_UNSUPPORTED",
                True,
            )
        elif duplicate_evidence_ids.intersection(checked_against):
            decision, reason, deterministic = (
                "UNVERIFIABLE",
                "CLAIM_UNSUPPORTED",
                True,
            )
        else:
            decision, reason, deterministic = _verify_claim(
                package,
                claim,
                refs,
                registry,
                checked_today,
                entailment_verifier,
                derived_recomputer,
            )
        report_claims.append(
            ClaimVerification(
                claim_id=claim.claim_id,
                status=decision,
                reason=reason,
                checked_against=checked_against,
                deterministic=deterministic,
            )
        )
        if decision != "VERIFIED":
            blocking_reasons.add(reason)

    if not report_claims:
        blocking_reasons.add("CLAIM_UNSUPPORTED")
    status = "PASS" if not blocking_reasons else "BLOCKED"
    return VerificationReport(
        status=status,
        claims=tuple(report_claims),
        blocking_reasons=tuple(sorted(blocking_reasons)),
    )


def _verify_claim(
    package: EvidencePackage,
    claim: Claim,
    refs: tuple[EvidenceRef, ...],
    registry: dict[str, EvidenceRef],
    today: date,
    entailment_verifier: EntailmentVerifier | None,
    derived_recomputer: DerivedEvidenceRecomputer | None,
) -> tuple[str, str, bool]:
    if claim.claim_type == "DOCUMENT_FACT":
        return _verify_document_claim(
            package,
            claim,
            refs,
            registry,
            today,
            entailment_verifier,
        )
    if claim.claim_type == "DERIVED_FACT":
        return _verify_derived_claim(
            package,
            claim,
            refs,
            registry,
            derived_recomputer,
        )
    return "UNVERIFIABLE", "CLAIM_UNSUPPORTED", True


def _verify_document_claim(
    package: EvidencePackage,
    claim: Claim,
    refs: tuple[EvidenceRef, ...],
    registry: dict[str, EvidenceRef],
    today: date,
    entailment_verifier: EntailmentVerifier | None,
) -> tuple[str, str, bool]:
    if not package.verifier_version:
        return "UNVERIFIABLE", "VERIFIER_VERSION_MISSING", True
    if not refs:
        return "UNVERIFIABLE", "CLAIM_UNSUPPORTED", True
    if claim.claim_type != "DOCUMENT_FACT":
        return "UNVERIFIABLE", "CLAIM_UNSUPPORTED", True

    documents: list[tuple[EvidenceRef, DocumentEvidence]] = []
    for ref in refs:
        if not _valid_evidence_ref(ref):
            return "UNVERIFIABLE", "EVIDENCE_NOT_FOUND", True
        registered = registry.get(ref.evidence_id)
        if registered is None:
            return "UNVERIFIABLE", "EVIDENCE_NOT_FOUND", True
        if not _valid_evidence_ref(registered):
            return "UNVERIFIABLE", "EVIDENCE_NOT_FOUND", True
        if (
            ref.release_id != package.release_id
            or registered.release_id != package.release_id
        ):
            return "CONTRADICTED", "EVIDENCE_RELEASE_MISMATCH", True
        if ref.evidence_type != "DOCUMENT":
            return "UNVERIFIABLE", "CLAIM_UNSUPPORTED", True
        if registered.evidence_type != ref.evidence_type:
            return "UNVERIFIABLE", "CLAIM_UNSUPPORTED", True
        if registered.content_sha256 != ref.content_sha256:
            return "CONTRADICTED", "EVIDENCE_HASH_MISMATCH", True
        document = registered.document
        if document is None:
            return "UNVERIFIABLE", "EVIDENCE_NOT_FOUND", True
        if not _valid_document_identity(document):
            if not _is_sha256(document.document_content_sha256):
                return "CONTRADICTED", "EVIDENCE_HASH_MISMATCH", True
            return "UNVERIFIABLE", "EVIDENCE_NOT_FOUND", True
        if document.release_id != package.release_id:
            return "CONTRADICTED", "EVIDENCE_RELEASE_MISMATCH", True
        if (
            ref.content_sha256 != document.excerpt_sha256
            or _sha256(document.excerpt) != document.excerpt_sha256
        ):
            return "CONTRADICTED", "EVIDENCE_HASH_MISMATCH", True
        if not _valid_locator(document):
            return "CONTRADICTED", "EVIDENCE_LOCATOR_INVALID", True
        if not _scope_matches(package.scope, document, today):
            return "CONTRADICTED", "EVIDENCE_SCOPE_MISMATCH", True
        documents.append((ref, document))

    if not documents:
        return "UNVERIFIABLE", "CLAIM_UNSUPPORTED", True
    if _claim_supported_by_documents(claim, documents):
        return "VERIFIED", "CLAIM_VERIFIED", True
    if entailment_verifier is None:
        return "UNVERIFIABLE", "CLAIM_UNSUPPORTED", True
    if not entailment_verifier.verifier_version:
        return "UNVERIFIABLE", "VERIFIER_VERSION_MISSING", True
    contradicted = False
    for _ref, document in documents:
        decision = entailment_verifier.verify(claim, document)
        if decision is True:
            return "VERIFIED", "CLAIM_VERIFIED", False
        if decision is False:
            contradicted = True
    if contradicted:
        return "CONTRADICTED", "CLAIM_UNSUPPORTED", False
    return "UNVERIFIABLE", "CLAIM_UNSUPPORTED", False


def _verify_derived_claim(
    package: EvidencePackage,
    claim: Claim,
    refs: tuple[EvidenceRef, ...],
    registry: dict[str, EvidenceRef],
    derived_recomputer: DerivedEvidenceRecomputer | None,
) -> tuple[str, str, bool]:
    if not refs:
        return "UNVERIFIABLE", "CLAIM_UNSUPPORTED", True
    if not _valid_derived_claim_identity(claim):
        return "UNVERIFIABLE", "DERIVED_IDENTITY_MISSING", True
    if _derived_claim_exceeds_boundary(claim):
        return "CONTRADICTED", "DERIVED_PROMISE_OUT_OF_BOUNDS", True

    for ref in refs:
        if not _valid_evidence_ref(ref):
            return "UNVERIFIABLE", "EVIDENCE_NOT_FOUND", True
        registered = registry.get(ref.evidence_id)
        if registered is None or not _valid_evidence_ref(registered):
            return "UNVERIFIABLE", "EVIDENCE_NOT_FOUND", True
        if (
            ref.release_id != package.release_id
            or registered.release_id != package.release_id
        ):
            return "CONTRADICTED", "EVIDENCE_RELEASE_MISMATCH", True
        if (
            ref.evidence_type != "DERIVED"
            or registered.evidence_type != "DERIVED"
        ):
            return "UNVERIFIABLE", "CLAIM_UNSUPPORTED", True
        if registered.content_sha256 != ref.content_sha256:
            return "CONTRADICTED", "EVIDENCE_HASH_MISMATCH", True
        evidence = registered.derived
        if evidence is None or not _valid_derived_identity(evidence):
            return "UNVERIFIABLE", "EVIDENCE_NOT_FOUND", True
        if derived_recomputer is not None:
            recomputed = derived_recomputer.recompute(evidence)
            if recomputed is not None:
                if not _valid_derived_identity(recomputed):
                    return "UNVERIFIABLE", "EVIDENCE_NOT_FOUND", True
                if not _same_derived_identity(evidence, recomputed):
                    return "CONTRADICTED", "DERIVED_INPUT_MISMATCH", True
                evidence = recomputed
        if evidence.release_id != package.release_id:
            return "CONTRADICTED", "EVIDENCE_RELEASE_MISMATCH", True
        if evidence.profile_version != package.profile_version:
            return "CONTRADICTED", "DERIVED_INPUT_MISMATCH", True
        if evidence.output_field != claim.normalized.predicate:
            return "CONTRADICTED", "DERIVED_VALUE_MISMATCH", True
        if (
            _canonical_scalar(evidence.output_value)
            != _canonical_scalar(claim.normalized.object)
            or evidence.unit != claim.normalized.unit
            or evidence.precision != claim.normalized.precision
        ):
            return "CONTRADICTED", "DERIVED_VALUE_MISMATCH", True
        if (
            _normalize_sample_years(evidence.sample_years)
            != _normalize_sample_years(claim.normalized.sample_years)
        ):
            return "CONTRADICTED", "DERIVED_VALUE_MISMATCH", True
        if not _derived_render_bindings_match(claim, evidence):
            return "CONTRADICTED", "CLAIM_RENDER_MISMATCH", True
    return "VERIFIED", "CLAIM_VERIFIED", True


def _package_preflight(package: EvidencePackage) -> str | None:
    if not package.verifier_version:
        return "VERIFIER_VERSION_MISSING"
    if not package.release_id or not package.profile_version:
        return "CLAIM_UNSUPPORTED"
    if not package.result_version:
        return "CLAIM_UNSUPPORTED"
    if (
        not package.scope.province
        or package.scope.application_year is None
        or not package.scope.category
        or not package.scope.batch
    ):
        return "EVIDENCE_SCOPE_MISMATCH"
    registry_refs = package.evidence_registry
    claim_refs = tuple(
        ref
        for claim in package.claims
        for ref in claim.evidence_refs
    )
    for ref in (*registry_refs, *claim_refs):
        if ref.release_id != package.release_id:
            return "EVIDENCE_RELEASE_MISMATCH"
        if (
            ref.document is not None
            and ref.document.release_id != package.release_id
        ):
            return "EVIDENCE_RELEASE_MISMATCH"
        if (
            ref.derived is not None
            and ref.derived.release_id != package.release_id
        ):
            return "EVIDENCE_RELEASE_MISMATCH"
    return None


def _claim_preflight_reason(claim: Claim) -> str | None:
    if not claim.claim_id or not claim.rendered_text:
        return "CLAIM_UNSUPPORTED"
    if claim.claim_type not in {
        "DOCUMENT_FACT",
        "STRUCTURED_FACT",
        "DERIVED_FACT",
    }:
        return "CLAIM_UNSUPPORTED"
    if claim.criticality not in {"CRITICAL", "NORMAL"}:
        return "CLAIM_UNSUPPORTED"
    if not claim.normalized.subject or not claim.normalized.predicate:
        return "CLAIM_UNSUPPORTED"
    return None


def _group_evidence(
    evidence: tuple[EvidenceRef, ...],
) -> dict[str, list[EvidenceRef]]:
    grouped: dict[str, list[EvidenceRef]] = {}
    for ref in sorted(evidence, key=lambda item: item.evidence_id):
        grouped.setdefault(ref.evidence_id, []).append(ref)
    return grouped


def _valid_locator(document: DocumentEvidence) -> bool:
    if not document.section_path.strip():
        return False
    if not document.excerpt:
        return False
    if (document.char_start is None) != (document.char_end is None):
        return False
    if document.char_start is None:
        return True
    return (
        document.char_start >= 0
        and document.char_end is not None
        and document.char_end >= document.char_start
        and document.char_end - document.char_start == len(document.excerpt)
    )


def _valid_evidence_ref(ref: EvidenceRef) -> bool:
    return (
        bool(ref.evidence_id)
        and bool(ref.evidence_type)
        and bool(ref.release_id)
        and _is_sha256(ref.content_sha256)
        and bool(ref.producer_version)
        and ref.created_at.tzinfo is not None
    )


def _valid_derived_identity(evidence: DerivedEvidence) -> bool:
    required = (
        evidence.operation,
        evidence.algorithm,
        evidence.algorithm_version,
        evidence.config_sha256,
        evidence.input_snapshot_sha256,
        evidence.profile_version,
        evidence.release_id,
        evidence.output_field,
    )
    if not all(value.strip() for value in required):
        return False
    if not _is_sha256(evidence.config_sha256):
        return False
    if not _is_sha256(evidence.input_snapshot_sha256):
        return False
    if not evidence.sample_years:
        return False
    if any(
        not isinstance(year, int) or isinstance(year, bool)
        for year in evidence.sample_years
    ):
        return False
    if evidence.precision is None or evidence.precision < 0:
        return False
    if _canonical_scalar(evidence.output_value)[0] == "invalid":
        return False
    return True


def _valid_derived_claim_identity(claim: Claim) -> bool:
    if not claim.normalized.sample_years:
        return False
    if any(
        not isinstance(year, int) or isinstance(year, bool)
        for year in claim.normalized.sample_years
    ):
        return False
    if claim.normalized.precision is None or claim.normalized.precision < 0:
        return False
    if _canonical_scalar(claim.normalized.object)[0] == "invalid":
        return False
    return True


def _derived_claim_exceeds_boundary(claim: Claim) -> bool:
    text = " ".join(
        (
            claim.normalized.subject,
            claim.normalized.predicate,
            str(claim.normalized.object or ""),
            claim.rendered_text,
        )
    )
    compact = re.sub(r"[\s，。！？、,.!?：:；;（）()]+", "", text)
    allowed_negations = (
        "不保证录取",
        "不能保证录取",
        "无法保证录取",
        "不确保录取",
        "不构成录取保证",
        "不构成任何录取保证",
        "不构成录取承诺",
        "不构成任何录取承诺",
        "并非录取承诺",
        "不是录取承诺",
        "非录取承诺",
        "不代表保证录取",
        "不承诺录取",
    )
    for phrase in allowed_negations:
        compact = compact.replace(phrase, "")
    forbidden = (
        "保证录取",
        "确保录取",
        "必定录取",
        "一定录取",
        "包录取",
        "100%录取",
        "百分之百录取",
    )
    return any(phrase in compact for phrase in forbidden)


def _valid_document_identity(document: DocumentEvidence) -> bool:
    required = (
        document.doc_id,
        document.chunk_id,
        document.title,
        document.document_type,
        document.issuer,
        document.issuer_type,
        document.issuer_id,
        document.source_uri,
        document.section_path,
        document.excerpt,
        document.release_id,
        document.document_content_sha256,
        document.raw_storage_uri,
        document.chunk_set_version,
    )
    if not all(value.strip() for value in required):
        return False
    if not _is_sha256(document.excerpt_sha256):
        return False
    if not _is_sha256(document.document_content_sha256):
        return False
    if document.fetched_at.tzinfo is None:
        return False
    if (
        document.issuer_type == "university"
        or document.document_type in {"charter", "university_notice"}
    ) and not document.school_id:
        return False
    return True


def _scope_matches(
    scope: VerificationScope,
    document: DocumentEvidence,
    today: date,
) -> bool:
    applicability = document.applicability
    if scope.application_year is None:
        return False
    if "全国" not in applicability.regions:
        if scope.province not in applicability.regions:
            return False
    if (
        "不限" not in applicability.categories
        and scope.category not in applicability.categories
    ):
        return False
    if (
        "不限" not in applicability.batches
        and scope.batch not in applicability.batches
    ):
        return False
    if scope.application_year < applicability.valid_from.year:
        return False
    if (
        applicability.valid_to is not None
        and scope.application_year > applicability.valid_to.year
    ):
        return False
    if not (
        applicability.valid_from <= today
        and (
            applicability.valid_to is None
            or today <= applicability.valid_to
        )
    ):
        return False
    if (
        document.school_id is not None
        and scope.school_id != document.school_id
    ):
        return False
    return True


def _claim_supported_by_documents(
    claim: Claim,
    documents: list[tuple[EvidenceRef, DocumentEvidence]],
) -> bool:
    object_text = str(claim.normalized.object)
    if object_text and not any(
        object_text in document.excerpt
        for _ref, document in documents
    ):
        return False
    by_id = {
        ref.evidence_id: document for ref, document in documents
    }
    for binding in claim.rendered_bindings:
        if not binding.rendered_text:
            return False
        document = by_id.get(binding.evidence_id)
        if document is None:
            return False
        if binding.rendered_text not in document.excerpt:
            return False
        if binding.rendered_text not in claim.rendered_text:
            return False
    return True


def _same_derived_identity(
    expected: DerivedEvidence,
    actual: DerivedEvidence,
) -> bool:
    return (
        actual.operation == expected.operation
        and actual.algorithm == expected.algorithm
        and actual.algorithm_version == expected.algorithm_version
        and actual.config_sha256 == expected.config_sha256
        and actual.input_snapshot_sha256 == expected.input_snapshot_sha256
        and actual.profile_version == expected.profile_version
        and actual.release_id == expected.release_id
        and _normalize_sample_years(actual.sample_years)
        == _normalize_sample_years(expected.sample_years)
        and actual.output_field == expected.output_field
        and actual.unit == expected.unit
        and actual.precision == expected.precision
        and actual.assumptions == expected.assumptions
        and actual.warnings == expected.warnings
    )


def _derived_render_bindings_match(
    claim: Claim,
    evidence: DerivedEvidence,
) -> bool:
    expected = {
        "normalized.object": _render_derived_value(
            claim.normalized.object,
            claim.normalized.precision,
        ),
        "normalized.unit": claim.normalized.unit or "",
        "normalized.sample_years": "、".join(
            str(year)
            for year in _normalize_sample_years(
                claim.normalized.sample_years
            )
        ),
    }
    by_path: dict[str, RenderedBinding] = {}
    for binding in claim.rendered_bindings:
        if binding.fact_path in by_path:
            return False
        by_path[binding.fact_path] = binding
    for fact_path, rendered_text in expected.items():
        binding = by_path.get(fact_path)
        if binding is None:
            return False
        if binding.evidence_id not in {
            ref.evidence_id for ref in claim.evidence_refs
        }:
            return False
        if binding.rendered_text != rendered_text:
            return False
        if rendered_text and rendered_text not in claim.rendered_text:
            return False
    return evidence.output_field == claim.normalized.predicate


def _canonical_scalar(value) -> tuple[str, object]:
    if value is None:
        return "null", None
    if isinstance(value, bool):
        return "bool", value
    if isinstance(value, str):
        try:
            return "decimal", Decimal(value)
        except InvalidOperation:
            return "string", value
    if isinstance(value, int):
        return "decimal", Decimal(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            return "invalid", None
        return "decimal", value
    if isinstance(value, float):
        if not math.isfinite(value):
            return "invalid", None
        return "decimal", Decimal(str(value))
    return "invalid", None


def _render_derived_value(value, precision: int | None) -> str:
    kind, canonical = _canonical_scalar(value)
    if kind == "decimal":
        decimal = canonical
        if precision is not None:
            return f"{decimal:.{precision}f}"
        return format(decimal.normalize(), "f")
    if kind == "bool":
        return "true" if canonical else "false"
    if kind == "null":
        return "null"
    return str(canonical)


def _normalize_sample_years(years: tuple[int, ...]) -> tuple[int, ...]:
    return tuple(sorted(set(years)))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_sha256(value: str) -> bool:
    return (
        len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
