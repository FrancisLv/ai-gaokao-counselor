"""V1_FORMAL 的资格、版本与必需说明门禁策略。"""
from __future__ import annotations

import re

from evidence.models import (
    EvidencePackage,
    EvidenceRef,
    FormalGateContext,
    QualificationSummary,
    RequiredNotice,
    StructuredEvidence,
)
from evidence.normalization import (
    evidence_payload_release_matches,
    is_sha256,
    sha256_text,
    text_exceeds_admission_boundary,
    valid_document_identity,
    valid_evidence_ref,
    valid_structured_identity,
)


def gate_preflight_reasons(package: EvidencePackage) -> set[str]:
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
        or not is_sha256(context.algorithm_config_sha256)
    ):
        reasons.add("ALGORITHM_CONFIG_MISMATCH")
    if context.verifier_version != package.verifier_version:
        reasons.add("VERIFIER_VERSION_MISMATCH")
    if context.applicable_as_of != package.applicable_as_of:
        reasons.add("APPLICABLE_DATE_MISMATCH")
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
    reasons.update(qualification_gate_reasons(context.qualification))
    reasons.update(required_notice_gate_reasons(package, context))
    if (
        any(
            group.status == "PASS_PARTIAL"
            for group in context.qualification.groups
        )
        and package_overstates_pass_partial(package)
    ):
        reasons.add("PASS_PARTIAL_GROUP_OVERSTATED")
    return reasons


def required_notice_gate_reasons(
    package: EvidencePackage,
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
        if text_exceeds_admission_boundary(notice.normalized_text):
            reasons.add("ADMISSION_GUARANTEE_FORBIDDEN")
        if (
            not is_sha256(notice.content_sha256)
            or sha256_text(notice.normalized_text)
            != notice.content_sha256
        ):
            reasons.add("REQUIRED_NOTICE_MISMATCH")
        if not notice.evidence_refs:
            reasons.add("REQUIRED_NOTICE_MISSING")
            continue
        for ref in notice.evidence_refs:
            registered = _registered_notice_evidence(package, ref)
            if (
                not valid_evidence_ref(ref)
                or registered is None
                or not valid_evidence_ref(registered)
                or not evidence_payload_release_matches(
                    registered,
                    context.release_id,
                )
            ):
                reasons.add("REQUIRED_NOTICE_MISSING")
                continue
            if (
                registered.evidence_type != ref.evidence_type
                or registered.release_id != ref.release_id
                or registered.content_sha256 != ref.content_sha256
            ):
                reasons.add("REQUIRED_NOTICE_MISSING")
                continue
            if not _required_notice_evidence_supports(
                notice,
                registered,
            ):
                reasons.add("REQUIRED_NOTICE_MISMATCH")
    return reasons


def _registered_notice_evidence(
    package: EvidencePackage,
    ref: EvidenceRef,
) -> EvidenceRef | None:
    matches = tuple(
        registered
        for registered in package.evidence_registry
        if registered.evidence_id == ref.evidence_id
    )
    if len(matches) != 1:
        return None
    return matches[0]


def _required_notice_evidence_supports(
    notice: RequiredNotice,
    ref: EvidenceRef,
) -> bool:
    document = ref.document
    if ref.evidence_type == "DOCUMENT" and document is not None:
        return (
            document.release_id == ref.release_id
            and valid_document_identity(document)
            and sha256_text(document.excerpt) == document.excerpt_sha256
            and ref.content_sha256 == document.excerpt_sha256
            and notice.normalized_text in document.excerpt
        )
    structured = ref.structured
    if ref.evidence_type == "STRUCTURED" and structured is not None:
        return (
            structured.release_id == ref.release_id
            and valid_structured_identity(structured)
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


def qualification_gate_reasons(
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


def package_overstates_pass_partial(package: EvidencePackage) -> bool:
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
