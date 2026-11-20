"""证据包校验的公开入口；内部策略由 claims/formal_gate 提供。"""
from __future__ import annotations

from collections import Counter
from datetime import date

from evidence.claims import (
    _claim_evidence_type_mismatch as claim_evidence_type_mismatch,
    _claim_preflight_reason as claim_preflight_reason,
    _verify_claim as verify_claim,
)
from evidence.formal_gate import gate_preflight_reasons
from evidence.models import (
    ClaimVerification,
    DerivedEvidenceRecomputer,
    EntailmentVerifier,
    EvidencePackage,
    EvidenceRef,
    StructuredEvidenceAuthority,
    VerificationReport,
)
from evidence.normalization import claim_exceeds_admission_boundary


def VerifyPackage(
    package: EvidencePackage,
    *,
    entailment_verifier: EntailmentVerifier | None = None,
    structured_authority: StructuredEvidenceAuthority | None = None,
    derived_recomputer: DerivedEvidenceRecomputer | None = None,
) -> VerificationReport:
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
    blocking_reasons.update(gate_preflight_reasons(package))
    if duplicate_evidence_ids:
        blocking_reasons.add("CLAIM_UNSUPPORTED")

    for claim in sorted(package.claims, key=lambda item: item.claim_id):
        refs = sorted(claim.evidence_refs, key=lambda item: item.evidence_id)
        checked_against = tuple(ref.evidence_id for ref in refs)
        claim_preflight = claim_preflight_reason(claim)
        if package_reason is not None:
            decision, reason, deterministic = (
                "UNVERIFIABLE",
                package_reason,
                True,
            )
        elif claim_preflight is not None:
            decision, reason, deterministic = (
                "UNVERIFIABLE",
                claim_preflight,
                True,
            )
            blocking_reasons.add(reason)
        elif claim_evidence_type_mismatch(claim, registry):
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
            and claim_exceeds_admission_boundary(claim)
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
            decision, reason, deterministic = verify_claim(
                package,
                claim,
                refs,
                registry,
                package.applicable_as_of,
                entailment_verifier,
                structured_authority,
                derived_recomputer,
            )
        report_claims.append(
            ClaimVerification(
                claim_id=claim.claim_id,
                claim_type=claim.claim_type,
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


def _package_preflight(package: EvidencePackage) -> str | None:
    if not package.verifier_version:
        return "VERIFIER_VERSION_MISSING"
    if not isinstance(package.applicable_as_of, date):
        return "CLAIM_UNSUPPORTED"
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


def _group_evidence(
    evidence: tuple[EvidenceRef, ...],
) -> dict[str, list[EvidenceRef]]:
    grouped: dict[str, list[EvidenceRef]] = {}
    for ref in sorted(evidence, key=lambda item: item.evidence_id):
        grouped.setdefault(ref.evidence_id, []).append(ref)
    return grouped
