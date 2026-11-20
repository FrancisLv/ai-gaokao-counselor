"""三类 Claim 的内部确定性校验适配器。"""
from __future__ import annotations

from datetime import date
import re

from evidence.canonical import normalize_sample_years
from evidence.models import (
    Claim,
    DerivedEvidence,
    DerivedEvidenceRecomputer,
    DocumentEvidence,
    EntailmentVerifier,
    EvidencePackage,
    EvidenceRef,
    RenderedBinding,
    StructuredEvidence,
    StructuredEvidenceAuthority,
)
from evidence.normalization import (
    canonical_derived_value,
    canonical_scalar,
    claim_exceeds_admission_boundary,
    is_sha256,
    render_derived_value,
    render_scalar,
    sha256_text,
    valid_document_identity,
    valid_evidence_ref,
    valid_locator,
    valid_structured_identity,
)


def _verify_claim(
    package: EvidencePackage,
    claim: Claim,
    refs: tuple[EvidenceRef, ...],
    registry: dict[str, EvidenceRef],
    applicable_as_of: date,
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
            applicable_as_of,
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
    applicable_as_of: date,
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
        if not valid_evidence_ref(ref):
            return "UNVERIFIABLE", "EVIDENCE_NOT_FOUND", True
        registered = registry.get(ref.evidence_id)
        if registered is None:
            return "UNVERIFIABLE", "EVIDENCE_NOT_FOUND", True
        if not valid_evidence_ref(registered):
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
        if not valid_document_identity(document):
            if not is_sha256(document.document_content_sha256):
                return "CONTRADICTED", "EVIDENCE_HASH_MISMATCH", True
            return "UNVERIFIABLE", "EVIDENCE_NOT_FOUND", True
        if document.release_id != package.release_id:
            return "CONTRADICTED", "EVIDENCE_RELEASE_MISMATCH", True
        if (
            ref.content_sha256 != document.excerpt_sha256
            or sha256_text(document.excerpt) != document.excerpt_sha256
        ):
            return "CONTRADICTED", "EVIDENCE_HASH_MISMATCH", True
        if not valid_locator(document):
            return "CONTRADICTED", "EVIDENCE_LOCATOR_INVALID", True
        if not _scope_matches(
            package.scope,
            document,
            applicable_as_of,
        ):
            return "CONTRADICTED", "EVIDENCE_SCOPE_MISMATCH", True
        documents.append((ref, document))

    if not documents:
        return "UNVERIFIABLE", "CLAIM_UNSUPPORTED", True
    if _claim_supported_by_documents(claim, documents):
        return "VERIFIED", "CLAIM_VERIFIED", True
    if (
        claim.normalized.unit is not None
        or claim.normalized.source_year is not None
    ):
        return "CONTRADICTED", "CLAIM_RENDER_MISMATCH", True
    if entailment_verifier is None:
        return "UNVERIFIABLE", "CLAIM_UNSUPPORTED", True
    adapter_version = getattr(
        entailment_verifier,
        "verifier_version",
        "",
    )
    if not adapter_version:
        return "UNVERIFIABLE", "VERIFIER_VERSION_MISSING", True
    if adapter_version != package.verifier_version:
        return "UNVERIFIABLE", "VERIFIER_VERSION_MISMATCH", True
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
        if not valid_evidence_ref(ref):
            return "UNVERIFIABLE", "EVIDENCE_NOT_FOUND", True
        registered = registry.get(ref.evidence_id)
        if registered is None or not valid_evidence_ref(registered):
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
        if evidence is None or not valid_structured_identity(evidence):
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
        if not valid_structured_identity(authoritative):
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
        if canonical_scalar(evidence.value) != canonical_scalar(
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
        if not valid_evidence_ref(ref):
            return "UNVERIFIABLE", "EVIDENCE_NOT_FOUND", True
        registered = registry.get(ref.evidence_id)
        if registered is None or not valid_evidence_ref(registered):
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
            canonical_derived_value(evidence.output_value)
            != canonical_derived_value(claim.normalized.object)
            or evidence.unit != claim.normalized.unit
            or evidence.precision != claim.normalized.precision
        ):
            return "CONTRADICTED", "DERIVED_VALUE_MISMATCH", True
        if (
            normalize_sample_years(evidence.sample_years)
            != normalize_sample_years(claim.normalized.sample_years)
        ):
            return "CONTRADICTED", "DERIVED_VALUE_MISMATCH", True
        if not _derived_render_bindings_match(claim, evidence):
            return "CONTRADICTED", "CLAIM_RENDER_MISMATCH", True
    return "VERIFIED", "CLAIM_VERIFIED", True


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
    if not is_sha256(evidence.config_sha256):
        return False
    if not is_sha256(evidence.input_snapshot_sha256):
        return False
    if not is_sha256(evidence.artifact_sha256):
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
    if canonical_derived_value(evidence.output_value)[0] == "unsupported":
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
    if canonical_derived_value(claim.normalized.object)[0] == "unsupported":
        return False
    return True


def _derived_claim_exceeds_boundary(claim: Claim) -> bool:
    return claim_exceeds_admission_boundary(claim)


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
        claim.normalized.subject == _structured_subject(evidence)
        and claim.normalized.predicate == _structured_predicate(evidence)
        and canonical_scalar(claim.normalized.object)
        == canonical_scalar(evidence.value)
        and claim.normalized.unit == evidence.unit
        and claim.normalized.source_year == evidence.source_year
    )


def _structured_render_bindings_match(
    claim: Claim,
    evidence: StructuredEvidence,
) -> bool:
    required = {
        "normalized.subject": _structured_subject(evidence),
        "normalized.predicate": _structured_predicate(evidence),
        "normalized.object": render_scalar(evidence.value),
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


def _structured_subject(evidence: StructuredEvidence) -> str:
    business_key = evidence.business_key
    parts = [
        business_key.province,
        f"{business_key.year}年",
        business_key.category,
        business_key.batch,
    ]
    if business_key.university_code:
        parts.append(f"院校代码{business_key.university_code}")
    if business_key.major_group:
        parts.append(f"{business_key.major_group}专业组")
    if business_key.major_code:
        parts.append(f"专业代码{business_key.major_code}")
    return " ".join(part for part in parts if part)


def _structured_predicate(evidence: StructuredEvidence) -> str:
    field_path = re.sub(
        r"[^a-z0-9]+",
        "_",
        evidence.field_path.strip().lower(),
    ).strip("_")
    aliases = {
        "plan_count": "招生计划数",
        "tuition": "学费",
        "score": "分数",
        "rank": "位次",
        "probability": "录取概率",
        "source_year": "来源年份",
    }
    return aliases.get(field_path, field_path)


def _scope_matches(
    scope: VerificationScope,
    document: DocumentEvidence,
    as_of: date,
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
        applicability.valid_from <= as_of
        and (
            applicability.valid_to is None
            or as_of <= applicability.valid_to
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
    by_id = {
        ref.evidence_id: document for ref, document in documents
    }
    expected = {
        "normalized.object": render_scalar(claim.normalized.object),
    }
    if claim.normalized.unit is not None:
        expected["normalized.unit"] = claim.normalized.unit
    if claim.normalized.source_year is not None:
        expected["normalized.source_year"] = str(
            claim.normalized.source_year
        )
    seen: set[str] = set()
    binding_evidence_ids: set[str] = set()
    for binding in claim.rendered_bindings:
        expected_text = expected.get(binding.fact_path)
        if (
            expected_text is None
            or binding.rendered_text != expected_text
        ):
            return False
        document = by_id.get(binding.evidence_id)
        if document is None:
            return False
        if binding.rendered_text not in document.excerpt:
            return False
        if binding.rendered_text not in claim.rendered_text:
            return False
        seen.add(binding.fact_path)
        binding_evidence_ids.add(binding.evidence_id)
    return (
        seen == set(expected)
        and len(binding_evidence_ids) == 1
    )


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
        and normalize_sample_years(actual.sample_years)
        == normalize_sample_years(expected.sample_years)
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
        "normalized.object": render_derived_value(
            claim.normalized.object,
            claim.normalized.precision,
        ),
        "normalized.unit": claim.normalized.unit or "",
        "normalized.sample_years": "、".join(
            str(year)
            for year in normalize_sample_years(
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
