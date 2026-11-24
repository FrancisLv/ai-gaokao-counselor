from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from api.orchestrator.v1 import orchestrator_pb2


@dataclass(frozen=True, slots=True)
class ProfileFieldWire:
    state: str
    source: str
    value: str | int | tuple[str, ...] | None

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "ProfileFieldWire":
        raw = value.get("value")
        if isinstance(raw, list):
            parsed: str | int | tuple[str, ...] | None = tuple(str(item) for item in raw)
        elif isinstance(raw, int) and not isinstance(raw, bool):
            parsed = raw
        elif raw is None:
            parsed = None
        else:
            parsed = str(raw)
        return cls(
            state=str(value.get("state") or ""),
            source=str(value.get("source") or ""),
            value=parsed,
        )

    def populate_proto(self, target) -> None:
        target.state = self.state
        target.source = self.source
        if isinstance(self.value, tuple):
            target.string_list.values.extend(self.value)
        elif isinstance(self.value, int):
            target.int_value = self.value
        elif self.value is not None:
            target.string_value = self.value


@dataclass(frozen=True, slots=True)
class ProfileFindingWire:
    code: str
    field_keys: tuple[str, ...]
    blocking: bool
    message_key: str

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "ProfileFindingWire":
        return cls(
            code=str(value.get("code") or ""),
            field_keys=tuple(str(item) for item in value.get("field_keys") or ()),
            blocking=bool(value.get("blocking")),
            message_key=str(value.get("message_key") or ""),
        )

    def populate_proto(self, target) -> None:
        target.code = self.code
        target.field_keys.extend(self.field_keys)
        target.blocking = self.blocking
        target.message_key = self.message_key


def populate_interrupt(
    target,
    *,
    interrupt_id: str,
    kind: str,
    state: str,
    question_key: str,
    payload_version: int,
    payload: dict[str, Any],
    created_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> None:
    """把持久 interrupt payload 映射成唯一 proto 形状。"""
    target.interrupt_id = interrupt_id
    target.kind = kind
    target.state = state
    target.question_key = question_key
    target.payload_version = payload_version
    target.profile_version = str(payload.get("profile_version") or "")
    for key, value in (payload.get("fields") or {}).items():
        ProfileFieldWire.from_mapping(value).populate_proto(target.fields[key])
    for finding in payload.get("findings") or ():
        ProfileFindingWire.from_mapping(finding).populate_proto(
            target.findings.add()
        )
    region = payload.get("region_clarification")
    if isinstance(region, dict):
        populate_region_clarification(target.region_clarification, region)
    if created_at is not None:
        target.created_at.FromDatetime(created_at)
    if expires_at is not None:
        target.expires_at.FromDatetime(expires_at)


def populate_region_clarification(
    target,
    payload: dict[str, Any],
) -> None:
    target.clarification_id = str(payload.get("clarification_id") or "")
    target.clarification_kind = str(
        payload.get("clarification_kind") or ""
    )
    target.reason_code = str(payload.get("reason_code") or "")
    target.term = str(payload.get("term") or "")
    target.operation = str(payload.get("operation") or "")
    target.catalog_version = str(payload.get("catalog_version") or "")
    target.provinces.extend(
        str(item) for item in payload.get("provinces") or ()
    )
    for preset in payload.get("presets") or ():
        item = target.presets.add()
        item.id = str(preset.get("id") or "")
        item.name = str(preset.get("name") or "")
        item.provinces.extend(
            str(province) for province in preset.get("provinces") or ()
        )


def populate_interrupt_from_event(target, payload: dict[str, Any]) -> None:
    created_at = payload.get("created_at")
    expires_at = payload.get("expires_at")
    populate_interrupt(
        target,
        interrupt_id=str(payload.get("interrupt_id") or ""),
        kind=str(payload.get("kind") or ""),
        state=str(payload.get("state") or ""),
        question_key=str(payload.get("question_key") or ""),
        payload_version=int(payload.get("payload_version") or 0),
        payload=payload.get("payload") or {},
        created_at=(
            datetime.fromisoformat(created_at)
            if isinstance(created_at, str)
            else None
        ),
        expires_at=(
            datetime.fromisoformat(expires_at)
            if isinstance(expires_at, str)
            else None
        ),
    )


def populate_progress(
    target: orchestrator_pb2.ProgressPayload,
    *,
    stage: str,
    payload: dict[str, Any],
) -> None:
    target.stage = stage
    target.message = str(
        payload.get("message")
        or payload.get("readiness")
        or payload.get("message_key")
        or ""
    )
    for key, value in payload.items():
        if key in {"message", "readiness", "message_key"} or value is None:
            continue
        target.metadata[str(key)] = str(value)


def populate_error(
    target: orchestrator_pb2.ErrorPayload,
    payload: dict[str, Any],
) -> None:
    target.code = str(
        payload.get("code")
        or payload.get("terminal_reason")
        or "INTERNAL_ERROR"
    )
    target.message = str(payload.get("message") or target.code)
    target.retryable = bool(payload.get("retryable", False))
