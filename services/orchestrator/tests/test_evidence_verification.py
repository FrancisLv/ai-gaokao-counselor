"""VerifyPackage 公开行为测试：只观察证据包校验结果。"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
import hashlib
import unittest

from evidence import (
    Claim,
    DerivedEvidence,
    DocumentApplicability,
    DocumentEvidence,
    EvidencePackage,
    EvidenceRef,
    NormalizedFact,
    RenderedBinding,
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
        evidence_registry=(
            registry if registry is not None else (ref,)
        ),
    )


def derived_evidence(
    *,
    output_value=15860,
    unit: str | None = "名",
    sample_years=(2022, 2023, 2024),
    precision: int | None = 0,
) -> DerivedEvidence:
    return DerivedEvidence(
        operation="rank_convert",
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
        content_sha256="e" * 64,
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
        evidence_registry=(ref,),
    )


class FixedEntailmentVerifier:
    def __init__(self, result, *, verifier_version="entailment-v1"):
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
        self.calls.append(evidence.operation)
        return self.evidence


class VerifyPackageDocumentTests(unittest.TestCase):
    def test_same_release_locatable_document_claim_is_verified(self):
        document = document_evidence()
        ref = document_ref(document)
        package = document_package(document, ref)

        report = VerifyPackage(package, today=date(2025, 6, 1))

        self.assertEqual(report.status, "PASS")
        self.assertEqual(report.claims[0].claim_id, "claim-transfer-policy")
        self.assertEqual(report.claims[0].status, "VERIFIED")
        self.assertEqual(report.claims[0].checked_against, ("ev-transfer",))
        self.assertTrue(report.claims[0].deterministic)
        self.assertEqual(report.blocking_reasons, ())

    def test_excerpt_hash_mismatch_is_contradicted(self):
        document = document_evidence()
        ref = replace(document_ref(document), content_sha256="b" * 64)
        package = document_package(document, ref)

        report = VerifyPackage(package, today=date(2025, 6, 1))

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
                    today=date(2025, 6, 1),
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
                    today=date(2025, 6, 1),
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
            today=date(2025, 6, 1),
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
            today=date(2025, 6, 1),
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
                    today=date(2025, 6, 1),
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
            today=date(2025, 6, 1),
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

        report = VerifyPackage(package, today=date(2025, 6, 1))

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
            today=date(2025, 6, 1),
            entailment_verifier=adapter,
        )

        self.assertEqual(report.claims[0].status, "VERIFIED")
        self.assertFalse(report.claims[0].deterministic)
        self.assertEqual(adapter.calls, [("claim-transfer-policy", "chunk-transfer")])

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
            today=date(2025, 6, 1),
            entailment_verifier=FixedEntailmentVerifier(None),
        )

        self.assertEqual(report.claims[0].status, "UNVERIFIABLE")
        self.assertFalse(report.claims[0].deterministic)

    def test_repeated_verification_is_stable(self):
        document = document_evidence()
        ref = document_ref(document)
        package = document_package(document, ref)

        first = VerifyPackage(package, today=date(2025, 6, 1))
        second = VerifyPackage(package, today=date(2025, 6, 1))

        self.assertEqual(first, second)

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

        report = VerifyPackage(package, today=date(2025, 6, 1))

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

        report = VerifyPackage(package, today=date(2025, 6, 1))

        self.assertEqual(report.status, "BLOCKED")
        self.assertTrue(
            all(item.status != "VERIFIED" for item in report.claims)
        )


class VerifyPackageDerivedTests(unittest.TestCase):
    def test_derived_probability_cannot_assert_admission_guarantee(self):
        evidence = replace(
            derived_evidence(),
            operation="probability",
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
            today=date(2025, 6, 1),
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
            today=date(2025, 6, 1),
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
            today=date(2025, 6, 1),
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
            today=date(2025, 6, 1),
        )

        self.assertEqual(report.status, "PASS")
        self.assertEqual(report.claims[0].status, "VERIFIED")
        self.assertEqual(
            report.claims[0].checked_against,
            ("ev-derived-rank",),
        )
        self.assertTrue(report.claims[0].deterministic)
