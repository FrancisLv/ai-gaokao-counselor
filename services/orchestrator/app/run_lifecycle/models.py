from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
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


@dataclass(frozen=True, slots=True)
class StructuredProfileInput:
    province: str
    application_year: int
    category: str
    batch: str | None
    score: int
    subjects: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SubmitRunCommand:
    principal_id: str
    session_id: UUID | None
    task_kind: TaskKind
    input: StructuredProfileInput
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
    cancel_requested_at: datetime | None
    current_interrupt: InterruptView | None
    result: ResultReference | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class RunEvent:
    run_id: UUID
    event_seq: int
    type: str
    stage: str
    state_version: int
    occurred_at: datetime
    payload_version: int
    payload: dict[str, Any]

