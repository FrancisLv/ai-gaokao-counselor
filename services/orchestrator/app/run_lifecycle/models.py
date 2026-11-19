from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, NotRequired, TypedDict
from uuid import UUID


TaskKind = Literal["GENERATE_PLAN", "POLICY_QA", "EXPLAIN_RESULT", "REVISE_PLAN"]
RunState = Literal[
    "QUEUED",
    "RUNNING",
    "WAITING_HITL",
    "EXPIRED",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
]


class RunLifecycleError(Exception):
    """带稳定业务 reason 的 RunLifecycle 错误。"""

    def __init__(self, reason: str, message: str | None = None) -> None:
        self.reason = reason
        super().__init__(message or reason)


class ProfileFieldPayload(TypedDict):
    state: str
    source: str
    value: str | int | list[str] | None


class ProfileFindingPayload(TypedDict):
    code: str
    field_keys: list[str]
    blocking: bool
    message_key: str


ProfileFields = dict[str, ProfileFieldPayload]


class ProgressPayload(TypedDict, total=False):
    message: str
    readiness: str
    message_key: str
    profile_version: str


class ErrorPayload(TypedDict, total=False):
    code: str
    message: str
    retryable: bool
    terminal_reason: str


class ResultPayload(TypedDict):
    result_version: str
    result_kind: str
    preview: "AlgorithmPreviewPayload"


class AlgorithmVerificationCheckPayload(TypedDict):
    key: str
    status: str
    detail: str


class UnverifiedConditionPayload(TypedDict):
    code: str
    message: str


class AlgorithmPreviewPayload(TypedDict, total=False):
    result_version: str
    run_id: str
    release_id: str
    confirmed_profile_version: str
    rank: int
    percentile: float
    rush_candidates: list[dict[str, Any]]
    stable_candidates: list[dict[str, Any]]
    safe_candidates: list[dict[str, Any]]
    rush_total: int
    stable_total: int
    safe_total: int
    audit_basis: dict[str, Any]
    kept_candidates: list[dict[str, Any]]
    rejected_candidates: list[dict[str, Any]]
    wishlist: dict[str, Any]
    algorithm_method: str
    algorithm_config_version: str
    verification_summary: list[AlgorithmVerificationCheckPayload]
    unverified_conditions: list[UnverifiedConditionPayload]
    risk_notice: str
    disclaimer: str
    formal_recommendation: bool
    internal_controlled_only: bool
    result_kind: str


class PolicyEvidenceItemPayload(TypedDict, total=False):
    evidence_id: str
    evidence_type: str
    content_sha256: str
    created_at: str
    producer_version: str
    doc_id: str
    chunk_id: str
    title: str
    document_type: str
    issuer: str
    source_uri: str
    fetched_at: str
    section_path: str
    excerpt: str
    excerpt_sha256: str
    char_start: int | None
    char_end: int | None
    regions: list[str]
    categories: list[str]
    batches: list[str]
    valid_from: str
    valid_to: str | None
    release_id: str


class PolicyEvidencePayload(TypedDict, total=False):
    result_version: str
    result_kind: str
    run_id: str
    release_id: str
    retrieval_status: str
    degraded_reason: str | None
    vector_candidates: int
    keyword_candidates: int
    rerank_enabled: bool
    rerank_model: str | None
    rerank_config_version: str | None
    rerank_timeout_ms: int | None
    relevance_filtered_candidates: int
    relevance_policy_version: str | None
    evidence: list[PolicyEvidenceItemPayload]


RunEventPayload = (
    dict[str, Any]
    | ProgressPayload
    | ErrorPayload
    | ResultPayload
)


@dataclass(frozen=True, slots=True)
class StructuredProfileInput:
    province: str = ""
    application_year: int | None = None
    category: str = ""
    batch: str | None = None
    score: int | None = None
    subjects: tuple[str, ...] = ()
    rank: int | None = None
    school_ids: tuple[str, ...] = ()
    document_types: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PolicyQaInput:
    query_text: str
    province: str
    application_year: int | None
    category: str
    batch: str
    school_ids: tuple[str, ...] = ()
    document_types: tuple[str, ...] = ()
    mode: str = "hybrid"


@dataclass(frozen=True, slots=True)
class SubmitRunCommand:
    principal_id: str
    session_id: UUID | None
    task_kind: TaskKind
    input: StructuredProfileInput | PolicyQaInput
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class AnswerFields:
    fields: ProfileFields


@dataclass(frozen=True, slots=True)
class ConfirmProfileAnswer:
    pass


@dataclass(frozen=True, slots=True)
class RejectOrEditProfileAnswer:
    patch: ProfileFields


@dataclass(frozen=True, slots=True)
class DeclineFieldsAnswer:
    field_keys: tuple[str, ...]


ResumeAnswer = (
    AnswerFields
    | ConfirmProfileAnswer
    | RejectOrEditProfileAnswer
    | DeclineFieldsAnswer
)


@dataclass(frozen=True, slots=True)
class ResumeRunCommand:
    principal_id: str
    run_id: UUID
    interrupt_id: UUID
    expected_state_version: int
    profile_version: UUID | None
    answer: ResumeAnswer
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class CancelRunCommand:
    principal_id: str
    run_id: UUID
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class RunReceipt:
    accepted: bool
    reason: str | None
    run_id: UUID | None
    session_id: UUID
    state: RunState
    stage: str
    state_version: int
    first_event_seq: int
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class ResultReference:
    result_version: UUID
    result_kind: str


@dataclass(frozen=True, slots=True)
class InterruptView:
    interrupt_id: UUID
    kind: str
    state: str
    question_key: str
    payload_version: int
    payload: dict[str, Any]
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class RunView:
    run_id: UUID
    session_id: UUID
    task_kind: TaskKind
    state: RunState
    stage: str
    terminal_reason: str | None
    state_version: int
    release_id: str
    confirmed_profile_version: UUID | None
    active_execution_remaining_ms: int
    cancel_requested_at: datetime | None
    current_interrupt: InterruptView | None
    result: ResultReference | None
    result_payload: AlgorithmPreviewPayload | PolicyEvidencePayload | None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class RunEvent:
    run_id: UUID
    event_seq: int
    type: str
    stage: str
    state_version: int
    occurred_at: datetime
    payload_version: int
    payload: RunEventPayload

