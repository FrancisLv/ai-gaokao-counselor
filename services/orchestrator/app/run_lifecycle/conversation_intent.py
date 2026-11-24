"""报告后对话意图分类的受限 LLM adapter。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import grpc
from google.protobuf.json_format import MessageToDict

from api.common.v1 import common_pb2
from api.llmgw.v1 import llmgw_pb2, llmgw_pb2_grpc


ConversationIntentDecision = Literal[
    "EXPLAIN_RESULT",
    "REVISE_PLAN",
    "CLARIFY",
    "OFF_TOPIC",
    "OUT_OF_SCOPE",
]

CONVERSATION_INTENT_PROMPT_KEY = "m5b_conversation_intent"
CONVERSATION_INTENT_PROMPT_VERSION = "v5"
CONVERSATION_INTENT_SCHEMA_KEY = "m5b_conversation_intent_v1"
CONVERSATION_INTENT_SCHEMA_VERSION = "v4"

_PATCH_FIELDS = frozenset({
    "score",
    "risk_appetite",
    "preferred_cities",
    "required_cities",
    "excluded_cities",
    "required_provinces",
    "excluded_provinces",
    "required_tiers",
    "major_interests",
    "required_major_names",
    "required_major_keywords",
    "excluded_major_keywords",
    "excluded_major_names",
    "excluded_universities",
})


class ConversationIntentUnavailable(RuntimeError):
    """意图模型不可用或输出不符合受限契约。"""


@dataclass(frozen=True, slots=True)
class ConversationIntent:
    decision: ConversationIntentDecision
    patch: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ConversationIntentRequest:
    principal_id: str
    session_id: str
    run_id: str
    request_id: str
    parent_result_version: str
    release_id: str
    message_text: str
    risk_appetite: str
    rush_count: int
    stable_count: int
    safe_count: int


class ConversationIntentClassifier(Protocol):
    async def classify(
        self,
        request: ConversationIntentRequest,
    ) -> ConversationIntent: ...


class GrpcConversationIntentClassifier:
    """只消费已注册 prompt/schema；模型不能直接修改画像或执行动作。"""

    def __init__(
        self,
        channel: grpc.aio.Channel,
        *,
        max_output_tokens: int = 4096,
        timeout_ms: int = 20000,
    ) -> None:
        self._stub = llmgw_pb2_grpc.LLMGatewayStub(channel)
        self._max_output_tokens = max_output_tokens
        self._timeout_ms = timeout_ms

    async def classify(
        self,
        request: ConversationIntentRequest,
    ) -> ConversationIntent:
        response = await self._stub.Chat(
            llmgw_pb2.ChatRequest(
                trace=common_pb2.Trace(
                    trace_id=request.request_id,
                    session_id=request.session_id,
                    node="conversation-intent",
                ),
                role="profile",
                messages=(
                    llmgw_pb2.Msg(
                        role="user",
                        content=request.message_text,
                    ),
                ),
                json_schema=json.dumps(
                    _response_schema(),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                temperature=0,
                max_tokens=self._max_output_tokens,
                timeout_ms=self._timeout_ms,
                session_id=request.session_id,
                run_id=request.run_id,
                release_id=request.release_id,
                logical_call_id=(
                    f"conversation-intent:{request.request_id}"
                ),
                prompt_template_key=CONVERSATION_INTENT_PROMPT_KEY,
                prompt_template_version=CONVERSATION_INTENT_PROMPT_VERSION,
                response_schema_key=CONVERSATION_INTENT_SCHEMA_KEY,
                response_schema_version=CONVERSATION_INTENT_SCHEMA_VERSION,
                data_classification="PRINCIPAL_DATA",
                data_class=llmgw_pb2.PRINCIPAL_DATA,
                prompt_variables={
                    "parent_result_version": request.parent_result_version,
                    "risk_appetite": request.risk_appetite,
                    "rush_count": str(request.rush_count),
                    "stable_count": str(request.stable_count),
                    "safe_count": str(request.safe_count),
                },
            ),
            metadata=(("x-principal-id", request.principal_id),),
        )
        return _parse_response(response)


def _response_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["decision", "patch"],
        "properties": {
            "decision": {
                "type": "string",
                "enum": [
                    "EXPLAIN_RESULT",
                    "REVISE_PLAN",
                    "CLARIFY",
                    "OFF_TOPIC",
                    "OUT_OF_SCOPE",
                ],
            },
            "patch": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "score": {"type": "integer"},
                    "risk_appetite": {
                        "type": "string",
                        "enum": [
                            "conservative",
                            "balanced",
                            "aggressive",
                        ],
                    },
                    **{
                        key: {
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": 20,
                        }
                        for key in sorted(_PATCH_FIELDS - {
                            "score",
                            "risk_appetite",
                        })
                    },
                },
            },
        },
    }


def _parse_response(response: Any) -> ConversationIntent:
    if not response.HasField("structured_output"):
        raise ConversationIntentUnavailable("SCHEMA_VALIDATION_FAILED")
    payload = MessageToDict(
        response.structured_output,
        preserving_proto_field_name=True,
    )
    decision = payload.get("decision")
    patch = payload.get("patch")
    if decision not in {
        "EXPLAIN_RESULT",
        "REVISE_PLAN",
        "CLARIFY",
        "OFF_TOPIC",
        "OUT_OF_SCOPE",
    } or not isinstance(patch, dict):
        raise ConversationIntentUnavailable("SCHEMA_VALIDATION_FAILED")
    if set(patch) - _PATCH_FIELDS:
        raise ConversationIntentUnavailable("PATCH_FIELD_REJECTED")
    if decision != "REVISE_PLAN" and patch:
        raise ConversationIntentUnavailable("PATCH_NOT_ALLOWED")
    return ConversationIntent(
        decision=decision,
        patch=patch,
    )
