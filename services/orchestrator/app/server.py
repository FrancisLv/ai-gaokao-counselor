"""svc-orchestrator 的 gRPC servicer：驱动 LangGraph 图，把事件以流返回。

调用方向（见 docs/architecture）：
  · 被 svc-gateway 调用（StartGraph / ResumeGraph）；
  · 反过来调 svc-llm-gateway（LLM 治理）与 svc-algo（领域算法）；
  · 本地 Embedding / Rerank 在进程内完成，不经过 svc-llm-gateway。
"""
from __future__ import annotations

import json
import logging
from typing import AsyncIterator
from uuid import UUID

import grpc
from google.protobuf import json_format
from api.orchestrator.v1 import orchestrator_pb2, orchestrator_pb2_grpc

from app.run_lifecycle import (
    AnswerFields,
    CancelRunCommand,
    ConfirmProfileAnswer,
    DeclineFieldsAnswer,
    RunEvent,
    RunLifecycle,
    RunLifecycleError,
    RunReceipt,
    RunView,
    RejectOrEditProfileAnswer,
    ResumeRunCommand,
    StructuredProfileInput,
    SubmitRunCommand,
)
from app.run_lifecycle.wire import (
    populate_error,
    populate_interrupt,
    populate_interrupt_from_event,
    populate_progress,
)

log = logging.getLogger(__name__)


class OrchestratorServicer(orchestrator_pb2_grpc.OrchestratorServicer):
    def __init__(self, lifecycle: RunLifecycle | None = None) -> None:
        self._lifecycle = lifecycle

    async def BuildRetrievalGraph(
        self,
        *,
        principal_id: str,
        run_id: UUID,
        retrieve_policy,
        mode: str = "hybrid",
        top_k: int = 5,
        pool_k: int = 20,
    ):
        """为后续 run-bound 在线 API 提供生产构图 adapter。

        StartGraph/SubmitRun 当前先受理未确认画像；待在线 API 接入后，由服务端
        在对应 run 进入检索阶段时调用本方法，不接受客户端 binding 覆盖。
        """
        from graph.build import build_run_bound_graph

        lifecycle = self._lifecycle
        if lifecycle is None:
            raise RuntimeError("RUN_LIFECYCLE_UNAVAILABLE")
        return await build_run_bound_graph(
            lifecycle=lifecycle,
            principal_id=principal_id,
            run_id=run_id,
            retrieve_policy=retrieve_policy,
            mode=mode,
            top_k=top_k,
            pool_k=pool_k,
        )

    async def StartGraph(self, request, context) -> AsyncIterator["orchestrator_pb2.GraphEvent"]:
        lifecycle = await self._require_lifecycle(context)
        principal_id = await self._principal(context)
        try:
            payload = json.loads(request.user_text)
            session_id = (
                UUID(payload.get("session_id") or request.trace.session_id)
                if payload.get("session_id") or request.trace.session_id
                else None
            )
            command = SubmitRunCommand(
                principal_id=principal_id,
                session_id=session_id,
                task_kind="GENERATE_PLAN",
                input=StructuredProfileInput(
                    province=payload.get("province", ""),
                    application_year=payload.get("application_year"),
                    category=payload.get("category", ""),
                    batch=payload.get("batch"),
                    score=payload.get("score"),
                    subjects=tuple(payload.get("subjects") or ()),
                    rank=payload.get("rank"),
                ),
                idempotency_key=(
                    payload.get("idempotency_key")
                    or request.trace.trace_id
                ),
            )
            if not command.idempotency_key:
                raise ValueError("idempotency_key required")
            receipt = await lifecycle.SubmitRun(command)
        except (ValueError, TypeError, json.JSONDecodeError):
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "STRUCTURED_PROFILE_AND_IDEMPOTENCY_REQUIRED",
            )
        except RunLifecycleError as exc:
            await self._abort_lifecycle(context, exc)
        yield orchestrator_pb2.GraphEvent(
            node="ACCEPTED",
            type="progress",
            payload_json=json.dumps(
                {
                    "run_id": str(receipt.run_id),
                    "session_id": str(receipt.session_id),
                    "state": receipt.state,
                    "stage": receipt.stage,
                    "state_version": receipt.state_version,
                },
                ensure_ascii=False,
            ),
        )

    async def ResumeGraph(self, request, context) -> AsyncIterator["orchestrator_pb2.GraphEvent"]:
        lifecycle = await self._require_lifecycle(context)
        principal_id = await self._principal(context)
        try:
            payload = json.loads(request.user_reply_json)
            run_id = UUID(payload["run_id"])
            interrupt_id = UUID(payload["interrupt_id"])
            profile_version = (
                UUID(payload["profile_version"])
                if payload.get("profile_version")
                else None
            )
            answer_type = str(payload.get("answer_type") or "CONFIRM_PROFILE")
            if answer_type == "CONFIRM_PROFILE":
                answer = ConfirmProfileAnswer()
            elif answer_type == "REJECT_OR_EDIT_PROFILE":
                answer = RejectOrEditProfileAnswer(
                    patch=payload.get("patch") or {},
                )
            elif answer_type == "ANSWER_FIELDS":
                answer = AnswerFields(fields=payload.get("fields") or {})
            elif answer_type == "DECLINE_FIELDS":
                answer = DeclineFieldsAnswer(
                    field_keys=tuple(payload.get("field_keys") or ()),
                )
            else:
                raise ValueError("unsupported answer type")
            receipt = await lifecycle.ResumeRun(
                ResumeRunCommand(
                    principal_id=principal_id,
                    run_id=run_id,
                    interrupt_id=interrupt_id,
                    expected_state_version=int(
                        payload["expected_state_version"]
                    ),
                    profile_version=profile_version,
                    answer=answer,
                    idempotency_key=payload.get("idempotency_key")
                    or request.trace.trace_id,
                )
            )
            if not payload.get("idempotency_key") and not request.trace.trace_id:
                raise ValueError("idempotency_key required")
        except (KeyError, ValueError, TypeError, json.JSONDecodeError):
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "RESUME_COMMAND_AND_IDEMPOTENCY_REQUIRED",
            )
        except RunLifecycleError as exc:
            await self._abort_lifecycle(context, exc)
        yield orchestrator_pb2.GraphEvent(
            node="ACCEPTED",
            type="progress",
            payload_json=json.dumps(
                {
                    "run_id": str(receipt.run_id),
                    "session_id": str(receipt.session_id),
                    "state": receipt.state,
                    "stage": receipt.stage,
                    "state_version": receipt.state_version,
                },
                ensure_ascii=False,
            ),
        )

    async def SubmitRun(self, request, context) -> "orchestrator_pb2.RunReceiptReply":
        lifecycle = await self._require_lifecycle(context)
        principal_id = await self._principal(context)
        if request.task_kind != orchestrator_pb2.GENERATE_PLAN:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "UNSUPPORTED_TASK_KIND")
        try:
            session_id = UUID(request.session_id) if request.session_id else None
        except ValueError:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "INVALID_SESSION_ID")
        try:
            receipt = await lifecycle.SubmitRun(
                SubmitRunCommand(
                    principal_id=principal_id,
                    session_id=session_id,
                    task_kind="GENERATE_PLAN",
                    input=StructuredProfileInput(
                        province=request.profile_input.province,
                        application_year=request.profile_input.application_year,
                        category=request.profile_input.category,
                        batch=request.profile_input.batch or None,
                        score=request.profile_input.score,
                        subjects=tuple(request.profile_input.subjects),
                    ),
                    idempotency_key=request.idempotency_key,
                )
            )
        except RunLifecycleError as exc:
            await self._abort_lifecycle(context, exc)
        return self._receipt_reply(receipt)

    async def GetRun(self, request, context) -> "orchestrator_pb2.RunViewReply":
        lifecycle = await self._require_lifecycle(context)
        principal_id = await self._principal(context)
        try:
            run_id = UUID(request.run_id)
        except ValueError:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "INVALID_RUN_ID")
        try:
            view = await lifecycle.GetRun(principal_id=principal_id, run_id=run_id)
        except RunLifecycleError as exc:
            await self._abort_lifecycle(context, exc)
        return self._run_view_reply(view)

    async def ResumeRun(self, request, context) -> "orchestrator_pb2.RunReceiptReply":
        lifecycle = await self._require_lifecycle(context)
        principal_id = await self._principal(context)
        try:
            run_id = UUID(request.run_id)
            interrupt_id = UUID(request.interrupt_id)
            profile_version = UUID(request.profile_version) if request.profile_version else None
        except ValueError:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "INVALID_RESUME_ID")

        answer_kind = request.WhichOneof("answer")
        if answer_kind == "answer_fields":
            answer = AnswerFields(fields=self._answer_fields(request.answer_fields))
        elif answer_kind == "confirm_profile":
            answer = ConfirmProfileAnswer()
        elif answer_kind == "reject_or_edit_profile":
            answer = RejectOrEditProfileAnswer(
                patch=self._answer_fields(request.reject_or_edit_profile.patch),
            )
        elif answer_kind == "decline_fields":
            answer = DeclineFieldsAnswer(
                field_keys=tuple(request.decline_fields.field_keys),
            )
        else:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "ANSWER_REQUIRED")

        try:
            receipt = await lifecycle.ResumeRun(
                ResumeRunCommand(
                    principal_id=principal_id,
                    run_id=run_id,
                    interrupt_id=interrupt_id,
                    expected_state_version=request.expected_state_version,
                    profile_version=profile_version,
                    answer=answer,
                    idempotency_key=request.idempotency_key,
                )
            )
        except RunLifecycleError as exc:
            await self._abort_lifecycle(context, exc)
        return self._receipt_reply(receipt)

    async def WatchRun(self, request, context) -> AsyncIterator["orchestrator_pb2.RunEvent"]:
        lifecycle = await self._require_lifecycle(context)
        principal_id = await self._principal(context)
        try:
            run_id = UUID(request.run_id)
        except ValueError:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "INVALID_RUN_ID")
        try:
            async for event in lifecycle.WatchRun(
                principal_id=principal_id,
                run_id=run_id,
                after_event_seq=request.after_event_seq,
            ):
                yield self._event_reply(event)
        except RunLifecycleError as exc:
            await self._abort_lifecycle(context, exc)

    async def CancelRun(self, request, context) -> "orchestrator_pb2.RunReceiptReply":
        lifecycle = await self._require_lifecycle(context)
        principal_id = await self._principal(context)
        try:
            run_id = UUID(request.run_id)
        except ValueError:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "INVALID_RUN_ID")
        try:
            receipt = await lifecycle.CancelRun(
                CancelRunCommand(
                    principal_id=principal_id,
                    run_id=run_id,
                    idempotency_key=request.idempotency_key,
                )
            )
        except RunLifecycleError as exc:
            await self._abort_lifecycle(context, exc)
        return self._receipt_reply(receipt)

    async def _require_lifecycle(self, context) -> RunLifecycle:
        if self._lifecycle is None:
            await context.abort(grpc.StatusCode.UNAVAILABLE, "RUN_LIFECYCLE_UNAVAILABLE")
        return self._lifecycle

    @staticmethod
    async def _principal(context) -> str:
        metadata = {key: value for key, value in context.invocation_metadata()}
        principal_id = metadata.get("x-principal-id", "").strip()
        if not principal_id:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "PRINCIPAL_REQUIRED")
        return principal_id

    async def _abort_lifecycle(self, context, exc: RunLifecycleError):
        code = {
            "RUN_NOT_FOUND": grpc.StatusCode.NOT_FOUND,
            "INTERRUPT_NOT_FOUND": grpc.StatusCode.NOT_FOUND,
            "RUN_FORBIDDEN": grpc.StatusCode.PERMISSION_DENIED,
            "EVENT_CURSOR_EXPIRED": grpc.StatusCode.OUT_OF_RANGE,
            "RELEASE_UNAVAILABLE": grpc.StatusCode.FAILED_PRECONDITION,
            "RELEASE_REVOKED": grpc.StatusCode.FAILED_PRECONDITION,
            "IDEMPOTENCY_CONFLICT": grpc.StatusCode.ABORTED,
            "RUN_STATE_CONFLICT": grpc.StatusCode.ABORTED,
            "RUN_ALREADY_TERMINAL": grpc.StatusCode.FAILED_PRECONDITION,
            "INTERRUPT_CONSUMED": grpc.StatusCode.ABORTED,
            "INTERRUPT_EXPIRED": grpc.StatusCode.FAILED_PRECONDITION,
            "PROFILE_VERSION_CONFLICT": grpc.StatusCode.ABORTED,
            "PROFILE_REQUIRED_MISSING": grpc.StatusCode.INVALID_ARGUMENT,
            "PROFILE_INVALID_VALUE": grpc.StatusCode.INVALID_ARGUMENT,
            "PROFILE_UNSUPPORTED_SCOPE": grpc.StatusCode.FAILED_PRECONDITION,
            "PROFILE_UNSUPPORTED_INPUT": grpc.StatusCode.INVALID_ARGUMENT,
            "PROFILE_NOT_CONFIRMED": grpc.StatusCode.FAILED_PRECONDITION,
            "RETRIEVAL_SCOPE_REQUIRED": grpc.StatusCode.FAILED_PRECONDITION,
            "RETRIEVAL_SCOPE_INVALID": grpc.StatusCode.INVALID_ARGUMENT,
            "VALIDATION_FAILED": grpc.StatusCode.FAILED_PRECONDITION,
            "NO_RELIABLE_DATA": grpc.StatusCode.FAILED_PRECONDITION,
            "DEPENDENCY_UNAVAILABLE": grpc.StatusCode.UNAVAILABLE,
        }.get(exc.reason, grpc.StatusCode.INTERNAL)
        await context.abort(code, exc.reason)

    @staticmethod
    def _receipt_reply(receipt: RunReceipt) -> "orchestrator_pb2.RunReceiptReply":
        return orchestrator_pb2.RunReceiptReply(
            accepted=receipt.accepted,
            reason=receipt.reason or "",
            run_id=str(receipt.run_id) if receipt.run_id else "",
            session_id=str(receipt.session_id),
            state=OrchestratorServicer._state_enum(receipt.state),
            stage=receipt.stage,
            state_version=receipt.state_version,
            first_event_seq=receipt.first_event_seq,
            replayed=receipt.replayed,
        )

    @staticmethod
    def _run_view_reply(view: RunView) -> "orchestrator_pb2.RunViewReply":
        reply = orchestrator_pb2.RunViewReply(
            run_id=str(view.run_id),
            session_id=str(view.session_id),
            task_kind=orchestrator_pb2.TaskKind.Value(view.task_kind),
            state=OrchestratorServicer._state_enum(view.state),
            stage=view.stage,
            terminal_reason=view.terminal_reason or "",
            state_version=view.state_version,
            release_id=view.release_id,
            confirmed_profile_version=(
                str(view.confirmed_profile_version) if view.confirmed_profile_version else ""
            ),
            active_execution_remaining_ms=view.active_execution_remaining_ms,
        )
        if view.cancel_requested_at is not None:
            reply.cancel_requested_at.FromDatetime(view.cancel_requested_at)
        if view.current_interrupt is not None:
            interrupt = view.current_interrupt
            populate_interrupt(
                reply.current_interrupt,
                interrupt_id=str(interrupt.interrupt_id),
                kind=interrupt.kind,
                state=interrupt.state,
                question_key=interrupt.question_key,
                payload_version=interrupt.payload_version,
                payload=interrupt.payload,
                created_at=interrupt.created_at,
                expires_at=interrupt.expires_at,
            )
        if view.result is not None:
            reply.result.result_version = str(view.result.result_version)
            reply.result.result_kind = view.result.result_kind
        if view.result_payload is not None:
            reply.result_preview.CopyFrom(
                OrchestratorServicer._algorithm_preview_message(
                    view.result_payload
                )
            )
        reply.created_at.FromDatetime(view.created_at)
        reply.updated_at.FromDatetime(view.updated_at)
        reply.expires_at.FromDatetime(view.expires_at)
        return reply

    @staticmethod
    def _event_reply(event: RunEvent) -> "orchestrator_pb2.RunEvent":
        reply = orchestrator_pb2.RunEvent(
            run_id=str(event.run_id),
            event_seq=event.event_seq,
            type=orchestrator_pb2.RunEventType.Value(event.type),
            stage=event.stage,
            state_version=event.state_version,
            payload_version=event.payload_version,
        )
        reply.occurred_at.FromDatetime(event.occurred_at)
        if event.type == "STATE_CHANGED":
            reply.state_changed.from_state = str(event.payload.get("from_state") or "")
            reply.state_changed.to_state = str(event.payload.get("to_state") or "")
        elif event.type == "INTERRUPT":
            populate_interrupt_from_event(reply.interrupt, event.payload)
        elif event.type == "PROGRESS":
            populate_progress(
                reply.progress,
                stage=event.stage,
                payload=event.payload,
            )
        elif event.type == "ERROR":
            populate_error(reply.error, event.payload)
        elif event.type == "RESULT":
            preview = event.payload.get("preview") or {}
            reply.result.CopyFrom(
                OrchestratorServicer._algorithm_preview_message(preview)
            )
        return reply

    @staticmethod
    def _algorithm_preview_message(payload: dict):
        message = orchestrator_pb2.AlgorithmPreview()
        json_format.ParseDict(payload, message)
        return message

    @staticmethod
    def _state_enum(state: str) -> int:
        return orchestrator_pb2.RunState.Value(state)

    @staticmethod
    def _answer_fields(fields) -> dict[str, dict[str, object]]:
        result: dict[str, dict[str, object]] = {}
        for key, field in fields.items():
            value_kind = field.WhichOneof("value")
            if value_kind is None:
                value = None
            elif value_kind == "string_list":
                value = list(field.string_list.values)
            else:
                value = getattr(field, value_kind)
            result[key] = {
                "state": field.state,
                "source": field.source,
                "value": value,
            }
        return result
