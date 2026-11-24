"""报告叙事到 LLMGateway 的内部 adapter；不是新的业务 seam。"""
from __future__ import annotations

from typing import Any

import grpc
from google.protobuf.json_format import MessageToDict

from api.common.v1 import common_pb2
from api.llmgw.v1 import llmgw_pb2, llmgw_pb2_grpc
from app.run_lifecycle.report import (
    ReportNarrative,
    ReportNarrativeRequest,
    ReportNarrativeUnavailable,
    ReportNarrativeViolation,
)


REPORT_PROMPT_TEMPLATE_KEY = "m5b_report_narrative"
REPORT_PROMPT_TEMPLATE_VERSION = "v4"
REPORT_RESPONSE_SCHEMA_KEY = "m5b_report_narrative_v1"
REPORT_RESPONSE_SCHEMA_VERSION = "v3"


class GrpcReportNarrativeWriter:
    """只消费服务端注册 prompt/schema 的 report role 调用。"""

    def __init__(
        self,
        channel: grpc.aio.Channel,
        *,
        max_output_tokens: int = 8192,
        timeout_ms: int = 30000,
    ) -> None:
        if max_output_tokens <= 0 or timeout_ms <= 0:
            raise ValueError("报告模型 token 与超时上限必须为正数")
        self._channel = channel
        self._stub = llmgw_pb2_grpc.LLMGatewayStub(channel)
        self._max_output_tokens = max_output_tokens
        self._timeout_ms = timeout_ms

    async def compose(
        self,
        request: ReportNarrativeRequest,
    ) -> ReportNarrative:
        metadata = (("x-principal-id", request.principal_id),)
        chat_request = llmgw_pb2.ChatRequest(
            trace=common_pb2.Trace(
                trace_id=request.run_id,
                session_id=request.session_id,
                node="report",
            ),
            role="report",
            messages=(
                llmgw_pb2.Msg(
                    role="user",
                    content=(
                        "请为当前志愿方案生成简短说明；不得提及具体候选人、"
                        "数值、年份、证据编号或录取承诺。"
                    ),
                ),
            ),
            temperature=0,
            max_tokens=self._max_output_tokens,
            timeout_ms=self._timeout_ms,
            session_id=request.session_id,
            run_id=request.run_id,
            release_id=request.release_id,
            logical_call_id=(
                f"report-narrative:{request.result_version}"
            ),
            prompt_template_key=REPORT_PROMPT_TEMPLATE_KEY,
            prompt_template_version=REPORT_PROMPT_TEMPLATE_VERSION,
            response_schema_key=REPORT_RESPONSE_SCHEMA_KEY,
            response_schema_version=REPORT_RESPONSE_SCHEMA_VERSION,
            data_classification="PUBLIC_DATA",
            data_class=llmgw_pb2.PUBLIC_DATA,
            prompt_variables={
                "candidate_count": str(request.candidate_count),
                "scope_label": request.scope_label,
            },
        )
        try:
            response = await self._stub.Chat(
                chat_request,
                metadata=metadata,
            )
        except grpc.aio.AioRpcError as exc:
            if exc.code() == grpc.StatusCode.DEADLINE_EXCEEDED:
                reason = "PROVIDER_TIMEOUT"
            elif exc.code() == grpc.StatusCode.RESOURCE_EXHAUSTED:
                reason = "PROVIDER_RATE_LIMITED"
            elif exc.code() == grpc.StatusCode.UNAVAILABLE:
                reason = "PROVIDER_UNAVAILABLE"
            else:
                reason = "REPORT_MODEL_REJECTED"
            raise ReportNarrativeUnavailable(reason) from exc
        return _parse_narrative_response(response)


def _parse_narrative_response(response: Any) -> ReportNarrative:
    if not response.HasField("structured_output"):
        raise ReportNarrativeUnavailable("SCHEMA_VALIDATION_FAILED")
    payload = MessageToDict(
        response.structured_output,
        preserving_proto_field_name=True,
    )
    if set(payload) - {"overview", "candidate_reasons", "note"}:
        raise ReportNarrativeViolation("报告模型返回了未注册字段")
    overview = payload.get("overview")
    candidate_reasons = payload.get("candidate_reasons")
    note = payload.get("note")
    if (
        not isinstance(overview, str)
        or not isinstance(candidate_reasons, list)
        or not all(isinstance(reason, str) for reason in candidate_reasons)
        or (note is not None and not isinstance(note, str))
    ):
        raise ReportNarrativeUnavailable("SCHEMA_VALIDATION_FAILED")
    return ReportNarrative(
        overview=overview,
        candidate_reasons=tuple(candidate_reasons),
        note=note,
    )
