"""从 RunLifecycle 公开操作执行脚本化端到端评测 case。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from typing import Callable
from uuid import uuid4

from app.run_lifecycle import (
    ConfirmProfileAnswer,
    RejectOrEditProfileAnswer,
    ResumeRunCommand,
    RunLifecycle,
    StructuredProfileInput,
    SubmitRunCommand,
)
from app.run_lifecycle.report import (
    ReportNarrative,
    ReportNarrativeRewriteRequired,
    ReportNarrativeUnavailable,
    ReportNarrativeViolation,
)
from eval.e2e_eval import (
    EndToEndCaseOutcome,
    EndToEndClaim,
)


ProviderCostReader = Callable[[str, str], int]


@dataclass(frozen=True, slots=True)
class ScriptedReportNarrativeWriter:
    """用 EvalDataset 中冻结的模型响应替换真实报告模型。"""

    scripted_model_responses: dict

    async def compose(self, request) -> ReportNarrative:
        if self.scripted_model_responses.get("violation"):
            raise ReportNarrativeViolation(
                str(self.scripted_model_responses["violation"])
            )
        if self.scripted_model_responses.get("rewrite_required"):
            raise ReportNarrativeRewriteRequired(
                str(self.scripted_model_responses["rewrite_required"])
            )
        report = self.scripted_model_responses.get("report")
        if not isinstance(report, dict):
            raise ReportNarrativeUnavailable(
                str(
                    self.scripted_model_responses.get(
                        "unavailable_reason",
                        "SCRIPTED_RESPONSE_MISSING",
                    )
                )
            )
        return ReportNarrative(
            overview=str(report.get("overview", "")),
            candidate_reasons=tuple(
                str(reason)
                for reason in report.get("candidate_reasons", ())
            ),
            note=(
                str(report["note"])
                if report.get("note") is not None
                else None
            ),
        )


@dataclass(slots=True)
class RunLifecycleCaseExecutor:
    """把 case 的脚本动作映射到唯一的 RunLifecycle seam。"""

    lifecycle: RunLifecycle
    poll_interval: float = 0.01
    timeout_seconds: float = 10.0
    provider_cost_reader: ProviderCostReader | None = None

    async def execute(self, case: dict) -> EndToEndCaseOutcome:
        case_id = case["case_id"]
        principal_id = f"eval:{case_id}"
        submitted = await self.lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id=principal_id,
                session_id=None,
                task_kind="GENERATE_PLAN",
                input=_profile_input(case["input_profile"]),
                idempotency_key=f"eval-submit-{case_id}-{uuid4().hex}",
            )
        )
        if submitted.run_id is None:
            raise RuntimeError("RunLifecycle rejected the eval submission")

        edited = False
        loops = max(
            1,
            int(self.timeout_seconds / max(self.poll_interval, 0.001)),
        )
        view = None
        for _ in range(loops):
            view = await self.lifecycle.GetRun(
                principal_id=principal_id,
                run_id=submitted.run_id,
            )
            if view.state in {
                "SUCCEEDED",
                "FAILED",
                "EXPIRED",
                "CANCELLED",
            }:
                return self._outcome(
                    case,
                    principal_id=principal_id,
                    view=view,
                )
            if view.current_interrupt is None:
                await asyncio.sleep(self.poll_interval)
                continue
            if (
                case["expected"]["terminal_status"] == "WAITING_HITL"
                and "missing_global_required_field" in case["tags"]
            ):
                return self._outcome(
                    case,
                    principal_id=principal_id,
                    view=view,
                )

            if (
                "profile_score_edit" in case["tags"]
                and not edited
            ):
                answer = RejectOrEditProfileAnswer(
                    patch=self._edited_profile_patch(view)
                )
                edited = True
            else:
                answer = ConfirmProfileAnswer()
            await self.lifecycle.ResumeRun(
                ResumeRunCommand(
                    principal_id=principal_id,
                    run_id=view.run_id,
                    interrupt_id=view.current_interrupt.interrupt_id,
                    expected_state_version=view.state_version,
                    profile_version=_profile_version(view),
                    answer=answer,
                    idempotency_key=(
                        f"eval-resume-{case_id}-"
                        f"{view.current_interrupt.interrupt_id}"
                    ),
                )
            )
            await asyncio.sleep(self.poll_interval)

        raise TimeoutError(
            f"{case_id}: RunLifecycle did not reach a terminal state "
            f"(state={getattr(view, 'state', None)}, "
            f"stage={getattr(view, 'stage', None)}, "
            f"interrupt={getattr(getattr(view, 'current_interrupt', None), 'kind', None)})"
        )

    def _outcome(
        self,
        case: dict,
        *,
        principal_id: str,
        view,
    ) -> EndToEndCaseOutcome:
        formal = view.formal_result
        result_kind = view.result.result_kind if view.result else None
        result_version = (
            str(view.result.result_version) if view.result else None
        )
        report = formal.report if formal is not None else None
        claims = ()
        report_text = view.terminal_reason or ""
        candidate_keys = ()
        if formal is not None:
            claims = tuple(
                EndToEndClaim(
                    claim_id=claim["claim_id"],
                    claim_type=claim["claim_type"],
                    criticality=claim["criticality"],
                    status=claim["status"],
                    checked_against=tuple(claim["checked_against"]),
                )
                for claim in formal.verification_report.get("claims", ())
            )
            report_text = json.dumps(report, ensure_ascii=False, sort_keys=True)
            candidate_keys = tuple(
                section["section_key"].split(":", 1)[1]
                for section in report.get("sections", ())
                if section.get("section_key", "").startswith("candidate:")
            )
        provider_cost = 0
        if self.provider_cost_reader is not None:
            provider_cost = self.provider_cost_reader(
                principal_id,
                str(view.session_id),
            )
        return EndToEndCaseOutcome(
            case_id=case["case_id"],
            terminal_status=view.state,
            result_kind=result_kind,
            result_version=result_version,
            run_id=str(view.run_id),
            provider_cost_micro_usd=provider_cost,
            report_text=report_text,
            candidate_keys=candidate_keys,
            claims=claims,
        )

    @staticmethod
    def _edited_profile_patch(view) -> dict:
        fields = view.current_interrupt.payload["fields"]
        score = fields["score"]
        current = score.get("value")
        if isinstance(current, int):
            revised = current + 10
        else:
            revised = 600
        return {"score": {**score, "state": "KNOWN", "source": "USER", "value": revised}}


def _profile_input(payload: dict) -> StructuredProfileInput:
    return StructuredProfileInput(
        province=str(payload.get("province") or ""),
        application_year=payload.get("application_year"),
        category=str(payload.get("category") or ""),
        batch=payload.get("batch"),
        score=payload.get("score"),
        subjects=tuple(payload.get("subjects") or ()),
    )


def _profile_version(view):
    from uuid import UUID

    value = view.current_interrupt.payload.get("profile_version")
    if value is None and view.confirmed_profile_version is not None:
        value = view.confirmed_profile_version
    return UUID(str(value))
