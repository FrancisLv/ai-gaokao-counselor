"""确定性正式报告：把已验证的算法产物转换为最终 Claims 与证据包。"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from evidence import (
    Claim,
    DerivedEvidence,
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
)
from evidence.canonical import normalize_sample_years
from evidence.normalization import render_derived_value, sha256_text
from app.run_lifecycle.formal import payload_sha256


REPORT_VERSION = "m5b-report-v1"
REPORT_PRODUCER_VERSION = "report-renderer-v1"
REQUIRED_NOTICE_TEXTS = {
    "DISCLAIMER": "本方案仅供志愿参考，不构成录取保证。",
    "RISK_NOTICE": "录取概率为历史估计，存在滑档风险；请结合当年招生计划谨慎决策。",
    "NO_ADMISSION_GUARANTEE": "不保证录取。",
}


def build_formal_report(
    *,
    preview: dict[str, Any],
    artifact: dict[str, Any],
    scope: VerificationScope,
    applicable_as_of: date,
    created_at: datetime,
) -> tuple[EvidencePackage, dict[str, Any]]:
    """从不可变算法结果生成报告、Claims、必需说明和证据注册表。"""
    qualification = _qualification_from_artifact(artifact)
    notices = _required_notices(
        artifact=artifact,
        scope=scope,
        created_at=created_at,
    )
    claims, derived_refs = _algorithm_claims(
        preview=preview,
        artifact=artifact,
        scope=scope,
        created_at=created_at,
    )
    registry = tuple(
        sorted(
            (
                *derived_refs,
                *(
                    ref
                    for notice in notices
                    for ref in notice.evidence_refs
                ),
            ),
            key=lambda ref: ref.evidence_id,
        )
    )
    gate_context = FormalGateContext(
        context_version="formal-gate-v1",
        release_id=artifact["release_id"],
        profile_version=artifact["profile_version"],
        result_version=preview["result_version"],
        algorithm_config_version=artifact["algorithm_config_version"],
        algorithm_config_sha256=artifact["algorithm_config_sha256"],
        verifier_version=artifact["verifier_version"],
        applicable_as_of=applicable_as_of,
        qualification=qualification,
        required_notices=notices,
    )
    package = EvidencePackage(
        release_id=artifact["release_id"],
        profile_version=artifact["profile_version"],
        result_version=preview["result_version"],
        verifier_version=artifact["verifier_version"],
        scope=scope,
        claims=claims,
        evidence_registry=registry,
        applicable_as_of=gate_context.applicable_as_of,
        gate_context=gate_context,
    )
    return package, _report_payload(
        preview=preview,
        qualification=qualification,
        notices=notices,
        claims=claims,
    )


def _algorithm_claims(
    *,
    preview: dict[str, Any],
    artifact: dict[str, Any],
    scope: VerificationScope,
    created_at: datetime,
) -> tuple[tuple[Claim, ...], tuple[EvidenceRef, ...]]:
    basis = preview["audit_basis"]
    sample_years = normalize_sample_years(
        tuple(int(year) for year in basis.get("sample_years", ()))
    )
    if not sample_years:
        raise ValueError("正式报告缺少算法样本年份")
    input_snapshot_sha256 = payload_sha256(
        {
            "release_id": artifact["release_id"],
            "profile_version": artifact["profile_version"],
            "audit_basis": basis,
            "wishlist": preview["wishlist"],
            "eligibility": preview["eligibility"],
        }
    )
    profile_version = artifact["profile_version"]
    release_id = artifact["release_id"]
    config_sha256 = artifact["algorithm_config_sha256"]

    claims: list[Claim] = []
    refs: list[EvidenceRef] = []

    def add_derived(
        *,
        operation_id: str,
        subject: str,
        predicate: str,
        value,
        unit: str,
        precision: int,
        rendered_text: str,
        algorithm_name: str = "weighted_hit",
    ) -> None:
        artifact_hash = payload_sha256(
            {
                "operation_id": operation_id,
                "algorithm": algorithm_name,
                "algorithm_version": artifact["algorithm_config_version"],
                "config_sha256": config_sha256,
                "input_snapshot_sha256": input_snapshot_sha256,
                "profile_version": profile_version,
                "release_id": release_id,
                "sample_years": sample_years,
                "output_field": predicate,
                "output_value": value,
                "unit": unit,
                "precision": precision,
            }
        )
        derived = DerivedEvidence(
            operation_id=operation_id,
            algorithm=algorithm_name,
            algorithm_version=artifact["algorithm_config_version"],
            config_sha256=config_sha256,
            input_snapshot_sha256=input_snapshot_sha256,
            profile_version=profile_version,
            release_id=release_id,
            sample_years=sample_years,
            output_field=predicate,
            output_value=value,
            unit=unit,
            precision=precision,
            artifact_sha256=artifact_hash,
            assumptions=("使用同 release 的算法输入快照确定性生成",),
            warnings=("录取概率为历史估计，不构成录取保证。",),
        )
        evidence_id = f"derived:{operation_id}"
        ref = EvidenceRef(
            evidence_id=evidence_id,
            evidence_type="DERIVED",
            release_id=release_id,
            content_sha256=artifact_hash,
            created_at=created_at,
            producer_version=REPORT_PRODUCER_VERSION,
            derived=derived,
        )
        rendered_object = render_derived_value(value, precision)
        rendered_years = "、".join(str(year) for year in sample_years)
        claim = Claim(
            claim_id=f"claim:{operation_id}",
            claim_type="DERIVED_FACT",
            criticality="CRITICAL",
            normalized=NormalizedFact(
                subject=subject,
                predicate=predicate,
                object=value,
                unit=unit,
                sample_years=sample_years,
                precision=precision,
            ),
            rendered_text=rendered_text,
            evidence_refs=(ref,),
            rendered_bindings=(
                RenderedBinding(
                    fact_path="normalized.object",
                    rendered_text=rendered_object,
                    evidence_id=evidence_id,
                ),
                RenderedBinding(
                    fact_path="normalized.unit",
                    rendered_text=unit,
                    evidence_id=evidence_id,
                ),
                RenderedBinding(
                    fact_path="normalized.sample_years",
                    rendered_text=rendered_years,
                    evidence_id=evidence_id,
                ),
            ),
        )
        claims.append(claim)
        refs.append(ref)

    rank = int(preview["rank"])
    add_derived(
        operation_id="rank",
        subject=f"{scope.province}{scope.category}考生",
        predicate="rank",
        value=rank,
        unit="名",
        precision=0,
        rendered_text=(
            f"{scope.province}{scope.category}考生位次为 {rank} 名，"
            f"样本年份为 {'、'.join(str(year) for year in sample_years)}。"
        ),
        algorithm_name="rank_conversion",
    )

    wishlist_candidates = preview["wishlist"]["candidates"]
    for item in wishlist_candidates:
        candidate = item["cwp"]["candidate"]
        probability = item["cwp"]["prob"]["probability"]
        identity = (
            f"{candidate.get('university_code', '')}|"
            f"{candidate.get('major_group', '')}"
        )
        subject = (
            f"{candidate.get('university_name', '')} "
            f"{candidate.get('major_group', '')}专业组"
        ).strip()
        years_text = "、".join(str(year) for year in sample_years)
        add_derived(
            operation_id=f"candidate:{identity}:probability",
            subject=subject,
            predicate="probability",
            value=probability,
            unit="比例",
            precision=2,
            rendered_text=(
                f"{subject}录取概率为 {render_derived_value(probability, 2)} 比例，"
                f"样本年份为 {years_text}。"
            ),
        )
        add_derived(
            operation_id=f"candidate:{identity}:final_rank",
            subject=subject,
            predicate="final_rank",
            value=int(item["final_rank"]),
            unit="位",
            precision=0,
            rendered_text=(
                f"{subject}最终排名为 {int(item['final_rank'])} 位，"
                f"样本年份为 {years_text}。"
            ),
            algorithm_name="wishlist_optimization",
        )
        add_derived(
            operation_id=f"candidate:{identity}:identity",
            subject=subject,
            predicate="candidate_identity",
            value=identity,
            unit="院校专业组",
            precision=0,
            rendered_text=(
                f"{subject}的院校专业组为 {identity} 院校专业组，"
                f"样本年份为 {years_text}。"
            ),
            algorithm_name="weighted_hit",
        )

    for group in artifact["qualification"]["groups"]:
        subject = str(group["group_key"])
        add_derived(
            operation_id=f"qualification:{subject}",
            subject=subject,
            predicate="group_status",
            value=str(group["status"]),
            unit="状态",
            precision=0,
            rendered_text=(
                f"院校专业组 {subject} 的资格状态为 {group['status']} 状态，"
                f"样本年份为 {'、'.join(str(year) for year in sample_years)}。"
            ),
            algorithm_name="eligibility",
        )

    return tuple(sorted(claims, key=lambda claim: claim.claim_id)), tuple(
        sorted(refs, key=lambda ref: ref.evidence_id)
    )


def _required_notices(
    *,
    artifact: dict[str, Any],
    scope: VerificationScope,
    created_at: datetime,
) -> tuple[RequiredNotice, ...]:
    notices: list[RequiredNotice] = []
    for notice_type in (
        "DISCLAIMER",
        "RISK_NOTICE",
        "NO_ADMISSION_GUARANTEE",
    ):
        text = REQUIRED_NOTICE_TEXTS[notice_type]
        evidence = StructuredEvidence(
            dataset="formal_notice",
            table="release_required_notice",
            primary_key=notice_type,
            business_key=StructuredBusinessKey(
                province=scope.province,
                year=int(scope.application_year),
                category=scope.category,
                batch=scope.batch,
            ),
            field_path=f"notice.{notice_type.lower()}",
            value=text,
            unit=None,
            source_year=None,
            precision=None,
            row_or_snapshot_sha256=payload_sha256(
                {
                    "release_id": artifact["release_id"],
                    "notice_type": notice_type,
                    "normalized_text": text,
                }
            ),
            release_id=artifact["release_id"],
        )
        ref = EvidenceRef(
            evidence_id=f"notice:{notice_type.lower()}",
            evidence_type="STRUCTURED",
            release_id=artifact["release_id"],
            content_sha256=evidence.row_or_snapshot_sha256,
            created_at=created_at,
            producer_version=REPORT_PRODUCER_VERSION,
            structured=evidence,
        )
        notices.append(
            RequiredNotice(
                notice_type=notice_type,
                normalized_text=text,
                content_sha256=sha256_text(text),
                evidence_refs=(ref,),
            )
        )
    return tuple(notices)


def _qualification_from_artifact(
    artifact: dict[str, Any],
) -> QualificationSummary:
    value = artifact["qualification"]
    return QualificationSummary(
        status=value["status"],
        coverage=tuple(
            QualificationCoverage(
                rule_type=item["rule_type"],
                status=item["status"],
            )
            for item in value["coverage"]
        ),
        groups=tuple(
            QualificationGroup(
                group_key=item["group_key"],
                status=item["status"],
                majors=tuple(
                    QualificationMajor(
                        major_code=major["major_code"],
                        status=major["status"],
                    )
                    for major in item["majors"]
                ),
                restrictions=tuple(item.get("restrictions", ())),
            )
            for item in value["groups"]
        ),
        conflict_count=int(value["conflict_count"]),
        unknown_count=int(value["unknown_count"]),
    )


def _report_payload(
    *,
    preview: dict[str, Any],
    qualification: QualificationSummary,
    notices: tuple[RequiredNotice, ...],
    claims: tuple[Claim, ...],
) -> dict[str, Any]:
    sample_years = "、".join(
        str(int(year))
        for year in preview["audit_basis"].get("sample_years", ())
    )
    candidate_sections = []
    for item in preview["wishlist"]["candidates"]:
        candidate = item["cwp"]["candidate"]
        probability = item["cwp"]["prob"]["probability"]
        identity = (
            f"{candidate.get('university_code', '')}|"
            f"{candidate.get('major_group', '')}"
        )
        majors = "、".join(
            major.get("major_name") or major.get("major_code") or ""
            for major in candidate.get("majors", ())
        )
        candidate_sections.append(
            {
                "section_key": f"candidate:{identity}",
                "title": (
                    f"{item['final_rank']}. "
                    f"{candidate.get('university_name', '')} "
                    f"{candidate.get('major_group', '')}专业组"
                ),
                "body": (
                    f"院校：{candidate.get('university_name', '')} "
                    f"{candidate.get('major_group', '')}专业组，"
                    f"院校代码 {candidate.get('university_code', '')}。"
                    f"专业：{majors or '见院校专业组'}。"
                    f"录取概率约 {probability * 100:.0f}%，"
                    f"样本年份为 {sample_years}。"
                    "概率为历史估计，不构成录取保证。"
                ),
            }
        )
    notice_by_type = {
        notice.notice_type: notice.normalized_text
        for notice in notices
    }
    qualification_text = "；".join(
        f"{group.group_key}={group.status}"
        for group in qualification.groups
    )
    return {
        "report_version": REPORT_VERSION,
        "generation_mode": "DETERMINISTIC",
        "sections": [
            {
                "section_key": "plan",
                "title": "方案概况",
                "body": (
                    f"位次 {preview['rank']} 名，百分位 "
                    f"{preview['percentile']:.4f}，"
                    f"共 {len(preview['wishlist']['candidates'])} 个院校专业组。"
                ),
            },
            *candidate_sections,
            {
                "section_key": "qualification",
                "title": "资格核验",
                "body": qualification_text or "资格状态未知。",
            },
            {
                "section_key": "risk",
                "title": "风险提示",
                "body": (
                    f"{notice_by_type['RISK_NOTICE']}"
                    f"{notice_by_type['DISCLAIMER']}"
                    f"{notice_by_type['NO_ADMISSION_GUARANTEE']}"
                ),
            },
        ],
        "claim_ids": [claim.claim_id for claim in claims],
    }
