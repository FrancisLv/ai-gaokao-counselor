"""VerifyPackage 公开行为测试：只观察证据包校验结果。"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
import hashlib
import unittest

from evidence import (
    Claim,
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


class FixedEntailmentVerifier:
    def __init__(self, result, *, verifier_version="entailment-v1"):
        self.result = result
        self.verifier_version = verifier_version
        self.calls = []

    def verify(self, claim, document):
        self.calls.append((claim.claim_id, document.chunk_id))
        return self.result


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

