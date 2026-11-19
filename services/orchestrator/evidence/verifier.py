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
class StructuredBusinessKey:
    province: str
    year: int
    category: str
    batch: str
    university_code: str | None = None
    major_group: str | None = None
    major_code: str | None = None


@dataclass(frozen=True, slots=True)
class StructuredEvidence:
    dataset: str
    table: str
    primary_key: str
    business_key: StructuredBusinessKey
    field_path: str
    value: str | int | float | bool | None
    unit: str | None
    source_year: int | None
    precision: int | None
    row_or_snapshot_sha256: str
    release_id: str


@dataclass(frozen=True, slots=True)
class DerivedEvidence:
    operation_id: str
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
    artifact_sha256: str
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
    structured: StructuredEvidence | None = None
    derived: DerivedEvidence | None = None


@dataclass(frozen=True, slots=True)
class QualificationMajor:
    major_code: str
    status: str


@dataclass(frozen=True, slots=True)
class QualificationGroup:
    group_key: str
    status: str
    majors: tuple[QualificationMajor, ...]
    restrictions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class QualificationCoverage:
    rule_type: str
    status: str


@dataclass(frozen=True, slots=True)
class QualificationSummary:
    status: str
    coverage: tuple[QualificationCoverage, ...]
    groups: tuple[QualificationGroup, ...]
    conflict_count: int
    unknown_count: int


@dataclass(frozen=True, slots=True)
class RequiredNotice:
    notice_type: str
    normalized_text: str
    content_sha256: str
    evidence_refs: tuple[EvidenceRef, ...]


@dataclass(frozen=True, slots=True)
class FormalGateContext:
    context_version: str
    release_id: str
    profile_version: str
    result_version: str
    algorithm_config_version: str
    algorithm_config_sha256: str
    verifier_version: str
    qualification: QualificationSummary
    required_notices: tuple[RequiredNotice, ...]


@dataclass(frozen=True, slots=True)
class NormalizedFact:
    subject: str
    predicate: str
    object: str | int | float | Decimal | bool | None
    unit: str | None
    source_year: int | None = None
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
    gate_context: FormalGateContext | None = None


@dataclass(frozen=True, slots=True)
class ClaimVerification:
    claim_id: str
    status: str
    reason: str
    checked_against: tuple[str, ...]
    deterministic: bool
    criticality: str = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class VerificationReport:
    status: str
    claims: tuple[ClaimVerification, ...]
    blocking_reasons: tuple[str, ...]
    stripped_claim_ids: tuple[str, ...] = ()
    citation_coverage: float = 0.0


class EntailmentVerifier(Protocol):
    verifier_version: str

    def verify(
        self,
        claim: Claim,
        document: DocumentEvidence,
    ) -> bool | None: ...


class StructuredEvidenceAuthority(Protocol):
    def resolve(
        self,
        evidence: StructuredEvidence,
    ) -> StructuredEvidence | None: ...


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
    structured_authority: StructuredEvidenceAuthority | None = None,
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
    stripped_claim_ids: list[str] = []
    if package_reason is not None:
        blocking_reasons.add(package_reason)
    blocking_reasons.update(_gate_preflight_reasons(package))
    if duplicate_evidence_ids:
        blocking_reasons.add("CLAIM_UNSUPPORTED")

    for claim in sorted(package.claims, key=lambda item: item.claim_id):
        refs = sorted(claim.evidence_refs, key=lambda item: item.evidence_id)
        checked_against = tuple(ref.evidence_id for ref in refs)
        claim_preflight_reason = _claim_preflight_reason(claim)
        if package_reason is not None:
            decision, reason, deterministic = (
                "UNVERIFIABLE",
                package_reason,
                True,
            )
        elif claim_preflight_reason is not None:
            decision, reason, deterministic = (
                "UNVERIFIABLE",
                claim_preflight_reason,
                True,
            )
            blocking_reasons.add(reason)
        elif _claim_evidence_type_mismatch(claim, registry):
            decision, reason, deterministic = (
                "UNVERIFIABLE",
                "CLAIM_UNSUPPORTED",
                True,
            )
            blocking_reasons.add(reason)
        elif claim.claim_id in duplicated_claim_ids:
            decision, reason, deterministic = (
                "UNVERIFIABLE",
                "CLAIM_UNSUPPORTED",
                True,
            )
            blocking_reasons.add(reason)
        elif (
            claim.claim_type != "DERIVED_FACT"
            and _claim_exceeds_admission_boundary(claim)
        ):
            decision, reason, deterministic = (
                "CONTRADICTED",
                "ADMISSION_GUARANTEE_FORBIDDEN",
                True,
            )
            blocking_reasons.add(reason)
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
                structured_authority,
                derived_recomputer,
            )
        report_claims.append(
            ClaimVerification(
                claim_id=claim.claim_id,
                criticality=claim.criticality,
                status=decision,
                reason=reason,
                checked_against=checked_against,
                deterministic=deterministic,
            )
        )
        if decision == "CONTRADICTED":
            blocking_reasons.add(reason)
        elif decision == "UNVERIFIABLE":
            if claim.criticality == "CRITICAL":
                blocking_reasons.add("CRITICAL_CLAIM_UNVERIFIABLE")
            else:
                stripped_claim_ids.append(claim.claim_id)

    if not report_claims:
        blocking_reasons.add("CLAIM_UNSUPPORTED")
    candidate_stripped_claim_ids = tuple(sorted(stripped_claim_ids))
    candidate_stripped_claim_id_set = set(candidate_stripped_claim_ids)
    candidate_retained_claims = tuple(
        claim
        for claim in package.claims
        if claim.claim_id not in candidate_stripped_claim_id_set
    )
    if not candidate_retained_claims:
        blocking_reasons.add("CLAIM_UNSUPPORTED")
    if blocking_reasons:
        status = "BLOCKED"
        stripped_claim_ids = []
    elif candidate_stripped_claim_ids:
        status = "PARTIAL"
        stripped_claim_ids = list(candidate_stripped_claim_ids)
    else:
        status = "PASS"
    stripped_claim_id_set = set(stripped_claim_ids)
    retained_claims = tuple(
        claim
        for claim in package.claims
        if claim.claim_id not in stripped_claim_id_set
    )
    factual_claim_count = len(retained_claims)
    cited_claim_count = sum(
        bool(claim.evidence_refs)
        for claim in retained_claims
    )
    citation_coverage = (
        cited_claim_count / factual_claim_count
        if factual_claim_count
        else 0.0
    )
    if status == "BLOCKED":
        citation_coverage = 0.0
    return VerificationReport(
        status=status,
        claims=tuple(report_claims),
        blocking_reasons=tuple(sorted(blocking_reasons)),
        citation_coverage=citation_coverage,
        stripped_claim_ids=tuple(sorted(stripped_claim_ids)),
    )


def _verify_claim(
    package: EvidencePackage,
    claim: Claim,
    refs: tuple[EvidenceRef, ...],
    registry: dict[str, EvidenceRef],
    today: date,
    entailment_verifier: EntailmentVerifier | None,
    structured_authority: StructuredEvidenceAuthority | None,
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
    if claim.claim_type == "STRUCTURED_FACT":
        return _verify_structured_claim(
            package,
            claim,
            refs,
            registry,
            structured_authority,
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


def _verify_structured_claim(
    package: EvidencePackage,
    claim: Claim,
    refs: tuple[EvidenceRef, ...],
    registry: dict[str, EvidenceRef],
    authority: StructuredEvidenceAuthority | None,
) -> tuple[str, str, bool]:
    if not package.verifier_version:
        return "UNVERIFIABLE", "VERIFIER_VERSION_MISSING", True
    if not refs:
        return "UNVERIFIABLE", "CLAIM_UNSUPPORTED", True
    if authority is None:
        return "UNVERIFIABLE", "CLAIM_UNSUPPORTED", True

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
            ref.evidence_type != "STRUCTURED"
            or registered.evidence_type != "STRUCTURED"
        ):
            return "UNVERIFIABLE", "CLAIM_UNSUPPORTED", True
        evidence = registered.structured
        if evidence is None or not _valid_structured_identity(evidence):
            return "UNVERIFIABLE", "EVIDENCE_NOT_FOUND", True
        if evidence.release_id != package.release_id:
            return "CONTRADICTED", "EVIDENCE_RELEASE_MISMATCH", True
        if (
            ref.content_sha256 != evidence.row_or_snapshot_sha256
            or registered.content_sha256 != ref.content_sha256
        ):
            return "CONTRADICTED", "EVIDENCE_HASH_MISMATCH", True
        authoritative = authority.resolve(evidence)
        if authoritative is None:
            return "UNVERIFIABLE", "EVIDENCE_NOT_FOUND", True
        if not _valid_structured_identity(authoritative):
            return "UNVERIFIABLE", "EVIDENCE_NOT_FOUND", True
        if authoritative.release_id != package.release_id:
            return "CONTRADICTED", "EVIDENCE_RELEASE_MISMATCH", True
        if (
            authoritative.row_or_snapshot_sha256
            != evidence.row_or_snapshot_sha256
        ):
            return "CONTRADICTED", "EVIDENCE_HASH_MISMATCH", True
        if not _same_structured_identity(evidence, authoritative):
            return "CONTRADICTED", "STRUCTURED_VALUE_MISMATCH", True
        if _canonical_scalar(evidence.value) != _canonical_scalar(
            authoritative.value
        ):
            return "CONTRADICTED", "STRUCTURED_VALUE_MISMATCH", True
        if (
            evidence.unit != authoritative.unit
            or evidence.source_year != authoritative.source_year
            or evidence.precision != authoritative.precision
        ):
            return "CONTRADICTED", "STRUCTURED_VALUE_MISMATCH", True
        if not _structured_fact_matches_claim(claim, authoritative):
            return "CONTRADICTED", "STRUCTURED_VALUE_MISMATCH", True
        if not _structured_render_bindings_match(claim, authoritative):
            return "CONTRADICTED", "CLAIM_RENDER_MISMATCH", True
    return "VERIFIED", "CLAIM_VERIFIED", True


def _verify_derived_claim(
    package: EvidencePackage,
    claim: Claim,
    refs: tuple[EvidenceRef, ...],
    registry: dict[str, EvidenceRef],
    derived_recomputer: DerivedEvidenceRecomputer | None,
) -> tuple[str, str, bool]:
    if not package.verifier_version:
        return "UNVERIFIABLE", "VERIFIER_VERSION_MISSING", True
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
        evidence = registered.derived
        if evidence is None or not _valid_derived_identity(evidence):
            return "UNVERIFIABLE", "EVIDENCE_NOT_FOUND", True
        if (
            registered.content_sha256 != ref.content_sha256
            or ref.content_sha256 != evidence.artifact_sha256
        ):
            return "CONTRADICTED", "EVIDENCE_HASH_MISMATCH", True
        if evidence.release_id != package.release_id:
            return "CONTRADICTED", "EVIDENCE_RELEASE_MISMATCH", True
        if evidence.profile_version != package.profile_version:
            return "CONTRADICTED", "DERIVED_INPUT_MISMATCH", True
        if derived_recomputer is not None:
            recomputed = derived_recomputer.recompute(evidence)
            if recomputed is not None:
                if not _valid_derived_identity(recomputed):
                    return "UNVERIFIABLE", "EVIDENCE_NOT_FOUND", True
                if not _same_derived_identity(evidence, recomputed):
                    return "CONTRADICTED", "DERIVED_INPUT_MISMATCH", True
                evidence = recomputed
        if evidence.output_field != claim.normalized.predicate:
            return "CONTRADICTED", "DERIVED_VALUE_MISMATCH", True
        if (
            _canonical_derived_value(evidence.output_value)
            != _canonical_derived_value(claim.normalized.object)
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
        if (
            ref.structured is not None
            and ref.structured.release_id != package.release_id
        ):
            return "EVIDENCE_RELEASE_MISMATCH"
    return None


def _gate_preflight_reasons(
    package: EvidencePackage,
) -> set[str]:
    context = package.gate_context
    if context is None:
        return {"FORMAL_GATE_CONTEXT_MISSING"}
    if context.context_version != "formal-gate-v1":
        return {"FORMAL_GATE_CONTEXT_VERSION_UNSUPPORTED"}
    reasons: set[str] = set()
    if context.release_id != package.release_id:
        reasons.add("RELEASE_MISMATCH")
    if context.profile_version != package.profile_version:
        reasons.add("PROFILE_VERSION_MISMATCH")
    if context.result_version != package.result_version:
        reasons.add("RESULT_VERSION_MISMATCH")
    if (
        not context.algorithm_config_version
        or not _is_sha256(context.algorithm_config_sha256)
    ):
        reasons.add("ALGORITHM_CONFIG_MISMATCH")
    if context.verifier_version != package.verifier_version:
        reasons.add("VERIFIER_VERSION_MISMATCH")
    package_refs = (
        *package.evidence_registry,
        *(
            ref
            for claim in package.claims
            for ref in claim.evidence_refs
        ),
    )
    if any(
        ref.derived is not None
        and ref.derived.config_sha256
        != context.algorithm_config_sha256
        for ref in package_refs
    ):
        reasons.add("ALGORITHM_CONFIG_MISMATCH")
    reasons.update(_qualification_gate_reasons(context.qualification))
    reasons.update(_required_notice_gate_reasons(context))
    if (
        any(
            group.status == "PASS_PARTIAL"
            for group in context.qualification.groups
        )
        and _package_overstates_pass_partial(package)
    ):
        reasons.add("PASS_PARTIAL_GROUP_OVERSTATED")
    return reasons


def _required_notice_gate_reasons(
    context: FormalGateContext,
) -> set[str]:
    reasons: set[str] = set()
    required_types = {
        "DISCLAIMER",
        "RISK_NOTICE",
        "NO_ADMISSION_GUARANTEE",
    }
    notices_by_type: dict[str, list[RequiredNotice]] = {}
    for notice in context.required_notices:
        notices_by_type.setdefault(notice.notice_type, []).append(notice)
    if any(
        not notices_by_type.get(notice_type)
        for notice_type in required_types
    ):
        reasons.add("REQUIRED_NOTICE_MISSING")
    for notice_type, notices in notices_by_type.items():
        if notice_type in required_types and len(notices) != 1:
            reasons.add("REQUIRED_NOTICE_MISSING")
    for notice in context.required_notices:
        if not notice.normalized_text.strip():
            reasons.add("REQUIRED_NOTICE_MISSING")
            continue
        if not _notice_text_satisfies_type(notice):
            reasons.add("REQUIRED_NOTICE_MISSING")
        if _text_exceeds_admission_boundary(notice.normalized_text):
            reasons.add("ADMISSION_GUARANTEE_FORBIDDEN")
        if (
            not _is_sha256(notice.content_sha256)
            or _sha256(notice.normalized_text) != notice.content_sha256
        ):
            reasons.add("REQUIRED_NOTICE_MISMATCH")
        if not notice.evidence_refs:
            reasons.add("REQUIRED_NOTICE_MISSING")
            continue
        for ref in notice.evidence_refs:
            if (
                not _valid_evidence_ref(ref)
                or not _evidence_payload_release_matches(
                    ref,
                    context.release_id,
                )
            ):
                reasons.add("REQUIRED_NOTICE_MISSING")
                continue
            if not _required_notice_evidence_supports(notice, ref):
                reasons.add("REQUIRED_NOTICE_MISMATCH")
    return reasons


def _required_notice_evidence_supports(
    notice: RequiredNotice,
    ref: EvidenceRef,
) -> bool:
    document = ref.document
    if ref.evidence_type == "DOCUMENT" and document is not None:
        return (
            document.release_id == ref.release_id
            and _valid_document_identity(document)
            and _sha256(document.excerpt) == document.excerpt_sha256
            and ref.content_sha256 == document.excerpt_sha256
            and notice.normalized_text in document.excerpt
        )
    structured = ref.structured
    if ref.evidence_type == "STRUCTURED" and structured is not None:
        return (
            structured.release_id == ref.release_id
            and _valid_structured_identity(structured)
            and ref.content_sha256
            == structured.row_or_snapshot_sha256
            and _structured_notice_field_supports(notice, structured)
        )
    return False


def _structured_notice_field_supports(
    notice: RequiredNotice,
    evidence: StructuredEvidence,
) -> bool:
    notice_field = notice.notice_type.lower()
    allowed_fields = {
        "normalized_text",
        "notice_text",
        "required_notice",
        "required_notice_text",
        notice_field,
        f"notice_{notice_field}",
        f"required_notice_{notice_field}",
    }
    field_path = re.sub(
        r"[^a-z0-9]+",
        "_",
        evidence.field_path.strip().lower(),
    ).strip("_")
    if (
        field_path not in allowed_fields
        and not any(
            field_path.endswith(f"_{allowed}")
            for allowed in allowed_fields
        )
    ):
        return False
    return (
        evidence.unit is None
        and isinstance(evidence.value, str)
        and evidence.value == notice.normalized_text
    )


def _notice_text_satisfies_type(notice: RequiredNotice) -> bool:
    text = notice.normalized_text
    if notice.notice_type == "DISCLAIMER":
        return any(
            phrase in text
            for phrase in ("免责", "仅供参考", "不构成")
        )
    if notice.notice_type == "RISK_NOTICE":
        return "风险" in text
    if notice.notice_type == "NO_ADMISSION_GUARANTEE":
        return any(
            phrase in text
            for phrase in (
                "不保证录取",
                "不能保证录取",
                "无法保证录取",
                "不确保录取",
                "不构成录取保证",
                "不构成录取承诺",
                "不承诺录取",
            )
        )
    return True


def _qualification_gate_reasons(
    qualification: QualificationSummary,
) -> set[str]:
    reasons: set[str] = set()
    if qualification.status != "COMPLETE":
        reasons.add("ELIGIBILITY_NOT_COMPLETE")
    if not qualification.coverage:
        reasons.add("ELIGIBILITY_COVERAGE_MISSING")
    elif any(
        not coverage.rule_type.strip()
        or coverage.status != "FULL"
        for coverage in qualification.coverage
    ):
        reasons.add("ELIGIBILITY_COVERAGE_INCOMPLETE")
    if qualification.conflict_count != 0:
        reasons.add("ELIGIBILITY_RULE_CONFLICT")
    if qualification.unknown_count != 0:
        reasons.add("ELIGIBILITY_UNKNOWN")
    for group in qualification.groups:
        if group.status == "UNKNOWN":
            reasons.add("ELIGIBILITY_GROUP_UNKNOWN")
        elif group.status == "FAIL":
            reasons.add("ELIGIBILITY_GROUP_FAIL")
        elif group.status not in {"PASS", "PASS_PARTIAL"}:
            reasons.add("ELIGIBILITY_GROUP_STATUS_INVALID")
        if not group.group_key or not group.majors:
            reasons.add("ELIGIBILITY_GROUP_STATUS_INVALID")
        major_statuses = tuple(
            major.status
            for major in group.majors
        )
        if (
            group.status == "PASS"
            and any(status != "PASS" for status in major_statuses)
        ):
            reasons.add("ELIGIBILITY_GROUP_STATUS_INVALID")
        if (
            group.status == "PASS_PARTIAL"
            and "PASS" not in major_statuses
        ):
            reasons.add("ELIGIBILITY_GROUP_STATUS_INVALID")
        if (
            group.status == "PASS_PARTIAL"
            and (
                not group.restrictions
                or any(
                    not restriction.strip()
                    for restriction in group.restrictions
                )
            )
        ):
            reasons.add("ELIGIBILITY_RESTRICTED_MAJORS_MISSING")
        if group.status == "PASS_PARTIAL":
            restricted_major_codes = {
                major.major_code
                for major in group.majors
                if major.status in {"FAIL", "NOT_APPLICABLE"}
            }
            if (
                any(
                    not major_code.strip()
                    for major_code in restricted_major_codes
                )
                or any(
                    not any(
                        major_code in restriction
                        for restriction in group.restrictions
                    )
                    for major_code in restricted_major_codes
                )
            ):
                reasons.add("ELIGIBILITY_RESTRICTED_MAJORS_MISSING")
        for major in group.majors:
            if major.status == "MISSING_REQUIRED_FACT":
                reasons.add("ELIGIBILITY_MISSING_REQUIRED_FACT")
            elif major.status == "UNKNOWN":
                reasons.add("ELIGIBILITY_GROUP_UNKNOWN")
            elif major.status not in {
                "PASS",
                "FAIL",
                "NOT_APPLICABLE",
            }:
                reasons.add("ELIGIBILITY_MAJOR_STATUS_INVALID")
    return reasons


def _package_overstates_pass_partial(package: EvidencePackage) -> bool:
    forbidden = (
        "全组可报",
        "整组可报",
        "全组专业均可报",
        "全组专业都可报",
        "整组专业均可报",
        "整组专业都可报",
        "组内专业均可报",
        "组内专业都可报",
        "所有专业均可报",
        "所有专业都可报",
    )
    for claim in package.claims:
        text = re.sub(
            r"[\s，。！？、,.!?：:；;（）()]+",
            "",
            " ".join(
                (
                    claim.normalized.subject,
                    claim.normalized.predicate,
                    str(claim.normalized.object or ""),
                    claim.rendered_text,
                )
            ),
        )
        if any(phrase in text for phrase in forbidden):
            return True
    return False


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


def _claim_evidence_type_mismatch(
    claim: Claim,
    registry: dict[str, EvidenceRef],
) -> bool:
    expected_type = {
        "DOCUMENT_FACT": "DOCUMENT",
        "STRUCTURED_FACT": "STRUCTURED",
        "DERIVED_FACT": "DERIVED",
    }.get(claim.claim_type)
    if expected_type is None:
        return False
    return any(
        ref.evidence_type != expected_type
        or (
            (registered := registry.get(ref.evidence_id)) is not None
            and registered.evidence_type != expected_type
        )
        for ref in claim.evidence_refs
    )


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


def _evidence_payload_release_matches(
    ref: EvidenceRef,
    release_id: str,
) -> bool:
    if ref.release_id != release_id:
        return False
    return all(
        payload is None or payload.release_id == release_id
        for payload in (ref.document, ref.structured, ref.derived)
    )


def _valid_derived_identity(evidence: DerivedEvidence) -> bool:
    required = (
        evidence.operation_id,
        evidence.algorithm,
        evidence.algorithm_version,
        evidence.config_sha256,
        evidence.input_snapshot_sha256,
        evidence.profile_version,
        evidence.release_id,
        evidence.output_field,
        evidence.artifact_sha256,
    )
    if not all(value.strip() for value in required):
        return False
    if not _is_sha256(evidence.config_sha256):
        return False
    if not _is_sha256(evidence.input_snapshot_sha256):
        return False
    if not _is_sha256(evidence.artifact_sha256):
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
    if evidence.output_value is None or evidence.unit is None:
        return False
    if _canonical_derived_value(evidence.output_value)[0] == "unsupported":
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
    if claim.normalized.object is None or claim.normalized.unit is None:
        return False
    if _canonical_derived_value(claim.normalized.object)[0] == "unsupported":
        return False
    return True


def _derived_claim_exceeds_boundary(claim: Claim) -> bool:
    return _claim_exceeds_admission_boundary(claim)


def _claim_exceeds_admission_boundary(claim: Claim) -> bool:
    text = " ".join(
        (
            claim.normalized.subject,
            claim.normalized.predicate,
            str(claim.normalized.object or ""),
            claim.rendered_text,
        )
    )
    return _text_exceeds_admission_boundary(text)


def _text_exceeds_admission_boundary(text: str) -> bool:
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
    forbidden_patterns = (
        r"(?:保证|确保|承诺|必定|一定).{0,6}录取",
        r"(?:100%|百分之百).{0,2}录取",
        r"录取.{0,2}(?:保证|无忧)",
        r"包录取",
    )
    return any(
        re.search(pattern, compact)
        for pattern in forbidden_patterns
    )


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


def _valid_structured_identity(evidence: StructuredEvidence) -> bool:
    business_key = evidence.business_key
    required = (
        evidence.dataset,
        evidence.table,
        evidence.primary_key,
        evidence.field_path,
        evidence.release_id,
        business_key.province,
        business_key.category,
        business_key.batch,
    )
    if not all(value.strip() for value in required):
        return False
    if business_key.year <= 0:
        return False
    if evidence.unit == "":
        return False
    if evidence.source_year is not None and evidence.source_year <= 0:
        return False
    if evidence.precision is not None and evidence.precision < 0:
        return False
    return _is_sha256(evidence.row_or_snapshot_sha256)


def _same_structured_identity(
    evidence: StructuredEvidence,
    authoritative: StructuredEvidence,
) -> bool:
    return (
        evidence.dataset == authoritative.dataset
        and evidence.table == authoritative.table
        and evidence.primary_key == authoritative.primary_key
        and evidence.business_key == authoritative.business_key
        and evidence.field_path == authoritative.field_path
        and evidence.release_id == authoritative.release_id
        and evidence.row_or_snapshot_sha256
        == authoritative.row_or_snapshot_sha256
    )


def _structured_fact_matches_claim(
    claim: Claim,
    evidence: StructuredEvidence,
) -> bool:
    return (
        _canonical_scalar(claim.normalized.object)
        == _canonical_scalar(evidence.value)
        and claim.normalized.unit == evidence.unit
        and claim.normalized.source_year == evidence.source_year
    )


def _structured_render_bindings_match(
    claim: Claim,
    evidence: StructuredEvidence,
) -> bool:
    required = {
        "normalized.object": _render_scalar(evidence.value),
    }
    if evidence.unit is not None:
        required["normalized.unit"] = evidence.unit
    if evidence.source_year is not None:
        required["normalized.source_year"] = str(evidence.source_year)
    seen: set[str] = set()
    for binding in claim.rendered_bindings:
        expected = required.get(binding.fact_path)
        if (
            expected is None
            or binding.rendered_text != expected
            or binding.rendered_text not in claim.rendered_text
            or binding.evidence_id
            not in {ref.evidence_id for ref in claim.evidence_refs}
        ):
            return False
        seen.add(binding.fact_path)
    return seen == set(required)


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
        actual.operation_id == expected.operation_id
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


def _canonical_scalar(value) -> tuple[str, str | None]:
    if value is None:
        return ("null", None)
    if isinstance(value, bool):
        return ("bool", "true" if value else "false")
    if isinstance(value, int):
        return ("int", str(value))
    if isinstance(value, Decimal):
        if not value.is_finite():
            return ("unsupported", None)
        return ("decimal", _plain_decimal(value))
    if isinstance(value, float):
        if not math.isfinite(value):
            return ("unsupported", None)
        return ("float", _plain_decimal(Decimal(repr(value))))
    if isinstance(value, str):
        return ("str", value)
    return ("unsupported", None)


def _render_derived_value(value, precision: int | None) -> str:
    canonical_type, canonical_value = _canonical_derived_value(value)
    if canonical_type == "number":
        decimal = Decimal(canonical_value or "0")
        if precision is not None:
            return f"{decimal:.{precision}f}"
        return _plain_decimal(decimal)
    if canonical_type == "bool":
        return "true" if canonical_value == "true" else "false"
    if canonical_type == "null":
        return "null"
    return canonical_value or ""


def _canonical_derived_value(value) -> tuple[str, str | None]:
    canonical_type, canonical_value = _canonical_scalar(value)
    if canonical_type in {"int", "decimal", "float"}:
        return (
            "number",
            _plain_decimal(Decimal(canonical_value or "0")),
        )
    if canonical_type == "str" and canonical_value is not None:
        try:
            decimal = Decimal(canonical_value)
        except InvalidOperation:
            return canonical_type, canonical_value
        if not decimal.is_finite():
            return "unsupported", None
        return "number", _plain_decimal(decimal)
    return canonical_type, canonical_value


def _render_scalar(value) -> str:
    canonical_type, canonical_value = _canonical_scalar(value)
    if canonical_type == "null":
        return "null"
    if canonical_type == "unsupported":
        return canonical_value or ""
    return canonical_value or ""


def _plain_decimal(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _normalize_sample_years(years: tuple[int, ...]) -> tuple[int, ...]:
    return tuple(sorted(set(years)))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_sha256(value: str) -> bool:
    return (
        len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
