"""确定性正式报告：把已验证的算法产物转换为最终 Claims 与证据包。"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
import re
from typing import Any, Protocol

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
from evidence.normalization import (
    render_derived_value,
    sha256_text,
    text_exceeds_admission_boundary,
)
from app.run_lifecycle.formal import payload_sha256


REPORT_VERSION = "m5b-report-v1"
REPORT_PRODUCER_VERSION = "report-renderer-v1"
REPORT_NARRATIVE_NOTE_CLAIM_ID = "claim:report-narrative-note"
REQUIRED_NOTICE_TEXTS = {
    "DISCLAIMER": "本方案仅供志愿参考，不构成录取保证。",
    "RISK_NOTICE": "录取概率为历史估计，存在滑档风险；请结合当年招生计划谨慎决策。",
    "NO_ADMISSION_GUARANTEE": "不保证录取。",
}


@dataclass(frozen=True, slots=True)
class ReportNarrativeRequest:
    principal_id: str
    session_id: str
    run_id: str
    release_id: str
    profile_version: str
    result_version: str
    candidate_count: int
    scope_label: str
    forbidden_fragments: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReportNarrative:
    overview: str
    candidate_reasons: tuple[str, ...]
    note: str | None = None


class ReportNarrativeWriter(Protocol):
    async def compose(
        self,
        request: ReportNarrativeRequest,
    ) -> ReportNarrative: ...


class ReportNarrativeViolation(ValueError):
    """模型返回了可能改变确定性事实或协议边界的叙事。"""


class ReportNarrativeUnavailable(RuntimeError):
    """报告模型暂时不可用，可以退回确定性叙事。"""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class ReportNarrativeRewriteRequired(RuntimeError):
    """叙事内容格式不合格，但未越过事实边界，可在上限内重写。"""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


_DIGIT = re.compile(r"[0-9０-９]")
_TEMPLATE_TOKENS = ("{{", "}}", "${")
_GUARANTEE_PATTERNS = (
    "保证录取",
    "包录取",
    "一定录取",
    "必录取",
    "100%录取",
    "100％录取",
)
_ELIGIBILITY_ASSERTION_PATTERNS = (
    "可以报考",
    "可报考",
    "能够报考",
    "符合报考条件",
    "不能报考",
    "不可报考",
    "不予报考",
    "不符合报考条件",
    "资格通过",
    "资格合格",
    "资格不合格",
    "资格状态为PASS",
    "资格状态为FAIL",
    "资格状态为UNKNOWN",
)


def apply_report_narrative(
    *,
    package: EvidencePackage,
    report: dict[str, Any],
    request: ReportNarrativeRequest,
    narrative: ReportNarrative,
) -> tuple[EvidencePackage, dict[str, Any]]:
    validate_report_narrative(
        package=package,
        report=report,
        request=request,
        narrative=narrative,
    )
    candidate_sections = [
        section
        for section in report["sections"]
        if section["section_key"].startswith("candidate:")
    ]
    reason_by_key = {
        section["section_key"]: reason
        for section, reason in zip(
            candidate_sections,
            narrative.candidate_reasons,
            strict=True,
        )
    }
    if narrative.note is not None:
        package = replace(
            package,
            claims=tuple(
                sorted(
                    (
                        *package.claims,
                        Claim(
                            claim_id=REPORT_NARRATIVE_NOTE_CLAIM_ID,
                            claim_type="DOCUMENT_FACT",
                            criticality="NORMAL",
                            normalized=NormalizedFact(
                                subject="报告叙事",
                                predicate="supplementary_note",
                                object=narrative.note,
                                unit=None,
                            ),
                            rendered_text=narrative.note,
                            evidence_refs=(),
                            rendered_bindings=(),
                        ),
                    ),
                    key=lambda claim: claim.claim_id,
                )
            ),
        )
    sections = []
    for section in report["sections"]:
        current = dict(section)
        if current["section_key"] == "plan":
            sections.append(
                {
                    "section_key": "narrative",
                    "title": "方案说明",
                    "body": narrative.overview,
                }
            )
        if current["section_key"].startswith("candidate:"):
            current["body"] = (
                f"{current['body']}"
                f"{reason_by_key[current['section_key']]}"
            )
        sections.append(current)
    return package, {
        **report,
        "generation_mode": "LLM_ASSISTED",
        "sections": sections,
    }


def filter_stripped_report_claims(
    report: dict[str, Any],
    stripped_claim_ids: tuple[str, ...],
) -> dict[str, Any]:
    stripped = set(stripped_claim_ids)
    return {
        **report,
        "claim_ids": [
            claim_id
            for claim_id in report.get("claim_ids", ())
            if claim_id not in stripped
        ],
    }


def validate_report_narrative(
    *,
    package: EvidencePackage,
    report: dict[str, Any],
    request: ReportNarrativeRequest,
    narrative: ReportNarrative,
) -> None:
    if type(narrative) is not ReportNarrative:
        raise ReportNarrativeViolation("报告模型返回了未注册的响应结构")
    if (
        not isinstance(narrative.overview, str)
        or not isinstance(narrative.candidate_reasons, tuple)
        or not all(isinstance(reason, str) for reason in narrative.candidate_reasons)
        or (
            narrative.note is not None
            and not isinstance(narrative.note, str)
        )
    ):
        raise ReportNarrativeViolation("报告模型响应字段类型无效")
    candidate_sections = [
        section
        for section in report["sections"]
        if section["section_key"].startswith("candidate:")
    ]
    if len(candidate_sections) != request.candidate_count:
        raise ReportNarrativeViolation("报告叙事候选数量不一致")
    if len(narrative.candidate_reasons) != request.candidate_count:
        raise ReportNarrativeViolation("报告叙事候选数量不一致")
    evidence_ids = {
        ref.evidence_id
        for claim in package.claims
        for ref in claim.evidence_refs
    }
    forbidden_fragments = tuple(
        sorted(
            {
                *(
                    fragment.strip()
                    for fragment in request.forbidden_fragments
                    if fragment and fragment.strip()
                ),
                *evidence_ids,
            },
            key=lambda item: (-len(item), item),
        )
    )
    texts = (narrative.overview, *narrative.candidate_reasons)
    if narrative.note is not None:
        texts = (*texts, narrative.note)
    for text in texts:
        normalized = text.strip()
        if not normalized or len(normalized) > 500:
            raise ReportNarrativeViolation("报告叙事长度无效")
        if any(character in normalized for character in ("\x00", "\r", "\n")):
            raise ReportNarrativeViolation("报告叙事包含控制字符")
        if any(token in normalized for token in _TEMPLATE_TOKENS):
            raise ReportNarrativeViolation("报告叙事包含模板控制符")
        if _DIGIT.search(normalized):
            raise ReportNarrativeViolation("报告叙事不得包含数值")
        if (
            any(pattern in normalized for pattern in _GUARANTEE_PATTERNS)
            or text_exceeds_admission_boundary(normalized)
        ):
            raise ReportNarrativeViolation("报告叙事不得承诺录取")
        if any(
            pattern in normalized
            for pattern in _ELIGIBILITY_ASSERTION_PATTERNS
        ):
            raise ReportNarrativeViolation("报告叙事不得决定资格状态")
        if any(fragment in normalized for fragment in forbidden_fragments):
            raise ReportNarrativeViolation("报告叙事不得改写候选或证据身份")


def apply_report_fallback(
    report: dict[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    sections = []
    for section in report["sections"]:
        current = dict(section)
        if current["section_key"] == "risk":
            current["body"] = (
                "生成质量降级：报告模型暂不可用，以下事实均由确定性模板生成。"
                f"{current['body']}"
            )
        sections.append(current)
    return {
        **report,
        "generation_mode": "DETERMINISTIC_FALLBACK",
        "degradation_reason": reason,
        "sections": sections,
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
        scope=scope,
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
        for major in candidate.get("majors", ()):
            major_code = str(major.get("major_code") or "").strip()
            if not major_code:
                raise ValueError("正式报告缺少专业代码")
            major_name = str(major.get("major_name") or major_code).strip()
            add_derived(
                operation_id=(
                    f"candidate:{identity}:major:{major_code}:major_code"
                ),
                subject=f"{subject} {major_name}",
                predicate="major_code",
                value=major_code,
                unit="专业代码",
                precision=0,
                rendered_text=(
                    f"{subject}的{major_name}专业代码为 {major_code}，"
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
    scope: VerificationScope,
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
            (
                f"{major.get('major_name')}（{major.get('major_code')}）"
                if major.get("major_name") and major.get("major_code")
                else major.get("major_name")
                or major.get("major_code")
                or ""
            )
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
                    f"{scope.province}{scope.category}考生位次 "
                    f"{preview['rank']} 名，百分位 "
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
