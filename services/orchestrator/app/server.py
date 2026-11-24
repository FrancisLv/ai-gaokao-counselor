"""svc-orchestrator 的 gRPC servicer：驱动 LangGraph 图，把事件以流返回。

调用方向（见 docs/architecture）：
  · 被 svc-gateway 调用（StartGraph / ResumeGraph）；
  · 反过来调 svc-llm-gateway（LLM 治理）与 svc-algo（领域算法）；
  · 本地 Embedding / Rerank 在进程内完成，不经过 svc-llm-gateway。
"""
from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator
from uuid import UUID

import grpc
from google.protobuf import json_format
from api.orchestrator.v1 import orchestrator_pb2, orchestrator_pb2_grpc

from app.run_lifecycle import (
    AnswerFields,
    CancelRunCommand,
    ChatMessageInput,
    ConfirmProfileAnswer,
    DeclineFieldsAnswer,
    PolicyQaInput,
    RegionAnswer,
    RunEvent,
    RunLifecycle,
    RunLifecycleError,
    RunReceipt,
    RunView,
    RejectOrEditProfileAnswer,
    ResumeRunCommand,
    StructuredProfileInput,
    SubmitRunCommand,
    WishlistDraftGroup,
)
from app.run_lifecycle.wire import (
    populate_error,
    populate_interrupt,
    populate_interrupt_from_event,
    populate_progress,
    populate_region_clarification,
)

log = logging.getLogger(__name__)


class OrchestratorServicer(orchestrator_pb2_grpc.OrchestratorServicer):
    def __init__(
        self,
        lifecycle: RunLifecycle | None = None,
        deletion=None,
    ) -> None:
        self._lifecycle = lifecycle
        self._deletion = deletion

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
        if request.task_kind not in {
            orchestrator_pb2.GENERATE_PLAN,
            orchestrator_pb2.POLICY_QA,
            orchestrator_pb2.EXPLAIN_RESULT,
            orchestrator_pb2.REVISE_PLAN,
        }:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "UNSUPPORTED_TASK_KIND")
        try:
            session_id = UUID(request.session_id) if request.session_id else None
        except ValueError:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "INVALID_SESSION_ID")
        if request.task_kind == orchestrator_pb2.POLICY_QA:
            policy = request.policy_input
            if not policy.query_text.strip():
                await context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    "RETRIEVAL_QUERY_REQUIRED",
                )
            try:
                receipt = await lifecycle.SubmitRun(
                    SubmitRunCommand(
                        principal_id=principal_id,
                        session_id=session_id,
                        task_kind="POLICY_QA",
                        input=PolicyQaInput(
                            query_text=policy.query_text,
                            province=policy.province,
                            application_year=policy.application_year or None,
                            category=policy.category,
                            batch=policy.batch,
                            school_ids=tuple(policy.school_ids),
                            document_types=tuple(policy.document_types),
                            mode=policy.mode or "hybrid",
                        ),
                        idempotency_key=request.idempotency_key,
                    )
                )
            except RunLifecycleError as exc:
                await self._abort_lifecycle(context, exc)
            return self._receipt_reply(receipt)
        task_kind = orchestrator_pb2.TaskKind.Name(request.task_kind)
        try:
            receipt = await lifecycle.SubmitRun(
                SubmitRunCommand(
                    principal_id=principal_id,
                    session_id=session_id,
                    task_kind=task_kind,
                    input=self._structured_profile(request.profile_input),
                    idempotency_key=request.idempotency_key,
                    parent_result_version=(
                        UUID(request.parent_result_version)
                        if request.parent_result_version
                        else None
                    ),
                    messages=tuple(
                        ChatMessageInput(
                            client_message_id=message.client_message_id,
                            role=message.role,
                            kind=message.kind,
                            content=message.content,
                        )
                        for message in request.messages
                    ),
                    user_message=request.user_message,
                )
            )
        except ValueError:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "INVALID_PARENT_RESULT_VERSION",
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
        elif answer_kind == "region_answer":
            answer = RegionAnswer(
                clarification_id=request.region_answer.clarification_id,
                provinces=tuple(request.region_answer.provinces),
                preset_ids=tuple(request.region_answer.preset_ids),
                catalog_version=request.region_answer.catalog_version,
                cancel_region_restriction=(
                    request.region_answer.cancel_region_restriction
                ),
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

    async def ListSessions(self, request, context) -> "orchestrator_pb2.ListSessionsReply":
        lifecycle = await self._require_lifecycle(context)
        principal_id = await self._principal(context)
        try:
            sessions = await lifecycle.ListSessions(
                principal_id=principal_id,
                limit=request.limit or 20,
            )
        except RunLifecycleError as exc:
            await self._abort_lifecycle(context, exc)
        return orchestrator_pb2.ListSessionsReply(
            sessions=[
                self._session_summary_reply(session)
                for session in sessions
            ]
        )

    async def GetSession(self, request, context) -> "orchestrator_pb2.ChatSessionReply":
        lifecycle = await self._require_lifecycle(context)
        principal_id = await self._principal(context)
        try:
            session_id = UUID(request.session_id)
        except ValueError:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "INVALID_SESSION_ID",
            )
        try:
            view = await lifecycle.GetSession(
                principal_id=principal_id,
                session_id=session_id,
            )
        except RunLifecycleError as exc:
            await self._abort_lifecycle(context, exc)
        return self._chat_session_reply(view)

    async def GetWishlistDraft(
        self,
        request,
        context,
    ) -> "orchestrator_pb2.WishlistDraftReply":
        lifecycle = await self._require_lifecycle(context)
        principal_id = await self._principal(context)
        try:
            result_version = UUID(request.result_version)
        except ValueError:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "INVALID_RESULT_VERSION",
            )
        try:
            draft = await lifecycle.GetWishlistDraft(
                principal_id=principal_id,
                result_version=result_version,
            )
        except RunLifecycleError as exc:
            await self._abort_lifecycle(context, exc)
        return self._wishlist_draft_reply(draft)

    async def SaveWishlistDraft(
        self,
        request,
        context,
    ) -> "orchestrator_pb2.WishlistDraftReply":
        lifecycle = await self._require_lifecycle(context)
        principal_id = await self._principal(context)
        if not request.idempotency_key.strip():
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "IDEMPOTENCY_KEY_REQUIRED",
            )
        try:
            result_version = UUID(request.result_version)
        except ValueError:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "INVALID_RESULT_VERSION",
            )
        groups = tuple(
            WishlistDraftGroup(
                university_code=group.university_code,
                major_group=group.major_group,
                major_codes=tuple(group.major_codes),
            )
            for group in request.groups
        )
        try:
            draft = await lifecycle.SaveWishlistDraft(
                principal_id=principal_id,
                result_version=result_version,
                expected_version=request.expected_version,
                idempotency_key=request.idempotency_key,
                groups=groups,
            )
        except RunLifecycleError as exc:
            await self._abort_lifecycle(context, exc)
        return self._wishlist_draft_reply(draft)

    async def ListWishlists(
        self,
        request,
        context,
    ) -> "orchestrator_pb2.ListWishlistsReply":
        lifecycle = await self._require_lifecycle(context)
        principal_id = await self._principal(context)
        try:
            items = await lifecycle.ListWishlists(
                principal_id=principal_id,
                limit=request.limit or 20,
                offset=request.offset,
                status_filter=request.status,
            )
        except RunLifecycleError as exc:
            await self._abort_lifecycle(context, exc)
        return orchestrator_pb2.ListWishlistsReply(
            wishlists=[
                self._wishlist_summary_reply(item)
                for item in items
            ]
        )

    async def ListWishlistHistory(
        self,
        request,
        context,
    ) -> "orchestrator_pb2.WishlistHistoryReply":
        lifecycle = await self._require_lifecycle(context)
        principal_id = await self._principal(context)
        try:
            result_version = UUID(request.result_version)
        except ValueError:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "INVALID_RESULT_VERSION",
            )
        try:
            drafts = await lifecycle.ListWishlistHistory(
                principal_id=principal_id,
                result_version=result_version,
            )
        except RunLifecycleError as exc:
            await self._abort_lifecycle(context, exc)
        return orchestrator_pb2.WishlistHistoryReply(
            drafts=[
                self._wishlist_draft_message(draft)
                for draft in drafts
            ]
        )

    async def RestoreWishlistDraftVersion(
        self,
        request,
        context,
    ) -> "orchestrator_pb2.WishlistDraftReply":
        lifecycle = await self._require_lifecycle(context)
        principal_id = await self._principal(context)
        if not request.idempotency_key.strip():
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "IDEMPOTENCY_KEY_REQUIRED",
            )
        try:
            result_version = UUID(request.result_version)
        except ValueError:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "INVALID_RESULT_VERSION",
            )
        try:
            draft = await lifecycle.RestoreWishlistDraftVersion(
                principal_id=principal_id,
                result_version=result_version,
                draft_version=request.draft_version,
                idempotency_key=request.idempotency_key,
            )
        except RunLifecycleError as exc:
            await self._abort_lifecycle(context, exc)
        return self._wishlist_draft_reply(draft)

    async def DeleteWishlist(
        self,
        request,
        context,
    ) -> "orchestrator_pb2.DeleteWishlistReply":
        lifecycle = await self._require_lifecycle(context)
        principal_id = await self._principal(context)
        if not request.request_id.strip():
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "IDEMPOTENCY_KEY_REQUIRED",
            )
        try:
            result_version = UUID(request.result_version)
        except ValueError:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "INVALID_RESULT_VERSION",
            )
        try:
            receipt = await lifecycle.DeleteWishlist(
                principal_id=principal_id,
                result_version=result_version,
                request_id=request.request_id,
            )
        except RunLifecycleError as exc:
            await self._abort_lifecycle(context, exc)
        return orchestrator_pb2.DeleteWishlistReply(
            deleted=receipt.deleted,
            request_id=receipt.request_id,
        )

    async def DeletePrincipalData(
        self,
        request,
        context,
    ) -> "orchestrator_pb2.DeletePrincipalDataReply":
        if self._deletion is None:
            await context.abort(
                grpc.StatusCode.UNAVAILABLE,
                "PRINCIPAL_DELETION_UNAVAILABLE",
            )
        principal_id = await self._principal(context)
        if not request.request_id.strip():
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "REQUEST_ID_REQUIRED",
            )
        from app.data_lifecycle import DeletePrincipalDataCommand

        receipt = await self._deletion.DeletePrincipalData(
            DeletePrincipalDataCommand(
                principal_id=principal_id,
                request_id=request.request_id,
                confirmation_token="gateway-authenticated",
            )
        )
        return orchestrator_pb2.DeletePrincipalDataReply(
            accepted=receipt.accepted,
            status=receipt.status,
            reason=receipt.reason or "",
        )

    async def RevokeSensitiveConsent(
        self,
        _request,
        context,
    ) -> "orchestrator_pb2.RevokeSensitiveConsentReply":
        lifecycle = await self._require_lifecycle(context)
        principal_id = await self._principal(context)
        try:
            count = await lifecycle.RevokeSensitiveConsent(
                principal_id=principal_id,
            )
        except RunLifecycleError as exc:
            await self._abort_lifecycle(context, exc)
        return orchestrator_pb2.RevokeSensitiveConsentReply(
            revoked_count=count,
        )

    async def SubmitConversationMessage(
        self,
        request,
        context,
    ) -> "orchestrator_pb2.SubmitConversationMessageReply":
        lifecycle = await self._require_lifecycle(context)
        principal_id = await self._principal(context)
        if not request.idempotency_key.strip():
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "IDEMPOTENCY_KEY_REQUIRED",
            )
        try:
            session_id = UUID(request.session_id)
            parent_result_version = UUID(request.parent_result_version)
        except ValueError:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "INVALID_CONVERSATION_IDENTITY",
            )
        try:
            region_answer = None
            if request.HasField("region_answer"):
                region_answer = RegionAnswer(
                    clarification_id=request.region_answer.clarification_id,
                    provinces=tuple(request.region_answer.provinces),
                    preset_ids=tuple(request.region_answer.preset_ids),
                    catalog_version=request.region_answer.catalog_version,
                    cancel_region_restriction=(
                        request.region_answer.cancel_region_restriction
                    ),
                )
            reply = await lifecycle.SubmitConversationMessage(
                principal_id=principal_id,
                session_id=session_id,
                profile=self._structured_profile(request.profile_input),
                parent_result_version=parent_result_version,
                message_text=request.message_text,
                idempotency_key=request.idempotency_key,
                region_answer=region_answer,
            )
        except RunLifecycleError as exc:
            await self._abort_lifecycle(context, exc)
        return orchestrator_pb2.SubmitConversationMessageReply(
            session_id=str(reply.session_id),
            run_id=str(reply.run_id) if reply.run_id else "",
            task_kind=reply.task_kind or "",
            status=reply.status,
            assistant_message=reply.assistant_message,
            region_clarification=(
                OrchestratorServicer._region_clarification_message(
                    reply.region_clarification
                )
                if reply.region_clarification
                else None
            ),
        )

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
            "PARENT_RESULT_REQUIRED": grpc.StatusCode.INVALID_ARGUMENT,
            "PARENT_RESULT_NOT_FOUND": grpc.StatusCode.NOT_FOUND,
            "PARENT_RESULT_FORBIDDEN": grpc.StatusCode.PERMISSION_DENIED,
            "PARENT_RESULT_INVALID": grpc.StatusCode.FAILED_PRECONDITION,
            "PARENT_RESULT_STALE": grpc.StatusCode.FAILED_PRECONDITION,
            "SESSION_REQUIRED": grpc.StatusCode.INVALID_ARGUMENT,
            "SESSION_NOT_FOUND": grpc.StatusCode.NOT_FOUND,
            "RESULT_NOT_FOUND": grpc.StatusCode.NOT_FOUND,
            "RESULT_STALE": grpc.StatusCode.FAILED_PRECONDITION,
            "DRAFT_COMMAND_INVALID": grpc.StatusCode.INVALID_ARGUMENT,
            "DRAFT_SELECTION_INVALID": grpc.StatusCode.INVALID_ARGUMENT,
            "DRAFT_VERSION_CONFLICT": grpc.StatusCode.ABORTED,
            "REPORT_STRUCTURED_MAJORS_MISSING":
                grpc.StatusCode.FAILED_PRECONDITION,
            "MESSAGE_REQUIRED": grpc.StatusCode.INVALID_ARGUMENT,
            "CHAT_MESSAGE_INVALID": grpc.StatusCode.INVALID_ARGUMENT,
            "REGION_ANSWER_INVALID": grpc.StatusCode.INVALID_ARGUMENT,
            "REGION_CLARIFICATION_NOT_FOUND": grpc.StatusCode.NOT_FOUND,
            "REGION_CLARIFICATION_CONFLICT": grpc.StatusCode.ABORTED,
            "MACRO_REGION_SCOPE_UNRESOLVED": grpc.StatusCode.FAILED_PRECONDITION,
            "MACRO_REGION_CATALOG_UNAVAILABLE": grpc.StatusCode.FAILED_PRECONDITION,
            "RETRIEVAL_QUERY_REQUIRED": grpc.StatusCode.INVALID_ARGUMENT,
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
            parent_result_version=(
                str(view.parent_result_version)
                if view.parent_result_version
                else ""
            ),
            conversation_answer=view.conversation_answer or "",
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
        if view.formal_result is not None:
            reply.formal_result.CopyFrom(
                OrchestratorServicer._formal_result_message(
                    OrchestratorServicer._formal_view_payload(
                        view.formal_result
                    )
                )
            )
        if view.result_payload is not None:
            if view.result is not None and view.result.result_kind == "POLICY_EVIDENCE":
                reply.policy_evidence.CopyFrom(
                    OrchestratorServicer._policy_evidence_message(
                        view.result_payload
                    )
                )
            elif (
                view.result is None
                or view.result.result_kind == "ALGORITHM_PREVIEW"
            ):
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
    def _structured_profile(profile) -> StructuredProfileInput:
        return StructuredProfileInput(
            province=profile.province,
            application_year=profile.application_year,
            category=profile.category,
            batch=profile.batch or None,
            score=profile.score,
            subjects=tuple(profile.subjects),
            risk_appetite=profile.risk_appetite,
            priority_dims=tuple(profile.priority_dims),
            interests=tuple(profile.interests),
            preferred_cities=tuple(profile.preferred_cities),
            preferred_tiers=tuple(profile.preferred_tiers),
            required_provinces=tuple(profile.required_provinces),
            excluded_provinces=tuple(profile.excluded_provinces),
            required_cities=tuple(profile.required_cities),
            excluded_cities=tuple(profile.excluded_cities),
            required_tiers=tuple(profile.required_tiers),
            required_major_names=tuple(profile.required_major_names),
            required_major_keywords=tuple(profile.required_major_keywords),
            excluded_major_keywords=tuple(profile.excluded_major_keywords),
            excluded_major_names=tuple(profile.excluded_major_names),
            excluded_universities=tuple(profile.excluded_universities),
        )

    @staticmethod
    def _region_clarification_message(payload: dict[str, Any]):
        message = orchestrator_pb2.RegionClarificationView()
        populate_region_clarification(message, payload)
        return message

    @staticmethod
    def _session_summary_reply(session):
        reply = orchestrator_pb2.ChatSessionSummary(
            session_id=str(session.session_id),
            title=session.title,
            latest_run_id=(
                str(session.latest_run_id) if session.latest_run_id else ""
            ),
            latest_state=session.latest_state,
            latest_result_version=(
                str(session.latest_result_version)
                if session.latest_result_version
                else ""
            ),
            latest_result_kind=session.latest_result_kind or "",
            run_count=session.run_count,
        )
        reply.created_at.FromDatetime(session.created_at)
        reply.updated_at.FromDatetime(session.updated_at)
        return reply

    @classmethod
    def _chat_session_reply(cls, view):
        reply = orchestrator_pb2.ChatSessionReply(
            summary=cls._session_summary_reply(view.summary),
        )
        for message in view.messages:
            item = reply.messages.add(
                message_id=str(message.message_id),
                run_id=str(message.run_id) if message.run_id else "",
                role=message.role,
                kind=message.kind,
                content=message.content,
            )
            item.created_at.FromDatetime(message.created_at)
            for key, value in message.metadata.items():
                item.metadata[str(key)] = (
                    value
                    if isinstance(value, str)
                    else json.dumps(
                        value,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                )
        for run in view.runs:
            item = reply.runs.add(
                run_id=str(run.run_id),
                task_kind=orchestrator_pb2.TaskKind.Value(run.task_kind),
                state=orchestrator_pb2.RunState.Value(run.state),
                stage=run.stage,
                terminal_reason=run.terminal_reason or "",
                parent_result_version=(
                    str(run.parent_result_version)
                    if run.parent_result_version
                    else ""
                ),
                result_version=(
                    str(run.result_version) if run.result_version else ""
                ),
                result_kind=run.result_kind or "",
            )
            item.created_at.FromDatetime(run.created_at)
            item.updated_at.FromDatetime(run.updated_at)
        for key, value in view.profile_fields.items():
            field = reply.profile_fields[key]
            field.state = str(value.get("state") or "")
            field.source = str(value.get("source") or "")
            raw = value.get("value")
            if isinstance(raw, list):
                field.string_list.values.extend(str(item) for item in raw)
            elif isinstance(raw, int) and not isinstance(raw, bool):
                field.int_value = raw
            elif raw is not None:
                field.string_value = str(raw)
        return reply

    @staticmethod
    def _wishlist_draft_reply(draft) -> "orchestrator_pb2.WishlistDraftReply":
        reply = orchestrator_pb2.WishlistDraftReply()
        if draft is None:
            return reply
        OrchestratorServicer._populate_wishlist_draft(reply.draft, draft)
        return reply

    @staticmethod
    def _wishlist_draft_message(draft) -> "orchestrator_pb2.WishlistDraft":
        reply = orchestrator_pb2.WishlistDraft()
        OrchestratorServicer._populate_wishlist_draft(reply, draft)
        return reply

    @staticmethod
    def _populate_wishlist_draft(target, draft) -> None:
        target.draft_version_id = str(draft.draft_version_id)
        target.result_version = str(draft.result_version)
        target.run_id = str(draft.run_id)
        target.release_id = draft.release_id
        target.draft_version = draft.draft_version
        for group in draft.groups:
            target.groups.add(
                university_code=group.university_code,
                major_group=group.major_group,
                major_codes=group.major_codes,
            )
        if draft.risk is not None:
            risk = target.risk
            risk.group_count = draft.risk.group_count
            risk.rush_count = draft.risk.rush_count
            risk.stable_count = draft.risk.stable_count
            risk.safe_tier_count = draft.risk.safe_tier_count
            risk.safe_guard_count = draft.risk.safe_guard_count
            risk.safe_guard_target = draft.risk.safe_guard_target
            risk.safe_guard_ok = draft.risk.safe_guard_ok
            risk.slip_rate = draft.risk.slip_rate
            if draft.risk.expected_satisfaction is not None:
                risk.expected_satisfaction = (
                    draft.risk.expected_satisfaction
                )
        target.created_at.FromDatetime(draft.created_at)

    @staticmethod
    def _wishlist_summary_reply(item) -> "orchestrator_pb2.WishlistSummary":
        reply = orchestrator_pb2.WishlistSummary(
            draft_version_id=str(item.draft_version_id),
            result_version=str(item.result_version),
            run_id=str(item.run_id),
            release_id=item.release_id,
            draft_version=item.draft_version,
            title=item.title,
            application_year=item.application_year,
            category=item.category,
            batch=item.batch,
            group_count=item.group_count,
            major_count=item.major_count,
            status=item.status,
            stale_reason=item.stale_reason or "",
            successor_run_id=(
                str(item.successor_run_id)
                if item.successor_run_id is not None
                else ""
            ),
        )
        reply.saved_at.FromDatetime(item.saved_at)
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
            if event.payload.get("result_kind") == "POLICY_EVIDENCE":
                retrieval = event.payload.get("retrieval") or {}
                reply.policy_evidence.CopyFrom(
                    OrchestratorServicer._policy_evidence_message(
                        retrieval
                    )
                )
            elif event.payload.get("result_kind") == "V1_FORMAL":
                formal_result = event.payload.get("formal_result") or {}
                reply.formal_result.CopyFrom(
                    OrchestratorServicer._formal_result_message(
                        formal_result
                    )
                )
            else:
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
    def _policy_evidence_message(payload: dict):
        message = orchestrator_pb2.PolicyEvidenceResult()
        json_format.ParseDict(payload, message)
        return message

    @staticmethod
    def _formal_view_payload(formal_result) -> dict:
        return {
            "result_version": str(formal_result.result_version),
            "result_kind": formal_result.result_kind,
            "release_id": formal_result.release_id,
            "confirmed_profile_version": str(
                formal_result.confirmed_profile_version
            ),
            "evidence_package_sha256": (
                formal_result.evidence_package_sha256
            ),
            "verification_report_sha256": (
                formal_result.verification_report_sha256
            ),
            "verification_report": formal_result.verification_report,
            "report": formal_result.report,
            "risk_notice": formal_result.risk_notice,
            "disclaimer": formal_result.disclaimer,
            "no_admission_guarantee": (
                formal_result.no_admission_guarantee
            ),
            "result_state": formal_result.result_state,
            "stale_reason": formal_result.stale_reason,
        }

    @staticmethod
    def _formal_result_message(payload: dict):
        report = (
            payload.get("verification_report")
            or payload.get("verification_summary")
            or {}
        )
        message = orchestrator_pb2.FormalResultSummary()
        json_format.ParseDict(
            {
                "resultVersion": payload.get("result_version", ""),
                "resultKind": payload.get("result_kind", ""),
                "releaseId": payload.get("release_id", ""),
                "confirmedProfileVersion": payload.get(
                    "confirmed_profile_version",
                    "",
                ),
                "evidencePackageSha256": payload.get(
                    "evidence_package_sha256",
                    "",
                ),
                "verificationReportSha256": payload.get(
                    "verification_report_sha256",
                    "",
                ),
                "verificationStatus": report.get("status", ""),
                "claims": report.get("claims", []),
                "blockingReasons": report.get("blocking_reasons", []),
                "strippedClaimIds": report.get("stripped_claim_ids", []),
                "citationCoverage": report.get("citation_coverage", 0.0),
                "report": payload.get("report", {}),
                "resultState": payload.get("result_state", "ACTIVE"),
                "staleReason": payload.get("stale_reason") or "",
                "disclaimer": payload.get("disclaimer", ""),
                "riskNotice": payload.get("risk_notice", ""),
                "noAdmissionGuarantee": payload.get(
                    "no_admission_guarantee",
                    "",
                ),
            },
            message,
        )
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
