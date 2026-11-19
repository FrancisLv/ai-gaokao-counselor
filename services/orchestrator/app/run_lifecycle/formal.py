"""V1_FORMAL 发布命令、不可变身份与规范序列化。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields, is_dataclass, replace
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
from typing import Any
from uuid import UUID

from evidence import EvidencePackage, VerificationReport


@dataclass(frozen=True, slots=True)
class PublishFormalResultCommand:
    principal_id: str
    run_id: UUID
    expected_state_version: int
    expected_lease_epoch: int
    package: EvidencePackage


@dataclass(frozen=True, slots=True)
class FormalPublication:
    accepted: bool
    reason: str | None
    run_id: UUID
    result_version: UUID
    result_kind: str | None
    verification_status: str
    evidence_package_sha256: str
    verification_report_sha256: str
    stripped_claim_ids: tuple[str, ...]
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class FormalResultView:
    result_version: UUID
    result_kind: str
    release_id: str
    confirmed_profile_version: UUID
    evidence_package_sha256: str
    verification_report_sha256: str
    verification_summary: dict[str, Any]
    result_state: str
    stale_reason: str | None


def retained_package(
    package: EvidencePackage,
    report: VerificationReport,
) -> EvidencePackage:
    """只保留报告允许发布的 Claim 及其实际引用的证据。"""
    stripped = set(report.stripped_claim_ids)
    claims = tuple(
        sorted(
            (
                claim
                for claim in package.claims
                if claim.claim_id not in stripped
            ),
            key=lambda claim: claim.claim_id,
        )
    )
    referenced_evidence = {
        ref.evidence_id
        for claim in claims
        for ref in claim.evidence_refs
    }
    if package.gate_context is not None:
        referenced_evidence.update(
            ref.evidence_id
            for notice in package.gate_context.required_notices
            for ref in notice.evidence_refs
        )
    registry = tuple(
        sorted(
            (
                ref
                for ref in package.evidence_registry
                if ref.evidence_id in referenced_evidence
            ),
            key=lambda ref: ref.evidence_id,
        )
    )
    return replace(package, claims=claims, evidence_registry=registry)


def canonical_source_package(package: EvidencePackage) -> EvidencePackage:
    """按稳定身份规范化后计算输入摘要，顺序变化不得伪造新内容。"""
    claims = tuple(
        sorted(
            (
                replace(
                    claim,
                    evidence_refs=tuple(
                        sorted(
                            claim.evidence_refs,
                            key=lambda ref: ref.evidence_id,
                        )
                    ),
                    rendered_bindings=tuple(
                        sorted(
                            claim.rendered_bindings,
                            key=lambda binding: (
                                binding.fact_path,
                                binding.evidence_id,
                                binding.rendered_text,
                            ),
                        )
                    ),
                )
                for claim in package.claims
            ),
            key=lambda claim: claim.claim_id,
        )
    )
    registry = tuple(
        sorted(package.evidence_registry, key=lambda ref: ref.evidence_id)
    )
    gate_context = package.gate_context
    if gate_context is not None:
        notices = tuple(
            sorted(
                (
                    replace(
                        notice,
                        evidence_refs=tuple(
                            sorted(
                                notice.evidence_refs,
                                key=lambda ref: ref.evidence_id,
                            )
                        ),
                    )
                    for notice in gate_context.required_notices
                ),
                key=lambda notice: (
                    notice.notice_type,
                    notice.content_sha256,
                ),
            )
        )
        qualification = replace(
            gate_context.qualification,
            coverage=tuple(
                sorted(
                    gate_context.qualification.coverage,
                    key=lambda coverage: coverage.rule_type,
                )
            ),
            groups=tuple(
                sorted(
                    (
                        replace(
                            group,
                            majors=tuple(
                                sorted(
                                    group.majors,
                                    key=lambda major: major.major_code,
                                )
                            ),
                            restrictions=tuple(sorted(group.restrictions)),
                        )
                        for group in gate_context.qualification.groups
                    ),
                    key=lambda group: group.group_key,
                )
            ),
        )
        gate_context = replace(
            gate_context,
            required_notices=notices,
            qualification=qualification,
        )
    return replace(
        package,
        claims=claims,
        evidence_registry=registry,
        gate_context=gate_context,
    )


def verification_summary(report: VerificationReport) -> dict[str, Any]:
    return _json_ready(asdict(report))


def canonical_payload(value: Any) -> dict[str, Any]:
    return _json_ready(asdict(value) if is_dataclass(value) else value)


def payload_sha256(value: Any) -> str:
    canonical = canonical_payload(value)
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_ready(value: Any) -> Any:
    if is_dataclass(value):
        return {
            field.name: _json_ready(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, dict):
        return {
            str(key): _json_ready(item)
            for key, item in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [_json_ready(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    return value
