"""证据校验运行时类型；仅供 VerifyPackage 内部与上游序列化使用。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

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
    applicable_as_of: date
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
    applicable_as_of: date
    gate_context: FormalGateContext | None = None


@dataclass(frozen=True, slots=True)
class ClaimVerification:
    claim_id: str
    status: str
    reason: str
    checked_against: tuple[str, ...]
    deterministic: bool
    criticality: str = "UNKNOWN"
    claim_type: str = "UNKNOWN_FACT"


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
