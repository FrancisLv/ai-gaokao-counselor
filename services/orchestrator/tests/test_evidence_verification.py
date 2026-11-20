"""VerifyPackage 公开行为测试：只观察证据包校验结果。"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import unittest

from evidence import (
    Claim,
    DerivedEvidence,
    DocumentApplicability,
    DocumentEvidence,
    EvidencePackage,
    EvidenceRef,
    FormalGateContext,
    NormalizedFact,
    QualificationCoverage,
    QualificationGroup,
    QualificationMajor,
    QualificationSummary,
    RenderedBinding,
    RequiredNotice,
    StructuredBusinessKey,
    StructuredEvidence,
    VerificationScope,
    VerifyPackage,
)


def document_evidence(
    *,
    release_id: str = "rel-2025",
    school_id: str = "1001",
    valid_from: date = date(2025, 1, 1),
    valid_to: date | None = date(2025, 12, 31),
    excerpt: str = "学生可以在第二学期申请转专业。",
) -> DocumentEvidence:
    return DocumentEvidence(
        doc_id="doc-charter-1001",
        chunk_id="chunk-transfer",
        title="湖南测试大学2025年招生章程",
        document_type="charter",
        issuer="湖南测试大学",
        issuer_type="university",
        issuer_id="school-1001",
        school_id=school_id,
        source_uri="https://example.invalid/charter",
        fetched_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
        applicability=DocumentApplicability(
            regions=("湖南",),
            categories=("物理类",),
            batches=("本科批",),
            valid_from=valid_from,
            valid_to=valid_to,
        ),
        section_path="第二章 > 转专业",
        excerpt_sha256=hashlib.sha256(excerpt.encode()).hexdigest(),
        char_start=0,
        char_end=len(excerpt),
        excerpt=excerpt,
        release_id=release_id,
        document_content_sha256="a" * 64,
        raw_storage_uri="s3://bucket/charter.txt",
        chunk_set_version="chunks-v1",
    )


def document_ref(document: DocumentEvidence, *, evidence_id: str = "ev-transfer"):
    return EvidenceRef(
        evidence_id=evidence_id,
        evidence_type="DOCUMENT",
        release_id=document.release_id,
        content_sha256=document.excerpt_sha256,
        created_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
        producer_version="policy-retrieval-v1",
        document=document,
    )


def document_claim(ref: EvidenceRef, *, criticality: str = "CRITICAL") -> Claim:
    return Claim(
        claim_id="claim-transfer-policy",
        claim_type="DOCUMENT_FACT",
        criticality=criticality,
        normalized=NormalizedFact(
            subject="湖南测试大学",
            predicate="转专业政策",
            object="学生可以在第二学期申请转专业。",
            unit=None,
        ),
        rendered_text="学生可以在第二学期申请转专业。",
        evidence_refs=(ref,),
        rendered_bindings=(
            RenderedBinding(
                fact_path="normalized.object",
                rendered_text="学生可以在第二学期申请转专业。",
                evidence_id=ref.evidence_id,
            ),
        ),
    )


def required_notice(
    notice_type: str,
    normalized_text: str,
) -> RequiredNotice:
    excerpt = f"{notice_type}：{normalized_text}"
    document = replace(
        document_evidence(),
        doc_id=f"doc-{notice_type.lower()}",
        chunk_id=f"chunk-{notice_type.lower()}",
        title=f"正式结果{notice_type}",
        section_path=f"正式结果 > {notice_type}",
        excerpt=excerpt,
        excerpt_sha256=hashlib.sha256(excerpt.encode()).hexdigest(),
        char_start=0,
        char_end=len(excerpt),
    )
    ref = document_ref(
        document,
        evidence_id=f"ev-{notice_type.lower()}",
    )
    return RequiredNotice(
        notice_type=notice_type,
        normalized_text=normalized_text,
        content_sha256=hashlib.sha256(normalized_text.encode()).hexdigest(),
        evidence_refs=(ref,),
    )


def formal_gate_context(
    *,
    release_id: str = "rel-2025",
    profile_version: str = "profile-v1",
    result_version: str = "result-v1",
    verifier_version: str = "evidence-verifier-v1",
    algorithm_config_version: str = "algo-config-v1",
    algorithm_config_sha256: str = "c" * 64,
    qualification: QualificationSummary | None = None,
    notices: tuple[RequiredNotice, ...] | None = None,
) -> FormalGateContext:
    qualification_value = qualification or QualificationSummary(
        status="COMPLETE",
        coverage=(
            QualificationCoverage(
                rule_type="ADMISSION_RULES",
                status="FULL",
            ),
        ),
        groups=(
            QualificationGroup(
                group_key="1001:01",
                status="PASS",
                majors=(
                    QualificationMajor(
                        major_code="080901",
                        status="PASS",
                    ),
                ),
            ),
        ),
        conflict_count=0,
        unknown_count=0,
    )
    return FormalGateContext(
        context_version="formal-gate-v1",
        release_id=release_id,
        profile_version=profile_version,
        result_version=result_version,
        algorithm_config_version=algorithm_config_version,
        algorithm_config_sha256=algorithm_config_sha256,
        verifier_version=verifier_version,
        applicable_as_of=date(2025, 6, 1),
        qualification=qualification_value,
        required_notices=notices
        or (
            required_notice(
                "DISCLAIMER",
                "本方案仅提供志愿参考，不构成录取承诺。",
            ),
            required_notice(
                "RISK_NOTICE",
                "录取概率为历史估计，存在滑档风险。",
            ),
            required_notice(
                "NO_ADMISSION_GUARANTEE",
                "不保证录取。",
            ),
        ),
    )


def registry_with_notices(
    registry: tuple[EvidenceRef, ...],
    context: FormalGateContext,
) -> tuple[EvidenceRef, ...]:
    return (
        *registry,
        *(
            ref
            for notice in context.required_notices
            for ref in notice.evidence_refs
        ),
    )


def document_package(
    document: DocumentEvidence | None = None,
    ref: EvidenceRef | None = None,
    *,
    scope: VerificationScope | None = None,
    registry: tuple[EvidenceRef, ...] | None = None,
    claims: tuple[Claim, ...] | None = None,
) -> EvidencePackage:
    document = document or document_evidence()
    ref = ref or document_ref(document)
    context = formal_gate_context()
    registry = registry if registry is not None else (ref,)
    return EvidencePackage(
        release_id="rel-2025",
        profile_version="profile-v1",
        result_version="result-v1",
        verifier_version="evidence-verifier-v1",
        scope=scope
        or VerificationScope(
            province="湖南",
            application_year=2025,
            category="物理类",
            batch="本科批",
            school_id="1001",
        ),
        claims=claims or (document_claim(ref),),
        evidence_registry=registry_with_notices(registry, context),
        applicable_as_of=date(2025, 6, 1),
        gate_context=context,
    )


def derived_evidence(
    *,
    output_value=15860,
    unit: str | None = "名",
    sample_years=(2022, 2023, 2024),
    precision: int | None = 0,
) -> DerivedEvidence:
    return DerivedEvidence(
        operation_id="rank_convert",
        algorithm="rank_conversion",
        algorithm_version="rank-v1",
        config_sha256="c" * 64,
        input_snapshot_sha256="d" * 64,
        profile_version="profile-v1",
        release_id="rel-2025",
        sample_years=sample_years,
        output_field="rank",
        output_value=output_value,
        unit=unit,
        precision=precision,
        assumptions=("使用湖南省同科类一分一段表",),
        warnings=("位次仅用于同科类比较",),
        artifact_sha256="e" * 64,
    )


def derived_ref(
    evidence: DerivedEvidence,
    *,
    evidence_id: str = "ev-derived-rank",
):
    return EvidenceRef(
        evidence_id=evidence_id,
        evidence_type="DERIVED",
        release_id=evidence.release_id,
        content_sha256=evidence.artifact_sha256,
        created_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
        producer_version="algo-estimate-v1",
        derived=evidence,
    )


def derived_claim(ref: EvidenceRef) -> Claim:
    return Claim(
        claim_id="claim-rank",
        claim_type="DERIVED_FACT",
        criticality="CRITICAL",
        normalized=NormalizedFact(
            subject="湖南物理类考生",
            predicate="rank",
            object=15860,
            unit="名",
            sample_years=(2022, 2023, 2024),
            precision=0,
        ),
        rendered_text="考生位次为 15860 名，样本年份为 2022、2023、2024 年。",
        evidence_refs=(ref,),
        rendered_bindings=(
            RenderedBinding(
                fact_path="normalized.object",
                rendered_text="15860",
                evidence_id=ref.evidence_id,
            ),
            RenderedBinding(
                fact_path="normalized.unit",
                rendered_text="名",
                evidence_id=ref.evidence_id,
            ),
            RenderedBinding(
                fact_path="normalized.sample_years",
                rendered_text="2022、2023、2024",
                evidence_id=ref.evidence_id,
            ),
        ),
    )


def derived_package(
    evidence: DerivedEvidence,
    ref: EvidenceRef,
) -> EvidencePackage:
    context = formal_gate_context()
    return EvidencePackage(
        release_id="rel-2025",
        profile_version="profile-v1",
        result_version="result-v1",
        verifier_version="evidence-verifier-v1",
        scope=VerificationScope(
            province="湖南",
            application_year=2025,
            category="物理类",
            batch="本科批",
            school_id="1001",
        ),
        claims=(derived_claim(ref),),
        evidence_registry=registry_with_notices((ref,), context),
        applicable_as_of=date(2025, 6, 1),
        gate_context=context,
    )


class FixedEntailmentVerifier:
    def __init__(
        self,
        result,
        *,
        verifier_version="evidence-verifier-v1",
    ):
        self.result = result
        self.verifier_version = verifier_version
        self.calls = []

    def verify(self, claim, document):
        self.calls.append((claim.claim_id, document.chunk_id))
        return self.result


class FixedDerivedRecomputer:
    def __init__(self, evidence):
        self.evidence = evidence
        self.calls = []

    def recompute(self, evidence):
        self.calls.append(evidence.operation_id)
        return self.evidence


class FixedStructuredAuthority:
    def __init__(self, evidence):
        self.evidence = evidence

    def resolve(self, evidence):
        return self.evidence


def structured_evidence(
    *,
    value=12,
    unit: str | None = "人",
    source_year: int | None = 2025,
    precision: int | None = 0,
    business_key: StructuredBusinessKey | None = None,
) -> StructuredEvidence:
    return StructuredEvidence(
        dataset="enrollment_plan",
        table="enrollment_plan_major",
        primary_key="plan-2025-1001-01-080901",
        business_key=business_key
        or StructuredBusinessKey(
            province="湖南",
            year=2025,
            category="物理类",
            batch="本科批",
            university_code="1001",
            major_group="01",
            major_code="080901",
        ),
        field_path="plan_count",
        value=value,
        unit=unit,
        source_year=source_year,
        precision=precision,
        row_or_snapshot_sha256="c" * 64,
        release_id="rel-2025",
    )


def structured_ref(
    evidence: StructuredEvidence,
    *,
    evidence_id: str = "ev-plan-count",
):
    return EvidenceRef(
        evidence_id=evidence_id,
        evidence_type="STRUCTURED",
        release_id=evidence.release_id,
        content_sha256=evidence.row_or_snapshot_sha256,
        created_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
        producer_version="structured-query-v1",
        structured=evidence,
    )


def structured_claim(ref: EvidenceRef) -> Claim:
    return Claim(
        claim_id="claim-plan-count",
        claim_type="STRUCTURED_FACT",
        criticality="CRITICAL",
        normalized=NormalizedFact(
            subject=(
                "湖南 2025年 物理类 本科批 "
                "院校代码1001 01专业组 专业代码080901"
            ),
            predicate="招生计划数",
            object=12,
            unit="人",
            source_year=2025,
        ),
        rendered_text=(
            "湖南 2025年 物理类 本科批 院校代码1001 "
            "01专业组 专业代码080901 的招生计划数为 12 人，"
            "来源年份为 2025 年。"
        ),
        evidence_refs=(ref,),
        rendered_bindings=(
            RenderedBinding(
                fact_path="normalized.subject",
                rendered_text=(
                    "湖南 2025年 物理类 本科批 "
                    "院校代码1001 01专业组 专业代码080901"
                ),
                evidence_id=ref.evidence_id,
            ),
            RenderedBinding(
                fact_path="normalized.predicate",
                rendered_text="招生计划数",
                evidence_id=ref.evidence_id,
            ),
            RenderedBinding(
                fact_path="normalized.object",
                rendered_text="12",
                evidence_id=ref.evidence_id,
            ),
            RenderedBinding(
                fact_path="normalized.unit",
                rendered_text="人",
                evidence_id=ref.evidence_id,
            ),
            RenderedBinding(
                fact_path="normalized.source_year",
                rendered_text="2025",
                evidence_id=ref.evidence_id,
            ),
        ),
    )


def structured_package(
    evidence: StructuredEvidence,
    ref: EvidenceRef,
    *,
    claims: tuple[Claim, ...] | None = None,
    registry: tuple[EvidenceRef, ...] | None = None,
) -> EvidencePackage:
    context = formal_gate_context()
    registry = registry if registry is not None else (ref,)
    return EvidencePackage(
        release_id="rel-2025",
        profile_version="profile-v1",
        result_version="result-v1",
        verifier_version="evidence-verifier-v1",
        scope=VerificationScope(
            province="湖南",
            application_year=2025,
            category="物理类",
            batch="本科批",
            school_id="1001",
        ),
        claims=claims or (structured_claim(ref),),
        evidence_registry=registry_with_notices(registry, context),
        applicable_as_of=date(2025, 6, 1),
        gate_context=context,
    )


class VerifyPackageDocumentTests(unittest.TestCase):
    def test_same_release_locatable_document_claim_is_verified(self):
        document = document_evidence()
        ref = document_ref(document)
        package = document_package(document, ref)

        report = VerifyPackage(package)

        self.assertEqual(report.status, "PASS")
        self.assertEqual(report.claims[0].claim_id, "claim-transfer-policy")
        self.assertEqual(report.claims[0].status, "VERIFIED")
        self.assertEqual(report.claims[0].checked_against, ("ev-transfer",))
        self.assertTrue(report.claims[0].deterministic)
        self.assertEqual(report.blocking_reasons, ())
        self.assertEqual(report.citation_coverage, 1.0)

    def test_document_unit_and_source_year_must_be_bound_to_the_excerpt(self):
        excerpt = "该专业学费为5000元，来源年份为2023年。"
        document = document_evidence(excerpt=excerpt)
        ref = document_ref(document)
        claim = replace(
            document_claim(ref),
            normalized=replace(
                document_claim(ref).normalized,
                object=5000,
                unit="万元",
                source_year=2025,
            ),
            rendered_text="该专业学费为5000万元，来源年份为2025年。",
            rendered_bindings=(
                RenderedBinding(
                    fact_path="normalized.object",
                    rendered_text="5000",
                    evidence_id=ref.evidence_id,
                ),
            ),
        )

        report = VerifyPackage(
            document_package(document, ref, claims=(claim,)),
        )

        self.assertEqual(report.status, "BLOCKED")
        self.assertEqual(report.claims[0].status, "CONTRADICTED")

    def test_excerpt_hash_mismatch_is_contradicted(self):
        document = document_evidence()
        ref = replace(document_ref(document), content_sha256="b" * 64)
        package = document_package(document, ref)

        report = VerifyPackage(package)

        self.assertEqual(report.status, "BLOCKED")
        self.assertEqual(report.claims[0].status, "CONTRADICTED")
        self.assertEqual(
            report.claims[0].reason,
            "EVIDENCE_HASH_MISMATCH",
        )

    def test_wrong_school_or_year_is_contradicted(self):
        document = document_evidence()
        ref = document_ref(document)
        cases = (
            VerificationScope("湖南", 2025, "物理类", "本科批", "1002"),
            VerificationScope("湖南", 2025, "物理类", "本科批", None),
            VerificationScope("湖南", 2026, "物理类", "本科批", "1001"),
        )
        for scope in cases:
            with self.subTest(scope=scope):
                package = document_package(
                    document,
                    ref,
                    scope=scope,
                )
                report = VerifyPackage(
                    package,
                )
                self.assertEqual(report.claims[0].status, "CONTRADICTED")
                self.assertEqual(
                    report.claims[0].reason,
                    "EVIDENCE_SCOPE_MISMATCH",
                )

    def test_invalid_locator_and_applicability_are_contradicted(self):
        document = document_evidence()
        cases = (
            replace(document, char_end=999),
            replace(
                document,
                applicability=replace(
                    document.applicability,
                    regions=("湖北",),
                ),
            ),
            replace(
                document,
                applicability=replace(
                    document.applicability,
                    valid_from=date(2026, 1, 1),
                    valid_to=date(2026, 12, 31),
                ),
            ),
        )
        for invalid_document in cases:
            with self.subTest(document=invalid_document):
                ref = document_ref(invalid_document)
                report = VerifyPackage(
                    document_package(invalid_document, ref),
                )
                self.assertEqual(
                    report.claims[0].status,
                    "CONTRADICTED",
                )

    def test_incomplete_document_identity_is_unverifiable(self):
        document = document_evidence()
        invalid_document = replace(document, raw_storage_uri="")
        ref = document_ref(invalid_document)

        report = VerifyPackage(
            document_package(invalid_document, ref),
        )

        self.assertEqual(report.claims[0].status, "UNVERIFIABLE")
        self.assertEqual(report.claims[0].reason, "EVIDENCE_NOT_FOUND")

    def test_text_not_supported_by_excerpt_is_unverifiable(self):
        document = document_evidence()
        ref = document_ref(document)
        claim = replace(
            document_claim(ref),
            normalized=NormalizedFact(
                subject="湖南测试大学",
                predicate="毕业要求",
                object="必须通过英语六级",
                unit=None,
            ),
            rendered_text="必须通过英语六级。",
            rendered_bindings=(),
        )

        report = VerifyPackage(
            document_package(document, ref, claims=(claim,)),
        )

        self.assertEqual(report.claims[0].status, "UNVERIFIABLE")
        self.assertEqual(report.claims[0].reason, "CLAIM_UNSUPPORTED")

    def test_dangling_and_cross_release_evidence_cannot_pass(self):
        document = document_evidence()
        ref = document_ref(document)
        cases = (
            ((), "EVIDENCE_NOT_FOUND"),
            (
                (
                    replace(ref, release_id="rel-other"),
                ),
                "EVIDENCE_RELEASE_MISMATCH",
            ),
        )
        for registry, reason in cases:
            with self.subTest(reason=reason):
                package = document_package(
                    document,
                    ref,
                    registry=registry,
                )
                report = VerifyPackage(
                    package,
                )
                self.assertEqual(report.status, "BLOCKED")
                self.assertEqual(report.claims[0].reason, reason)

    def test_unreferenced_registry_evidence_cannot_mix_releases(self):
        document = document_evidence()
        ref = document_ref(document)
        other_document = document_evidence(release_id="rel-other")
        other_ref = document_ref(
            other_document,
            evidence_id="ev-unreferenced-other-release",
        )

        report = VerifyPackage(
            document_package(
                document,
                ref,
                registry=(ref, other_ref),
            ),
        )

        self.assertEqual(report.status, "BLOCKED")
        self.assertEqual(report.claims[0].status, "UNVERIFIABLE")
        self.assertEqual(
            report.claims[0].reason,
            "EVIDENCE_RELEASE_MISMATCH",
        )
        self.assertIn(
            "EVIDENCE_RELEASE_MISMATCH",
            report.blocking_reasons,
        )

    def test_missing_verifier_identity_fails_closed(self):
        document = document_evidence()
        ref = document_ref(document)
        package = replace(document_package(document, ref), verifier_version="")

        report = VerifyPackage(package)

        self.assertEqual(report.claims[0].status, "UNVERIFIABLE")
        self.assertEqual(
            report.claims[0].reason,
            "VERIFIER_VERSION_MISSING",
        )

    def test_versioned_entailment_adapter_is_nondeterministic_provenance(self):
        document = document_evidence()
        ref = document_ref(document)
        claim = replace(
            document_claim(ref),
            normalized=NormalizedFact(
                subject="湖南测试大学",
                predicate="转专业政策",
                object="允许学生转专业",
                unit=None,
            ),
            rendered_text="允许学生转专业。",
            rendered_bindings=(),
        )
        adapter = FixedEntailmentVerifier(True)

        report = VerifyPackage(
            document_package(document, ref, claims=(claim,)),
            entailment_verifier=adapter,
        )

        self.assertEqual(report.claims[0].status, "VERIFIED")
        self.assertFalse(report.claims[0].deterministic)
        self.assertEqual(adapter.calls, [("claim-transfer-policy", "chunk-transfer")])

    def test_entailment_adapter_version_must_match_the_package_verifier(self):
        document = document_evidence()
        ref = document_ref(document)
        claim = replace(
            document_claim(ref),
            normalized=NormalizedFact(
                subject="湖南测试大学",
                predicate="转专业政策",
                object="允许学生转专业",
                unit=None,
            ),
            rendered_text="允许学生转专业。",
            rendered_bindings=(),
        )
        adapter = FixedEntailmentVerifier(
            True,
            verifier_version="entailment-v1",
        )

        report = VerifyPackage(
            document_package(document, ref, claims=(claim,)),
            entailment_verifier=adapter,
        )

        self.assertEqual(report.status, "BLOCKED")
        self.assertEqual(report.claims[0].status, "UNVERIFIABLE")
        self.assertEqual(
            report.claims[0].reason,
            "VERIFIER_VERSION_MISMATCH",
        )
        self.assertEqual(adapter.calls, [])

    def test_entailment_uncertainty_is_unverifiable(self):
        document = document_evidence()
        ref = document_ref(document)
        claim = replace(
            document_claim(ref),
            normalized=NormalizedFact(
                subject="湖南测试大学",
                predicate="转专业政策",
                object="允许学生转专业",
                unit=None,
            ),
            rendered_bindings=(),
        )

        report = VerifyPackage(
            document_package(document, ref, claims=(claim,)),
            entailment_verifier=FixedEntailmentVerifier(None),
        )

        self.assertEqual(report.claims[0].status, "UNVERIFIABLE")
        self.assertFalse(report.claims[0].deterministic)

    def test_normal_unverifiable_claim_is_stripped_as_partial(self):
        document = document_evidence()
        ref = document_ref(document)
        claim = replace(
            document_claim(ref),
            claim_id="claim-normal",
            criticality="NORMAL",
            normalized=NormalizedFact(
                subject="湖南测试大学",
                predicate="毕业要求",
                object="必须通过英语六级",
                unit=None,
            ),
            rendered_text="必须通过英语六级。",
            rendered_bindings=(),
        )

        report = VerifyPackage(
            document_package(
                document,
                ref,
                claims=(document_claim(ref), claim),
            ),
        )

        self.assertEqual(report.status, "PARTIAL")
        stripped_verification = next(
            item
            for item in report.claims
            if item.claim_id == "claim-normal"
        )
        self.assertEqual(stripped_verification.status, "UNVERIFIABLE")
        self.assertEqual(stripped_verification.criticality, "NORMAL")
        self.assertEqual(report.stripped_claim_ids, ("claim-normal",))
        self.assertEqual(report.blocking_reasons, ())
        self.assertEqual(report.citation_coverage, 1.0)

    def test_normal_claim_with_mismatched_evidence_type_is_blocked(self):
        document = document_evidence()
        ref = document_ref(document)
        claim = replace(
            structured_claim(ref),
            claim_id="claim-wrong-type",
            criticality="NORMAL",
        )

        report = VerifyPackage(
            document_package(document, ref, claims=(claim,)),
        )

        self.assertEqual(report.status, "BLOCKED")
        self.assertEqual(report.claims[0].status, "UNVERIFIABLE")
        self.assertEqual(report.stripped_claim_ids, ())
        self.assertIn("CLAIM_UNSUPPORTED", report.blocking_reasons)

    def test_partial_report_coverage_counts_retained_claims(self):
        document = document_evidence()
        ref = document_ref(document)
        verified_claim = replace(
            document_claim(ref),
            claim_id="claim-verified",
        )
        stripped_claim = replace(
            document_claim(ref),
            claim_id="claim-stripped",
            criticality="NORMAL",
            normalized=NormalizedFact(
                subject="湖南测试大学",
                predicate="毕业要求",
                object="必须通过英语六级",
                unit=None,
            ),
            rendered_text="必须通过英语六级。",
            evidence_refs=(),
            rendered_bindings=(),
        )

        report = VerifyPackage(
            document_package(
                document,
                ref,
                claims=(verified_claim, stripped_claim),
            ),
        )

        self.assertEqual(report.status, "PARTIAL")
        self.assertEqual(report.stripped_claim_ids, ("claim-stripped",))
        self.assertEqual(report.citation_coverage, 1.0)

    def test_package_with_no_retained_factual_claims_is_blocked(self):
        document = document_evidence()
        ref = document_ref(document)
        claim = replace(
            document_claim(ref),
            criticality="NORMAL",
            normalized=NormalizedFact(
                subject="湖南测试大学",
                predicate="毕业要求",
                object="必须通过英语六级",
                unit=None,
            ),
            rendered_text="必须通过英语六级。",
            rendered_bindings=(),
        )

        report = VerifyPackage(
            document_package(document, ref, claims=(claim,)),
        )

        self.assertEqual(report.status, "BLOCKED")
        self.assertEqual(report.stripped_claim_ids, ())
        self.assertEqual(report.citation_coverage, 0.0)
        self.assertIn("CLAIM_UNSUPPORTED", report.blocking_reasons)

    def test_contradiction_takes_precedence_over_normal_stripping(self):
        document = document_evidence()
        ref = document_ref(document)
        contradicted_claim = replace(
            document_claim(ref),
            claim_id="claim-contradicted",
            criticality="NORMAL",
            evidence_refs=(
                replace(ref, content_sha256="b" * 64),
            ),
        )
        unverifiable_claim = replace(
            document_claim(ref),
            claim_id="claim-unverifiable",
            criticality="NORMAL",
            normalized=NormalizedFact(
                subject="湖南测试大学",
                predicate="毕业要求",
                object="必须通过英语六级",
                unit=None,
            ),
            rendered_text="必须通过英语六级。",
            evidence_refs=(),
            rendered_bindings=(),
        )

        report = VerifyPackage(
            document_package(
                document,
                ref,
                claims=(contradicted_claim, unverifiable_claim),
            ),
        )

        self.assertEqual(report.status, "BLOCKED")
        self.assertEqual(report.stripped_claim_ids, ())
        self.assertIn(
            "EVIDENCE_HASH_MISMATCH",
            report.blocking_reasons,
        )

    def test_critical_unverifiable_claim_has_stable_blocking_reason(self):
        document = document_evidence()
        ref = document_ref(document)
        claim = replace(
            document_claim(ref),
            normalized=NormalizedFact(
                subject="湖南测试大学",
                predicate="毕业要求",
                object="必须通过英语六级",
                unit=None,
            ),
            rendered_text="必须通过英语六级。",
            rendered_bindings=(),
        )

        report = VerifyPackage(
            document_package(document, ref, claims=(claim,)),
        )

        self.assertEqual(report.status, "BLOCKED")
        self.assertEqual(report.claims[0].criticality, "CRITICAL")
        self.assertEqual(report.claims[0].status, "UNVERIFIABLE")
        self.assertEqual(report.stripped_claim_ids, ())
        self.assertIn(
            "CRITICAL_CLAIM_UNVERIFIABLE",
            report.blocking_reasons,
        )

    def test_repeated_verification_is_stable(self):
        document = document_evidence()
        ref = document_ref(document)
        package = document_package(document, ref)

        first = VerifyPackage(package)
        second = VerifyPackage(package)

        self.assertEqual(first, second)

    def test_applicability_uses_the_versioned_package_date(self):
        document = document_evidence()
        ref = document_ref(document)
        package = replace(
            document_package(document, ref),
            applicable_as_of=date(2025, 6, 1),
        )

        first = VerifyPackage(package)
        second = VerifyPackage(package)
        expired = VerifyPackage(
            replace(package, applicable_as_of=date(2027, 1, 1))
        )

        self.assertEqual(first, second)
        self.assertEqual(first.status, "PASS")
        self.assertEqual(expired.status, "BLOCKED")
        self.assertIn(
            "EVIDENCE_SCOPE_MISMATCH",
            expired.blocking_reasons,
        )

    def test_claim_and_evidence_output_order_is_stable(self):
        document = document_evidence()
        ref = document_ref(document)
        first_claim = replace(
            document_claim(ref),
            claim_id="claim-a",
        )
        second_claim = replace(
            document_claim(ref),
            claim_id="claim-z",
        )
        package = document_package(
            document,
            ref,
            claims=(second_claim, first_claim),
        )

        report = VerifyPackage(package)

        self.assertEqual(
            tuple(item.claim_id for item in report.claims),
            ("claim-a", "claim-z"),
        )

    def test_duplicate_claim_identity_cannot_pass(self):
        document = document_evidence()
        ref = document_ref(document)
        claim = document_claim(ref)
        package = document_package(
            document,
            ref,
            claims=(claim, claim),
        )

        report = VerifyPackage(package)

        self.assertEqual(report.status, "BLOCKED")
        self.assertTrue(
            all(item.status != "VERIFIED" for item in report.claims)
        )

    def test_duplicate_normal_claim_identity_cannot_be_stripped(self):
        document = document_evidence()
        ref = document_ref(document)
        claim = replace(
            document_claim(ref),
            claim_id="claim-duplicate",
            criticality="NORMAL",
        )

        report = VerifyPackage(
            document_package(
                document,
                ref,
                claims=(claim, claim),
            ),
        )

        self.assertEqual(report.status, "BLOCKED")
        self.assertEqual(report.stripped_claim_ids, ())
        self.assertIn("CLAIM_UNSUPPORTED", report.blocking_reasons)

    def test_invalid_criticality_cannot_be_stripped(self):
        document = document_evidence()
        ref = document_ref(document)
        malformed_claim = replace(
            document_claim(ref),
            claim_id="claim-malformed",
            criticality="UNKNOWN",
        )

        report = VerifyPackage(
            document_package(
                document,
                ref,
                claims=(document_claim(ref), malformed_claim),
            ),
        )

        self.assertEqual(report.status, "BLOCKED")
        self.assertEqual(report.stripped_claim_ids, ())
        self.assertIn("CLAIM_UNSUPPORTED", report.blocking_reasons)

    def test_duplicate_evidence_identity_cannot_be_stripped(self):
        document = document_evidence()
        ref = document_ref(document)
        duplicate_ref = document_ref(
            document,
            evidence_id="ev-duplicate",
        )
        normal_claim = replace(
            document_claim(duplicate_ref),
            claim_id="claim-normal",
            criticality="NORMAL",
        )

        report = VerifyPackage(
            document_package(
                document,
                ref,
                registry=(
                    ref,
                    duplicate_ref,
                    replace(
                        duplicate_ref,
                        producer_version="policy-retrieval-v2",
                    ),
                ),
                claims=(
                    replace(
                        document_claim(ref),
                        claim_id="claim-critical",
                    ),
                    normal_claim,
                ),
            ),
        )

        self.assertEqual(report.status, "BLOCKED")
        self.assertEqual(report.stripped_claim_ids, ())
        self.assertIn("CLAIM_UNSUPPORTED", report.blocking_reasons)


class VerifyPackageStructuredTests(unittest.TestCase):
    def test_same_release_structured_fact_is_verified(self):
        authoritative = structured_evidence()
        ref = structured_ref(authoritative)

        report = VerifyPackage(
            structured_package(authoritative, ref),
            structured_authority=FixedStructuredAuthority(authoritative),
        )

        self.assertEqual(report.status, "PASS")
        self.assertEqual(report.claims[0].status, "VERIFIED")
        self.assertEqual(report.claims[0].checked_against, ("ev-plan-count",))

    def test_structured_claim_must_match_subject_and_predicate_semantics(self):
        authoritative = structured_evidence()
        ref = structured_ref(authoritative)
        base_claim = structured_claim(ref)
        cases = (
            replace(
                base_claim,
                normalized=replace(
                    base_claim.normalized,
                    predicate="学费",
                ),
            ),
            replace(
                base_claim,
                normalized=replace(
                    base_claim.normalized,
                    subject="湖南另一所大学 01 专业组",
                ),
            ),
        )
        for claim in cases:
            with self.subTest(claim=claim):
                report = VerifyPackage(
                    structured_package(
                        authoritative,
                        ref,
                        claims=(claim,),
                    ),
                    structured_authority=FixedStructuredAuthority(
                        authoritative
                    ),
                )

                self.assertEqual(report.status, "BLOCKED")
                self.assertEqual(
                    report.claims[0].status,
                    "CONTRADICTED",
                )

    def test_structured_value_type_unit_or_year_mismatch_is_contradicted(self):
        authoritative = structured_evidence()
        cases = (
            replace(authoritative, value=13),
            replace(authoritative, value=12.0),
            replace(authoritative, unit="万元"),
            replace(authoritative, source_year=2024),
            replace(authoritative, precision=2),
        )
        for candidate in cases:
            with self.subTest(candidate=candidate):
                ref = structured_ref(candidate)
                report = VerifyPackage(
                    structured_package(candidate, ref),
                    structured_authority=FixedStructuredAuthority(
                        authoritative
                    ),
                )
                self.assertEqual(report.status, "BLOCKED")
                self.assertEqual(
                    report.claims[0].status,
                    "CONTRADICTED",
                )
                self.assertEqual(
                    report.claims[0].reason,
                    "STRUCTURED_VALUE_MISMATCH",
                )

    def test_wrong_structured_business_key_is_contradicted(self):
        authoritative = structured_evidence()
        wrong_business_key = replace(
            authoritative.business_key,
            major_group="02",
        )
        candidate = replace(
            authoritative,
            business_key=wrong_business_key,
        )
        ref = structured_ref(candidate)

        report = VerifyPackage(
            structured_package(candidate, ref),
            structured_authority=FixedStructuredAuthority(authoritative),
        )

        self.assertEqual(report.status, "BLOCKED")
        self.assertEqual(report.claims[0].status, "CONTRADICTED")
        self.assertEqual(
            report.claims[0].reason,
            "STRUCTURED_VALUE_MISMATCH",
        )

    def test_null_and_zero_are_not_interchangeable(self):
        cases = (
            (0, None, "null"),
            (None, 0, "0"),
        )
        for evidence_value, claimed_value, rendered_value in cases:
            with self.subTest(
                evidence_value=evidence_value,
                claimed_value=claimed_value,
            ):
                evidence = structured_evidence(value=evidence_value)
                ref = structured_ref(evidence)
                claim = replace(
                    structured_claim(ref),
                    normalized=NormalizedFact(
                        subject="湖南测试大学 01 专业组",
                        predicate="招生计划数",
                        object=claimed_value,
                        unit="人",
                        source_year=2025,
                    ),
                    rendered_text=(
                        f"该专业组计划招生 {rendered_value} 人，"
                        "来源年份为 2025 年。"
                    ),
                    rendered_bindings=(
                        replace(
                            structured_claim(ref).rendered_bindings[0],
                            rendered_text=rendered_value,
                        ),
                        *structured_claim(ref).rendered_bindings[1:],
                    ),
                )
                report = VerifyPackage(
                    structured_package(evidence, ref, claims=(claim,)),
                    structured_authority=FixedStructuredAuthority(
                        evidence
                    ),
                )
                self.assertEqual(report.status, "BLOCKED")
                self.assertEqual(
                    report.claims[0].status,
                    "CONTRADICTED",
                )

    def test_structured_claim_requires_complete_render_bindings(self):
        authoritative = structured_evidence()
        ref = structured_ref(authoritative)
        claim = structured_claim(ref)
        cases = (
            replace(
                claim,
                rendered_bindings=claim.rendered_bindings[:-1],
            ),
            replace(
                claim,
                rendered_bindings=(
                    replace(
                        claim.rendered_bindings[0],
                        rendered_text="13",
                    ),
                    *claim.rendered_bindings[1:],
                ),
            ),
            replace(
                claim,
                rendered_bindings=(
                    replace(
                        claim.rendered_bindings[0],
                        evidence_id="ev-other",
                    ),
                    *claim.rendered_bindings[1:],
                ),
            ),
        )
        for invalid_claim in cases:
            with self.subTest(claim=invalid_claim):
                report = VerifyPackage(
                    structured_package(
                        authoritative,
                        ref,
                        claims=(invalid_claim,),
                    ),
                    structured_authority=FixedStructuredAuthority(
                        authoritative
                    ),
                )
                self.assertEqual(report.status, "BLOCKED")
                self.assertEqual(
                    report.claims[0].status,
                    "CONTRADICTED",
                )
                self.assertEqual(
                    report.claims[0].reason,
                    "CLAIM_RENDER_MISMATCH",
                )

    def test_authoritative_structured_release_must_match(self):
        candidate = structured_evidence()
        ref = structured_ref(candidate)
        authoritative = replace(candidate, release_id="rel-other")

        report = VerifyPackage(
            structured_package(candidate, ref),
            structured_authority=FixedStructuredAuthority(authoritative),
        )

        self.assertEqual(report.status, "BLOCKED")
        self.assertEqual(report.claims[0].status, "CONTRADICTED")
        self.assertEqual(
            report.claims[0].reason,
            "EVIDENCE_RELEASE_MISMATCH",
        )

    def test_authoritative_row_snapshot_hash_must_match(self):
        candidate = structured_evidence()
        ref = structured_ref(candidate)
        authoritative = replace(
            candidate,
            row_or_snapshot_sha256="d" * 64,
        )

        report = VerifyPackage(
            structured_package(candidate, ref),
            structured_authority=FixedStructuredAuthority(authoritative),
        )

        self.assertEqual(report.status, "BLOCKED")
        self.assertEqual(report.claims[0].status, "CONTRADICTED")
        self.assertEqual(
            report.claims[0].reason,
            "EVIDENCE_HASH_MISMATCH",
        )

    def test_structured_evidence_requires_typed_payload_and_identity(self):
        evidence = structured_evidence()
        ref = structured_ref(evidence)
        cases = (
            replace(ref, structured=None),
            replace(
                ref,
                structured=replace(evidence, field_path=""),
            ),
            replace(
                ref,
                structured=replace(
                    evidence,
                    business_key=replace(
                        evidence.business_key,
                        year=0,
                    ),
                ),
            ),
        )
        for invalid_ref in cases:
            with self.subTest(ref=invalid_ref):
                report = VerifyPackage(
                    structured_package(
                        evidence,
                        ref,
                        claims=(structured_claim(ref),),
                        registry=(invalid_ref,),
                    ),
                    structured_authority=FixedStructuredAuthority(evidence),
                )
                self.assertEqual(report.status, "BLOCKED")
                self.assertEqual(
                    report.claims[0].status,
                    "UNVERIFIABLE",
                )
                self.assertEqual(
                    report.claims[0].reason,
                    "EVIDENCE_NOT_FOUND",
                )

    def test_missing_structured_authority_is_unverifiable(self):
        evidence = structured_evidence()
        ref = structured_ref(evidence)

        report = VerifyPackage(
            structured_package(evidence, ref),
        )

        self.assertEqual(report.status, "BLOCKED")
        self.assertEqual(report.claims[0].status, "UNVERIFIABLE")
        self.assertEqual(report.claims[0].reason, "CLAIM_UNSUPPORTED")


class VerifyPackageDerivedTests(unittest.TestCase):
    def test_missing_derived_output_value_or_unit_is_unverifiable(self):
        evidence = derived_evidence()
        ref = derived_ref(evidence)
        claim = derived_claim(ref)
        cases = (
            replace(evidence, output_value=None),
            replace(evidence, unit=None),
        )
        for invalid_evidence in cases:
            with self.subTest(evidence=invalid_evidence):
                invalid_ref = derived_ref(invalid_evidence)
                report = VerifyPackage(
                    derived_package(invalid_evidence, invalid_ref),
                )
                self.assertEqual(report.status, "BLOCKED")
                self.assertEqual(
                    report.claims[0].status,
                    "UNVERIFIABLE",
                )
                self.assertEqual(
                    report.claims[0].reason,
                    "EVIDENCE_NOT_FOUND",
                )

        claim_cases = (
            replace(claim.normalized, object=None),
            replace(claim.normalized, unit=None),
        )
        for normalized in claim_cases:
            with self.subTest(normalized=normalized):
                report = VerifyPackage(
                    replace(
                        derived_package(evidence, ref),
                        claims=(replace(claim, normalized=normalized),),
                    ),
                )
                self.assertEqual(report.status, "BLOCKED")
                self.assertEqual(
                    report.claims[0].status,
                    "UNVERIFIABLE",
                )
                self.assertEqual(
                    report.claims[0].reason,
                    "DERIVED_IDENTITY_MISSING",
                )

    def test_decimal_string_artifact_uses_canonical_numeric_value(self):
        evidence = replace(
            derived_evidence(),
            operation_id="probability",
            algorithm="weighted_hit",
            algorithm_version="weighted-hit-v1",
            output_field="probability",
            output_value="0.670",
            unit="",
            precision=2,
        )
        ref = derived_ref(evidence)
        claim = replace(
            derived_claim(ref),
            normalized=replace(
                derived_claim(ref).normalized,
                predicate="probability",
                object=0.67,
                unit="",
                precision=2,
            ),
            rendered_text="录取概率为0.67，样本年份为2022、2023、2024。",
            rendered_bindings=(
                RenderedBinding(
                    fact_path="normalized.object",
                    rendered_text="0.67",
                    evidence_id=ref.evidence_id,
                ),
                RenderedBinding(
                    fact_path="normalized.unit",
                    rendered_text="",
                    evidence_id=ref.evidence_id,
                ),
                RenderedBinding(
                    fact_path="normalized.sample_years",
                    rendered_text="2022、2023、2024",
                    evidence_id=ref.evidence_id,
                ),
            ),
        )

        report = VerifyPackage(
            replace(derived_package(evidence, ref), claims=(claim,)),
        )

        self.assertEqual(report.status, "PASS")
        self.assertEqual(report.claims[0].status, "VERIFIED")

    def test_derived_evidence_must_be_resolvable_typed_and_same_release(self):
        evidence = derived_evidence()
        ref = derived_ref(evidence)
        other_release = replace(
            evidence,
            release_id="rel-other",
        )
        other_ref = replace(
            ref,
            release_id="rel-other",
            derived=other_release,
        )
        document = document_evidence()
        document_evidence_ref = document_ref(document)
        cases = (
            (
                replace(
                    derived_package(evidence, ref),
                    evidence_registry=(),
                ),
                "EVIDENCE_NOT_FOUND",
            ),
            (
                replace(
                    derived_package(evidence, ref),
                    evidence_registry=(
                        replace(ref, derived=None),
                    ),
                ),
                "EVIDENCE_NOT_FOUND",
            ),
            (
                derived_package(other_release, other_ref),
                "EVIDENCE_RELEASE_MISMATCH",
            ),
            (
                replace(
                    derived_package(evidence, ref),
                    claims=(derived_claim(document_evidence_ref),),
                    evidence_registry=(document_evidence_ref,),
                ),
                "CLAIM_UNSUPPORTED",
            ),
        )

        for package, reason in cases:
            with self.subTest(reason=reason):
                report = VerifyPackage(
                    package,
                )
                self.assertEqual(report.status, "BLOCKED")
                self.assertEqual(
                    report.claims[0].status,
                    "UNVERIFIABLE",
                )
                self.assertEqual(report.claims[0].reason, reason)

    def test_derived_rendered_bindings_must_match_canonical_identity(self):
        evidence = derived_evidence()
        ref = derived_ref(evidence)
        claim = derived_claim(ref)
        cases = (
            replace(
                claim,
                rendered_text=claim.rendered_text.replace("15860", "16000"),
                rendered_bindings=(
                    replace(
                        claim.rendered_bindings[0],
                        rendered_text="16000",
                    ),
                    *claim.rendered_bindings[1:],
                ),
            ),
            replace(
                claim,
                rendered_bindings=claim.rendered_bindings[:-1],
            ),
            replace(
                claim,
                rendered_bindings=(
                    replace(
                        claim.rendered_bindings[0],
                        evidence_id="ev-other",
                    ),
                    *claim.rendered_bindings[1:],
                ),
            ),
        )

        for invalid_claim in cases:
            with self.subTest(claim=invalid_claim):
                report = VerifyPackage(
                    replace(
                        derived_package(evidence, ref),
                        claims=(invalid_claim,),
                    ),
                )
                self.assertEqual(report.status, "BLOCKED")
                self.assertEqual(
                    report.claims[0].status,
                    "CONTRADICTED",
                )
                self.assertEqual(
                    report.claims[0].reason,
                    "CLAIM_RENDER_MISMATCH",
                )

    def test_equivalent_admission_guarantee_is_out_of_bounds(self):
        evidence = derived_evidence()
        ref = derived_ref(evidence)
        claim = replace(
            derived_claim(ref),
            rendered_text="我将保证你被录取。",
        )

        report = VerifyPackage(
            replace(derived_package(evidence, ref), claims=(claim,)),
        )

        self.assertEqual(report.status, "BLOCKED")
        self.assertEqual(report.claims[0].status, "CONTRADICTED")
        self.assertEqual(
            report.claims[0].reason,
            "DERIVED_PROMISE_OUT_OF_BOUNDS",
        )

    def test_negated_admission_guarantee_is_allowed(self):
        evidence = derived_evidence()
        ref = derived_ref(evidence)
        claim = replace(
            derived_claim(ref),
            rendered_text=(
                "考生位次为 15860 名，样本年份为 2022、2023、2024 年，"
                "不保证录取。"
            ),
        )

        report = VerifyPackage(
            replace(derived_package(evidence, ref), claims=(claim,)),
        )

        self.assertEqual(report.status, "PASS")
        self.assertEqual(report.claims[0].status, "VERIFIED")

    def test_repeated_derived_verification_is_deterministic(self):
        evidence = derived_evidence()
        ref = derived_ref(evidence)
        package = derived_package(evidence, ref)
        recomputer = FixedDerivedRecomputer(evidence)

        first = VerifyPackage(
            package,
            derived_recomputer=recomputer,
        )
        second = VerifyPackage(
            package,
            derived_recomputer=recomputer,
        )

        self.assertEqual(first, second)
        self.assertEqual(recomputer.calls, ["rank_convert", "rank_convert"])

    def test_nearby_float_is_not_accepted_as_the_same_probability(self):
        evidence = replace(
            derived_evidence(),
            operation_id="probability",
            algorithm="weighted_hit",
            algorithm_version="weighted-hit-v1",
            output_field="probability",
            output_value=0.67,
            unit="",
            precision=2,
        )
        ref = derived_ref(evidence)
        claim = replace(
            derived_claim(ref),
            normalized=replace(
                derived_claim(ref).normalized,
                predicate="probability",
                object=0.670001,
                unit="",
                precision=2,
            ),
        )

        report = VerifyPackage(
            replace(derived_package(evidence, ref), claims=(claim,)),
        )

        self.assertEqual(report.status, "BLOCKED")
        self.assertEqual(report.claims[0].status, "CONTRADICTED")
        self.assertEqual(
            report.claims[0].reason,
            "DERIVED_VALUE_MISMATCH",
        )

    def test_unavailable_recomputation_falls_back_to_immutable_artifact(self):
        evidence = derived_evidence()
        ref = derived_ref(evidence)
        recomputer = FixedDerivedRecomputer(None)

        report = VerifyPackage(
            derived_package(evidence, ref),
            derived_recomputer=recomputer,
        )

        self.assertEqual(report.status, "PASS")
        self.assertEqual(report.claims[0].status, "VERIFIED")
        self.assertEqual(recomputer.calls, ["rank_convert"])

    def test_successful_recomputation_verifies_the_claim(self):
        saved = replace(derived_evidence(), output_value=15000)
        ref = derived_ref(saved)
        recomputer = FixedDerivedRecomputer(
            replace(saved, output_value=15860)
        )

        report = VerifyPackage(
            derived_package(saved, ref),
            derived_recomputer=recomputer,
        )

        self.assertEqual(report.status, "PASS")
        self.assertEqual(report.claims[0].status, "VERIFIED")
        self.assertTrue(report.claims[0].deterministic)
        self.assertEqual(recomputer.calls, ["rank_convert"])

    def test_derived_values_use_canonical_decimal_comparison(self):
        evidence = replace(
            derived_evidence(),
            operation_id="probability",
            algorithm="weighted_hit",
            algorithm_version="weighted-hit-v1",
            output_field="probability",
            output_value=Decimal("0.67"),
            unit="",
            precision=2,
        )
        ref = derived_ref(evidence)
        claim = replace(
            derived_claim(ref),
            normalized=replace(
                derived_claim(ref).normalized,
                predicate="probability",
                object=0.67,
                unit="",
                precision=2,
            ),
            rendered_text="录取概率为0.67，样本年份为2022、2023、2024。",
            rendered_bindings=(
                RenderedBinding(
                    fact_path="normalized.object",
                    rendered_text="0.67",
                    evidence_id=ref.evidence_id,
                ),
                RenderedBinding(
                    fact_path="normalized.unit",
                    rendered_text="",
                    evidence_id=ref.evidence_id,
                ),
                RenderedBinding(
                    fact_path="normalized.sample_years",
                    rendered_text="2022、2023、2024",
                    evidence_id=ref.evidence_id,
                ),
            ),
        )

        report = VerifyPackage(
            replace(derived_package(evidence, ref), claims=(claim,)),
        )

        self.assertEqual(report.status, "PASS")
        self.assertEqual(report.claims[0].status, "VERIFIED")

    def test_derived_artifact_hash_must_match_evidence_ref(self):
        evidence = derived_evidence()
        ref = derived_ref(evidence)
        tampered = replace(evidence, artifact_sha256="f" * 64)

        report = VerifyPackage(
            derived_package(
                tampered,
                replace(ref, derived=tampered),
            ),
        )

        self.assertEqual(report.status, "BLOCKED")
        self.assertEqual(report.claims[0].status, "CONTRADICTED")
        self.assertEqual(
            report.claims[0].reason,
            "EVIDENCE_HASH_MISMATCH",
        )

    def test_derived_probability_cannot_assert_admission_guarantee(self):
        evidence = replace(
            derived_evidence(),
            operation_id="probability",
            algorithm="weighted_hit",
            algorithm_version="weighted-hit-v1",
            output_field="probability",
            output_value=0.67,
            unit="",
            precision=2,
        )
        ref = derived_ref(evidence)
        claim = replace(
            derived_claim(ref),
            normalized=replace(
                derived_claim(ref).normalized,
                predicate="probability",
                object=0.67,
                unit="",
                precision=2,
            ),
            rendered_text=(
                "录取概率为0.67，样本年份为2022、2023、2024，保证录取。"
            ),
            rendered_bindings=(
                RenderedBinding(
                    fact_path="normalized.object",
                    rendered_text="0.67",
                    evidence_id=ref.evidence_id,
                ),
                RenderedBinding(
                    fact_path="normalized.unit",
                    rendered_text="",
                    evidence_id=ref.evidence_id,
                ),
                RenderedBinding(
                    fact_path="normalized.sample_years",
                    rendered_text="2022、2023、2024",
                    evidence_id=ref.evidence_id,
                ),
            ),
        )

        report = VerifyPackage(
            replace(derived_package(evidence, ref), claims=(claim,)),
        )

        self.assertEqual(report.status, "BLOCKED")
        self.assertEqual(report.claims[0].status, "CONTRADICTED")
        self.assertEqual(
            report.claims[0].reason,
            "DERIVED_PROMISE_OUT_OF_BOUNDS",
        )

    def test_missing_claim_sample_year_identity_is_unverifiable(self):
        evidence = derived_evidence()
        ref = derived_ref(evidence)
        claim = derived_claim(ref)
        package = replace(
            derived_package(evidence, ref),
            claims=(
                replace(
                    claim,
                    normalized=replace(
                        claim.normalized,
                        sample_years=(),
                    ),
                    rendered_bindings=tuple(
                        binding
                        for binding in claim.rendered_bindings
                        if binding.fact_path != "normalized.sample_years"
                    ),
                ),
            ),
        )

        report = VerifyPackage(
            package,
        )

        self.assertEqual(report.status, "BLOCKED")
        self.assertEqual(report.claims[0].status, "UNVERIFIABLE")
        self.assertEqual(
            report.claims[0].reason,
            "DERIVED_IDENTITY_MISSING",
        )

    def test_available_recomputation_is_authoritative(self):
        evidence = derived_evidence()
        ref = derived_ref(evidence)
        recomputer = FixedDerivedRecomputer(
            replace(evidence, output_value=15000)
        )

        report = VerifyPackage(
            derived_package(evidence, ref),
            derived_recomputer=recomputer,
        )

        self.assertEqual(report.status, "BLOCKED")
        self.assertEqual(report.claims[0].status, "CONTRADICTED")
        self.assertEqual(
            report.claims[0].reason,
            "DERIVED_VALUE_MISMATCH",
        )
        self.assertEqual(recomputer.calls, ["rank_convert"])

    def test_saved_immutable_derived_artifact_is_verified(self):
        evidence = derived_evidence()
        ref = derived_ref(evidence)

        report = VerifyPackage(
            derived_package(evidence, ref),
        )

        self.assertEqual(report.status, "PASS")
        self.assertEqual(report.claims[0].status, "VERIFIED")
        self.assertEqual(
            report.claims[0].checked_against,
            ("ev-derived-rank",),
        )
        self.assertTrue(report.claims[0].deterministic)

    def test_derived_value_unit_precision_and_sample_years_must_match(self):
        evidence = derived_evidence()
        ref = derived_ref(evidence)
        claim = derived_claim(ref)
        cases = (
            replace(claim.normalized, object=15000),
            replace(claim.normalized, unit="位"),
            replace(claim.normalized, precision=2),
            replace(claim.normalized, sample_years=(2021, 2023, 2024)),
            replace(claim.normalized, predicate="probability"),
        )
        for normalized in cases:
            with self.subTest(normalized=normalized):
                report = VerifyPackage(
                    replace(
                        derived_package(evidence, ref),
                        claims=(replace(claim, normalized=normalized),),
                    ),
                )
                self.assertEqual(report.status, "BLOCKED")
                self.assertEqual(
                    report.claims[0].status,
                    "CONTRADICTED",
                )
                self.assertEqual(
                    report.claims[0].reason,
                    "DERIVED_VALUE_MISMATCH",
                )

    def test_derived_identity_fields_are_required(self):
        evidence = derived_evidence()
        cases = (
            replace(evidence, operation_id=""),
            replace(evidence, algorithm=""),
            replace(evidence, algorithm_version=""),
            replace(evidence, config_sha256=""),
            replace(evidence, input_snapshot_sha256=""),
            replace(evidence, profile_version=""),
            replace(evidence, sample_years=()),
            replace(evidence, output_field=""),
            replace(evidence, output_value=float("nan")),
            replace(evidence, precision=None),
            replace(evidence, artifact_sha256=""),
        )
        for invalid_evidence in cases:
            with self.subTest(evidence=invalid_evidence):
                ref = derived_ref(invalid_evidence)
                report = VerifyPackage(
                    derived_package(invalid_evidence, ref),
                )
                self.assertEqual(report.status, "BLOCKED")
                self.assertEqual(
                    report.claims[0].status,
                    "UNVERIFIABLE",
                )
                self.assertEqual(
                    report.claims[0].reason,
                    "EVIDENCE_NOT_FOUND",
                )

    def test_recomputed_algorithm_identity_must_match_artifact(self):
        evidence = derived_evidence()
        ref = derived_ref(evidence)
        recomputed_cases = (
            replace(evidence, operation_id="probability"),
            replace(evidence, algorithm="weighted_hit"),
            replace(evidence, algorithm_version="rank-v2"),
            replace(evidence, config_sha256="f" * 64),
            replace(evidence, input_snapshot_sha256="a" * 64),
            replace(evidence, profile_version="profile-v2"),
            replace(evidence, release_id="rel-other"),
            replace(evidence, sample_years=(2021, 2023, 2024)),
            replace(evidence, output_field="probability"),
            replace(evidence, unit="位"),
            replace(evidence, precision=2),
            replace(evidence, assumptions=("改用另一假设",)),
            replace(evidence, warnings=("改用另一告警",)),
        )
        for recomputed in recomputed_cases:
            with self.subTest(recomputed=recomputed):
                report = VerifyPackage(
                    derived_package(evidence, ref),
                    derived_recomputer=FixedDerivedRecomputer(recomputed),
                )
                self.assertEqual(report.status, "BLOCKED")
                self.assertEqual(
                    report.claims[0].status,
                    "CONTRADICTED",
                )
                self.assertEqual(
                    report.claims[0].reason,
                    "DERIVED_INPUT_MISMATCH",
                )

    def test_sample_years_are_compared_as_a_sorted_set(self):
        evidence = replace(
            derived_evidence(),
            sample_years=(2024, 2022, 2023, 2022),
        )
        ref = derived_ref(evidence)

        report = VerifyPackage(
            derived_package(evidence, ref),
        )

        self.assertEqual(report.status, "PASS")
        self.assertEqual(report.claims[0].status, "VERIFIED")


class VerifyPackageAggregationTests(unittest.TestCase):
    def test_formal_gate_context_is_required_for_a_publishable_report(self):
        document = document_evidence()
        ref = document_ref(document)
        package = document_package(document, ref)

        passed = VerifyPackage(package)
        blocked = VerifyPackage(
            replace(package, gate_context=None),
        )

        self.assertEqual(passed.status, "PASS")
        self.assertEqual(blocked.status, "BLOCKED")
        self.assertEqual(
            blocked.blocking_reasons,
            ("FORMAL_GATE_CONTEXT_MISSING",),
        )

    def test_formal_gate_identities_must_match_the_package(self):
        document = document_evidence()
        ref = document_ref(document)
        package = document_package(document, ref)
        context = package.gate_context
        self.assertIsNotNone(context)

        cases = (
            (
                "unsupported context version",
                replace(context, context_version="formal-gate-v2"),
                "FORMAL_GATE_CONTEXT_VERSION_UNSUPPORTED",
            ),
            (
                "release",
                replace(context, release_id="rel-other"),
                "RELEASE_MISMATCH",
            ),
            (
                "profile",
                replace(context, profile_version="profile-v2"),
                "PROFILE_VERSION_MISMATCH",
            ),
            (
                "result",
                replace(context, result_version="result-v2"),
                "RESULT_VERSION_MISMATCH",
            ),
            (
                "algorithm version",
                replace(context, algorithm_config_version=""),
                "ALGORITHM_CONFIG_MISMATCH",
            ),
            (
                "verifier",
                replace(context, verifier_version="evidence-verifier-v2"),
                "VERIFIER_VERSION_MISMATCH",
            ),
        )

        for label, invalid_context, reason in cases:
            with self.subTest(label=label):
                report = VerifyPackage(
                    replace(package, gate_context=invalid_context),
                )

                self.assertEqual(report.status, "BLOCKED")
                self.assertIn(reason, report.blocking_reasons)

    def test_derived_evidence_must_use_the_gate_algorithm_config(self):
        evidence = derived_evidence()
        ref = derived_ref(evidence)
        package = derived_package(evidence, ref)
        context = package.gate_context
        self.assertIsNotNone(context)

        report = VerifyPackage(
            replace(
                package,
                gate_context=replace(
                    context,
                    algorithm_config_sha256="b" * 64,
                ),
            ),
        )

        self.assertEqual(report.status, "BLOCKED")
        self.assertIn(
            "ALGORITHM_CONFIG_MISMATCH",
            report.blocking_reasons,
        )

    def test_formal_gate_requires_complete_full_coverage_eligibility(self):
        document = document_evidence()
        ref = document_ref(document)
        package = document_package(document, ref)
        context = package.gate_context
        self.assertIsNotNone(context)
        qualification = context.qualification

        cases = (
            (
                "partial result",
                replace(qualification, status="PARTIAL"),
                "ELIGIBILITY_NOT_COMPLETE",
            ),
            (
                "blocked result",
                replace(qualification, status="BLOCKED"),
                "ELIGIBILITY_NOT_COMPLETE",
            ),
            (
                "missing coverage",
                replace(qualification, coverage=()),
                "ELIGIBILITY_COVERAGE_MISSING",
            ),
            (
                "partial coverage",
                replace(
                    qualification,
                    coverage=(
                        QualificationCoverage(
                            rule_type="ADMISSION_RULES",
                            status="PARTIAL",
                        ),
                    ),
                ),
                "ELIGIBILITY_COVERAGE_INCOMPLETE",
            ),
            (
                "rule conflict",
                replace(qualification, conflict_count=1),
                "ELIGIBILITY_RULE_CONFLICT",
            ),
            (
                "unknown diagnostics",
                replace(qualification, unknown_count=1),
                "ELIGIBILITY_UNKNOWN",
            ),
        )

        for label, invalid_qualification, reason in cases:
            with self.subTest(label=label):
                report = VerifyPackage(
                    replace(
                        package,
                        gate_context=replace(
                            context,
                            qualification=invalid_qualification,
                        ),
                    ),
                )

                self.assertEqual(report.status, "BLOCKED")
                self.assertIn(reason, report.blocking_reasons)

    def test_formal_gate_rejects_blank_coverage_rule_type(self):
        document = document_evidence()
        ref = document_ref(document)
        package = document_package(document, ref)
        context = package.gate_context
        self.assertIsNotNone(context)

        report = VerifyPackage(
            replace(
                package,
                gate_context=replace(
                    context,
                    qualification=replace(
                        context.qualification,
                        coverage=(
                            QualificationCoverage(
                                rule_type=" \t",
                                status="FULL",
                            ),
                        ),
                    ),
                ),
            ),
        )

        self.assertEqual(report.status, "BLOCKED")
        self.assertEqual(
            report.blocking_reasons,
            ("ELIGIBILITY_COVERAGE_INCOMPLETE",),
        )

    def test_formal_gate_rejects_unknown_failed_or_missing_major_groups(self):
        document = document_evidence()
        ref = document_ref(document)
        package = document_package(document, ref)
        context = package.gate_context
        self.assertIsNotNone(context)
        qualification = context.qualification

        cases = (
            (
                "unknown group",
                QualificationGroup(
                    group_key="1001:01",
                    status="UNKNOWN",
                    majors=(
                        QualificationMajor(
                            major_code="080901",
                            status="UNKNOWN",
                        ),
                    ),
                ),
                "ELIGIBILITY_GROUP_UNKNOWN",
            ),
            (
                "failed group",
                QualificationGroup(
                    group_key="1001:01",
                    status="FAIL",
                    majors=(
                        QualificationMajor(
                            major_code="080901",
                            status="FAIL",
                        ),
                    ),
                    restrictions=("080901 不满足选科要求",),
                ),
                "ELIGIBILITY_GROUP_FAIL",
            ),
            (
                "missing required fact",
                QualificationGroup(
                    group_key="1001:01",
                    status="PASS",
                    majors=(
                        QualificationMajor(
                            major_code="080901",
                            status="MISSING_REQUIRED_FACT",
                        ),
                    ),
                ),
                "ELIGIBILITY_MISSING_REQUIRED_FACT",
            ),
            (
                "unknown major under pass group",
                QualificationGroup(
                    group_key="1001:01",
                    status="PASS",
                    majors=(
                        QualificationMajor(
                            major_code="080901",
                            status="UNKNOWN",
                        ),
                    ),
                ),
                "ELIGIBILITY_GROUP_UNKNOWN",
            ),
        )

        for label, group, reason in cases:
            with self.subTest(label=label):
                report = VerifyPackage(
                    replace(
                        package,
                        gate_context=replace(
                            context,
                            qualification=replace(
                                qualification,
                                groups=(group,),
                            ),
                        ),
                    ),
                )

                self.assertEqual(report.status, "BLOCKED")
                self.assertIn(reason, report.blocking_reasons)

    def test_complete_pass_partial_requires_restricted_major_explanation(self):
        document = document_evidence()
        ref = document_ref(document)
        package = document_package(document, ref)
        context = package.gate_context
        self.assertIsNotNone(context)
        partial_group = QualificationGroup(
            group_key="1001:01",
            status="PASS_PARTIAL",
            majors=(
                QualificationMajor(
                    major_code="080901",
                    status="PASS",
                ),
                QualificationMajor(
                    major_code="080902",
                    status="FAIL",
                ),
            ),
            restrictions=("080902 不满足物理类选科要求",),
        )
        qualification = replace(
            context.qualification,
            groups=(partial_group,),
        )

        allowed = VerifyPackage(
            replace(
                package,
                gate_context=replace(
                    context,
                    qualification=qualification,
                ),
            ),
        )
        missing_restriction = VerifyPackage(
            replace(
                package,
                gate_context=replace(
                    context,
                    qualification=replace(
                        qualification,
                        groups=(
                            replace(partial_group, restrictions=()),
                        ),
                    ),
                ),
            ),
        )

        self.assertEqual(allowed.status, "PASS")
        self.assertEqual(missing_restriction.status, "BLOCKED")
        self.assertEqual(
            missing_restriction.blocking_reasons,
            ("ELIGIBILITY_RESTRICTED_MAJORS_MISSING",),
        )

    def test_pass_partial_restrictions_must_identify_restricted_majors(self):
        document = document_evidence()
        ref = document_ref(document)
        package = document_package(document, ref)
        context = package.gate_context
        self.assertIsNotNone(context)
        partial_group = QualificationGroup(
            group_key="1001:01",
            status="PASS_PARTIAL",
            majors=(
                QualificationMajor(
                    major_code="080901",
                    status="PASS",
                ),
                QualificationMajor(
                    major_code="080902",
                    status="FAIL",
                ),
            ),
            restrictions=("部分专业受限",),
        )

        report = VerifyPackage(
            replace(
                package,
                gate_context=replace(
                    context,
                    qualification=replace(
                        context.qualification,
                        groups=(partial_group,),
                    ),
                ),
            ),
        )

        self.assertEqual(report.status, "BLOCKED")
        self.assertEqual(
            report.blocking_reasons,
            ("ELIGIBILITY_RESTRICTED_MAJORS_MISSING",),
        )

    def test_pass_partial_rejects_blank_restricted_major_codes(self):
        document = document_evidence()
        ref = document_ref(document)
        package = document_package(document, ref)
        context = package.gate_context
        self.assertIsNotNone(context)

        for blank_major_code in ("", " \t"):
            with self.subTest(major_code=blank_major_code):
                partial_group = QualificationGroup(
                    group_key="1001:01",
                    status="PASS_PARTIAL",
                    majors=(
                        QualificationMajor(
                            major_code="080901",
                            status="PASS",
                        ),
                        QualificationMajor(
                            major_code=blank_major_code,
                            status="FAIL",
                        ),
                    ),
                    restrictions=(
                        f"{blank_major_code} 不满足物理类选科要求",
                    ),
                )

                report = VerifyPackage(
                    replace(
                        package,
                        gate_context=replace(
                            context,
                            qualification=replace(
                                context.qualification,
                                groups=(partial_group,),
                            ),
                        ),
                    ),
                )

                self.assertEqual(report.status, "BLOCKED")
                self.assertEqual(
                    report.blocking_reasons,
                    ("ELIGIBILITY_RESTRICTED_MAJORS_MISSING",),
                )

    def test_pass_partial_cannot_be_described_as_a_fully_eligible_group(self):
        document = document_evidence(
            excerpt="学生可以在第二学期申请转专业。该专业组全组可报。",
        )
        ref = document_ref(document)
        claim = replace(
            document_claim(ref),
            rendered_text="学生可以在第二学期申请转专业。该专业组全组可报。",
        )
        package = document_package(
            document,
            ref,
            claims=(claim,),
        )
        context = package.gate_context
        self.assertIsNotNone(context)
        partial_group = QualificationGroup(
            group_key="1001:01",
            status="PASS_PARTIAL",
            majors=(
                QualificationMajor(
                    major_code="080901",
                    status="PASS",
                ),
                QualificationMajor(
                    major_code="080902",
                    status="FAIL",
                ),
            ),
            restrictions=("080902 不满足物理类选科要求",),
        )

        report = VerifyPackage(
            replace(
                package,
                gate_context=replace(
                    context,
                    qualification=replace(
                        context.qualification,
                        groups=(partial_group,),
                    ),
                ),
            ),
        )

        self.assertEqual(report.status, "BLOCKED")
        self.assertIn(
            "PASS_PARTIAL_GROUP_OVERSTATED",
            report.blocking_reasons,
        )

    def test_formal_gate_requires_unmodified_notice_evidence(self):
        document = document_evidence()
        ref = document_ref(document)
        package = document_package(document, ref)
        context = package.gate_context
        self.assertIsNotNone(context)
        notices = context.required_notices

        cases = (
            (
                "missing no-guarantee notice",
                notices[:2],
                "REQUIRED_NOTICE_MISSING",
            ),
            (
                "notice without evidence",
                (
                    replace(notices[0], evidence_refs=()),
                    *notices[1:],
                ),
                "REQUIRED_NOTICE_MISSING",
            ),
            (
                "cross-release notice evidence",
                (
                    replace(
                        notices[0],
                        evidence_refs=(
                            replace(
                                notices[0].evidence_refs[0],
                                release_id="rel-other",
                            ),
                        ),
                    ),
                    *notices[1:],
                ),
                "REQUIRED_NOTICE_MISSING",
            ),
            (
                "rewritten notice",
                (
                    replace(
                        notices[0],
                        normalized_text=notices[0].normalized_text
                        + "可保证录取。",
                    ),
                    *notices[1:],
                ),
                "REQUIRED_NOTICE_MISMATCH",
            ),
        )

        for label, invalid_notices, reason in cases:
            with self.subTest(label=label):
                report = VerifyPackage(
                    replace(
                        package,
                        gate_context=replace(
                            context,
                            required_notices=invalid_notices,
                        ),
                    ),
                )

                self.assertEqual(report.status, "BLOCKED")
                self.assertIn(reason, report.blocking_reasons)

    def test_required_notice_labels_alone_do_not_satisfy_the_gate(self):
        document = document_evidence()
        ref = document_ref(document)
        package = document_package(document, ref)
        context = package.gate_context
        self.assertIsNotNone(context)

        report = VerifyPackage(
            replace(
                package,
                gate_context=replace(
                    context,
                    required_notices=(
                        context.required_notices[0],
                        context.required_notices[1],
                        required_notice(
                            "NO_ADMISSION_GUARANTEE",
                            "录取概率为历史估计，存在滑档风险。",
                        ),
                    ),
                ),
            ),
        )

        self.assertEqual(report.status, "BLOCKED")
        self.assertEqual(
            report.blocking_reasons,
            ("REQUIRED_NOTICE_MISSING",),
        )

    def test_required_notice_must_resolve_through_the_evidence_registry(self):
        document = document_evidence()
        ref = document_ref(document)
        package = document_package(document, ref)
        context = package.gate_context
        self.assertIsNotNone(context)
        notice = context.required_notices[0]
        package = replace(
            package,
            evidence_registry=(
                *package.evidence_registry,
                *(
                    evidence_ref
                    for required in context.required_notices
                    for evidence_ref in required.evidence_refs
                ),
            ),
            gate_context=replace(
                context,
                required_notices=(
                    replace(
                        notice,
                        evidence_refs=(
                            replace(
                                notice.evidence_refs[0],
                                evidence_id="ev-unregistered-notice",
                            ),
                        ),
                    ),
                    *context.required_notices[1:],
                ),
            ),
        )

        report = VerifyPackage(package)

        self.assertEqual(report.status, "BLOCKED")
        self.assertIn("REQUIRED_NOTICE_MISSING", report.blocking_reasons)

    def test_required_notice_rejects_unrelated_structured_evidence(self):
        document = document_evidence()
        document_ref_value = document_ref(document)
        unrelated = structured_evidence()
        unrelated_ref = structured_ref(
            unrelated,
            evidence_id="ev-unrelated-plan-count",
        )
        package = document_package(
            document,
            document_ref_value,
            registry=(document_ref_value, unrelated_ref),
        )
        context = package.gate_context
        self.assertIsNotNone(context)

        report = VerifyPackage(
            replace(
                package,
                gate_context=replace(
                    context,
                    required_notices=(
                        replace(
                            context.required_notices[0],
                            evidence_refs=(unrelated_ref,),
                        ),
                        *context.required_notices[1:],
                    ),
                ),
            ),
            structured_authority=FixedStructuredAuthority(unrelated),
        )

        self.assertEqual(report.status, "BLOCKED")
        self.assertEqual(
            report.blocking_reasons,
            ("REQUIRED_NOTICE_MISMATCH",),
        )

    def test_required_notice_requires_supported_structured_field(self):
        document = document_evidence()
        document_ref_value = document_ref(document)
        base_package = document_package(document, document_ref_value)
        context = base_package.gate_context
        self.assertIsNotNone(context)
        notice = context.required_notices[0]
        unrelated = structured_evidence()

        wrong_field = replace(
            unrelated,
            field_path="plan_count",
            value=notice.normalized_text,
            unit=None,
        )
        wrong_field_ref = structured_ref(
            wrong_field,
            evidence_id="ev-wrong-notice-field",
        )
        wrong_field_report = VerifyPackage(
            replace(
                base_package,
                evidence_registry=registry_with_notices(
                    (
                        document_ref_value,
                        wrong_field_ref,
                    ),
                    context,
                ),
                gate_context=replace(
                    context,
                    required_notices=(
                        replace(
                            notice,
                            evidence_refs=(wrong_field_ref,),
                        ),
                        *context.required_notices[1:],
                    ),
                ),
            ),
        )

        supported = replace(
            unrelated,
            dataset="formal_notice",
            table="required_notice",
            business_key=replace(
                unrelated.business_key,
                university_code=None,
                major_group=None,
                major_code=None,
            ),
            field_path="notice.disclaimer",
            value=notice.normalized_text,
            unit=None,
        )
        supported_ref = structured_ref(
            supported,
            evidence_id="ev-supported-disclaimer",
        )
        supported_report = VerifyPackage(
            replace(
                base_package,
                evidence_registry=registry_with_notices(
                    (
                        document_ref_value,
                        supported_ref,
                    ),
                    context,
                ),
                gate_context=replace(
                    context,
                    required_notices=(
                        replace(
                            notice,
                            evidence_refs=(supported_ref,),
                        ),
                        *context.required_notices[1:],
                    ),
                ),
            ),
        )

        self.assertEqual(wrong_field_report.status, "BLOCKED")
        self.assertEqual(
            wrong_field_report.blocking_reasons,
            ("REQUIRED_NOTICE_MISMATCH",),
        )
        self.assertEqual(supported_report.status, "PASS")

    def test_all_claim_types_reject_admission_guarantees(self):
        for phrase in ("保证录取", "包录取", "100%录取"):
            with self.subTest(phrase=phrase):
                excerpt = f"该专业{phrase}。"
                document = document_evidence(excerpt=excerpt)
                ref = document_ref(document)
                claim = replace(
                    document_claim(ref),
                    normalized=replace(
                        document_claim(ref).normalized,
                        predicate="录取承诺",
                        object=excerpt,
                    ),
                    rendered_text=excerpt,
                    rendered_bindings=(
                        RenderedBinding(
                            fact_path="normalized.object",
                            rendered_text=excerpt,
                            evidence_id=ref.evidence_id,
                        ),
                    ),
                )

                report = VerifyPackage(
                    document_package(
                        document,
                        ref,
                        claims=(claim,),
                    ),
                )

                self.assertEqual(report.status, "BLOCKED")
                self.assertEqual(
                    report.claims[0].status,
                    "CONTRADICTED",
                )
                self.assertEqual(
                    report.claims[0].reason,
                    "ADMISSION_GUARANTEE_FORBIDDEN",
                )

    def test_global_gate_failure_cannot_downgrade_to_publishable_partial(self):
        document = document_evidence()
        document_evidence_ref = document_ref(document)
        derived = derived_evidence()
        derived_evidence_ref = derived_ref(derived)
        normal_unknown = replace(
            derived_claim(derived_evidence_ref),
            claim_id="claim-normal-unknown",
            criticality="NORMAL",
            normalized=replace(
                derived_claim(derived_evidence_ref).normalized,
                sample_years=(),
            ),
            rendered_bindings=tuple(
                binding
                for binding in derived_claim(
                    derived_evidence_ref
                ).rendered_bindings
                if binding.fact_path != "normalized.sample_years"
            ),
        )
        package = document_package(
            document,
            document_evidence_ref,
            registry=(document_evidence_ref, derived_evidence_ref),
            claims=(
                replace(
                    document_claim(document_evidence_ref),
                    claim_id="claim-critical-document",
                ),
                normal_unknown,
            ),
        )
        context = package.gate_context
        self.assertIsNotNone(context)
        blocked_group = QualificationGroup(
            group_key="1001:01",
            status="UNKNOWN",
            majors=(
                QualificationMajor(
                    major_code="080901",
                    status="UNKNOWN",
                ),
            ),
        )

        partial = VerifyPackage(package)
        blocked = VerifyPackage(
            replace(
                package,
                gate_context=replace(
                    context,
                    qualification=replace(
                        context.qualification,
                        groups=(blocked_group,),
                    ),
                ),
            ),
        )

        self.assertEqual(partial.status, "PARTIAL")
        self.assertEqual(
            partial.stripped_claim_ids,
            ("claim-normal-unknown",),
        )
        self.assertEqual(blocked.status, "BLOCKED")
        self.assertEqual(blocked.stripped_claim_ids, ())
        self.assertIn(
            "ELIGIBILITY_GROUP_UNKNOWN",
            blocked.blocking_reasons,
        )

    def test_registry_resolved_type_mismatch_cannot_strip_normal_claim(self):
        document = document_evidence()
        document_ref_value = document_ref(document)
        verified_claim = replace(
            document_claim(document_ref_value),
            claim_id="claim-a-document",
        )

        structured = structured_evidence()
        declared_ref = structured_ref(
            structured,
            evidence_id="ev-plan-count",
        )
        registry_document_ref = document_ref(
            document,
            evidence_id=declared_ref.evidence_id,
        )
        mismatched_claim = replace(
            structured_claim(declared_ref),
            claim_id="claim-b-registry-type-mismatch",
            criticality="NORMAL",
        )
        package = document_package(
            document,
            document_ref_value,
            registry=(document_ref_value, registry_document_ref),
            claims=(verified_claim, mismatched_claim),
        )

        report = VerifyPackage(
            package,
            structured_authority=FixedStructuredAuthority(structured),
        )

        self.assertEqual(report.status, "BLOCKED")
        self.assertEqual(report.stripped_claim_ids, ())
        self.assertEqual(report.blocking_reasons, ("CLAIM_UNSUPPORTED",))
        mismatch = next(
            item
            for item in report.claims
            if item.claim_id == "claim-b-registry-type-mismatch"
        )
        self.assertEqual(mismatch.status, "UNVERIFIABLE")
        self.assertEqual(mismatch.reason, "CLAIM_UNSUPPORTED")

    def test_blocked_report_reasons_and_claims_have_stable_order(self):
        document = document_evidence()
        ref = document_ref(document)
        contradicted_claim = replace(
            document_claim(ref),
            claim_id="claim-b-contradicted",
            evidence_refs=(
                replace(ref, content_sha256="b" * 64),
            ),
        )
        mismatched_claim = replace(
            structured_claim(ref),
            claim_id="claim-a-type-mismatch",
        )
        package = document_package(
            document,
            ref,
            claims=(contradicted_claim, mismatched_claim),
        )

        first = VerifyPackage(package)
        second = VerifyPackage(package)

        self.assertEqual(first, second)
        self.assertEqual(first.status, "BLOCKED")
        self.assertEqual(
            tuple(item.claim_id for item in first.claims),
            (
                "claim-a-type-mismatch",
                "claim-b-contradicted",
            ),
        )
        self.assertEqual(
            first.blocking_reasons,
            (
                "CLAIM_UNSUPPORTED",
                "CRITICAL_CLAIM_UNVERIFIABLE",
                "EVIDENCE_HASH_MISMATCH",
            ),
        )

    def test_mixed_three_evidence_types_produce_deterministic_partial(self):
        document = document_evidence()
        document_ref_value = document_ref(document)
        document_claim_value = replace(
            document_claim(document_ref_value),
            claim_id="claim-a-document",
        )

        structured = structured_evidence()
        structured_ref_value = structured_ref(structured)
        structured_claim_value = replace(
            structured_claim(structured_ref_value),
            claim_id="claim-b-structured",
        )

        derived = derived_evidence()
        derived_ref_value = derived_ref(derived)
        derived_claim_value = derived_claim(derived_ref_value)
        unknown_claim = replace(
            derived_claim_value,
            claim_id="claim-c-derived",
            criticality="NORMAL",
            normalized=replace(
                derived_claim_value.normalized,
                sample_years=(),
            ),
            rendered_bindings=tuple(
                binding
                for binding in derived_claim_value.rendered_bindings
                if binding.fact_path != "normalized.sample_years"
            ),
        )
        context = formal_gate_context()
        package = EvidencePackage(
            release_id="rel-2025",
            profile_version="profile-v1",
            result_version="result-v1",
            verifier_version="evidence-verifier-v1",
            scope=VerificationScope(
                province="湖南",
                application_year=2025,
                category="物理类",
                batch="本科批",
                school_id="1001",
            ),
            claims=(
                unknown_claim,
                structured_claim_value,
                document_claim_value,
            ),
            evidence_registry=registry_with_notices(
                (
                    derived_ref_value,
                    structured_ref_value,
                    document_ref_value,
                ),
                context,
            ),
            applicable_as_of=date(2025, 6, 1),
            gate_context=context,
        )

        first = VerifyPackage(
            package,
            structured_authority=FixedStructuredAuthority(structured),
        )
        second = VerifyPackage(
            package,
            structured_authority=FixedStructuredAuthority(structured),
        )

        self.assertEqual(first, second)
        self.assertEqual(first.status, "PARTIAL")
        self.assertEqual(
            tuple(item.claim_id for item in first.claims),
            (
                "claim-a-document",
                "claim-b-structured",
                "claim-c-derived",
            ),
        )
        self.assertEqual(
            tuple(item.status for item in first.claims),
            ("VERIFIED", "VERIFIED", "UNVERIFIABLE"),
        )
        self.assertEqual(
            first.stripped_claim_ids,
            ("claim-c-derived",),
        )
        self.assertEqual(first.blocking_reasons, ())
        self.assertEqual(first.citation_coverage, 1.0)
