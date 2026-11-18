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
from api.orchestrator.v1 import orchestrator_pb2, orchestrator_pb2_grpc

from app.run_lifecycle import (
    CancelRunCommand,
    RunEvent,
    RunLifecycle,
    RunLifecycleError,
    RunReceipt,
    RunView,
    StructuredProfileInput,
    SubmitRunCommand,
)
from graph.build import build_graph
from graph.state import new_state

log = logging.getLogger(__name__)


class OrchestratorServicer(orchestrator_pb2_grpc.OrchestratorServicer):
    def __init__(self, lifecycle: RunLifecycle | None = None, graph=None) -> None:
        self._lifecycle = lifecycle
        self._graph = graph or build_graph()

    async def StartGraph(self, request, context) -> AsyncIterator["orchestrator_pb2.GraphEvent"]:
        state = new_state(
            session_id=request.trace.session_id,
            trace_id=request.trace.trace_id,
            user_text=request.user_text,
        )
        async for ev in self._run(state):
            yield ev

    async def ResumeGraph(self, request, context) -> AsyncIterator["orchestrator_pb2.GraphEvent"]:
        # TODO(M3): 从 LangGraph Postgres saver 恢复检查点，合并用户回复后继续。
        yield orchestrator_pb2.GraphEvent(
            node="RESUME", type="progress",
            payload_json=json.dumps({"session_id": request.session_id}, ensure_ascii=False),
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

    async def _run(self, state) -> AsyncIterator["orchestrator_pb2.GraphEvent"]:
        # TODO(M3): 识别 LangGraph 的 interrupt（HITL）→ type="interrupt" 推给 gateway。
        async for chunk in self._graph.astream(state):
            for node, payload in chunk.items():
                yield orchestrator_pb2.GraphEvent(
                    node=node, type="progress",
                    payload_json=json.dumps(payload, ensure_ascii=False, default=str),
                )
        yield orchestrator_pb2.GraphEvent(node="DONE", type="result", payload_json="{}")

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
        )
        if view.cancel_requested_at is not None:
            reply.cancel_requested_at.FromDatetime(view.cancel_requested_at)
        if view.current_interrupt is not None:
            interrupt = view.current_interrupt
            reply.current_interrupt.interrupt_id = str(interrupt.interrupt_id)
            reply.current_interrupt.kind = interrupt.kind
            reply.current_interrupt.state = interrupt.state
            reply.current_interrupt.question_key = interrupt.question_key
            reply.current_interrupt.payload_version = interrupt.payload_version
            reply.current_interrupt.created_at.FromDatetime(interrupt.created_at)
            reply.current_interrupt.expires_at.FromDatetime(interrupt.expires_at)
        if view.result is not None:
            reply.result.result_version = str(view.result.result_version)
            reply.result.result_kind = view.result.result_kind
        reply.created_at.FromDatetime(view.created_at)
        reply.updated_at.FromDatetime(view.updated_at)
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
        return reply

    @staticmethod
    def _state_enum(state: str) -> int:
        return orchestrator_pb2.RunState.Value(state)
