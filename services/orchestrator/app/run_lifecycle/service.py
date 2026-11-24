from __future__ import annotations

import asyncio
import base64
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import math
import re
from collections.abc import Callable
from dataclasses import replace
from typing import Any, AsyncIterator, Protocol
from uuid import UUID, uuid4

import grpc
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
import psycopg
from google.protobuf.json_format import MessageToDict
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from api.algo.v1 import algo_pb2
from api.common.v1 import common_pb2
from app.run_lifecycle.algo import AlgoClient, grpc_error_reason
from app.run_lifecycle.consent import (
    CONSENT_POLICY_VERSION,
    redact_sensitive_profile_fields,
    require_sensitive_consent,
)
from app.run_lifecycle.conversation_intent import (
    ConversationIntentClassifier,
    ConversationIntentRequest,
)
from app.run_lifecycle.events import append_event
from app.run_lifecycle.formal import (
    FormalPublication,
    FormalResultView,
    PublishFormalResultCommand,
    canonical_payload,
    canonical_source_package,
    formal_gate_artifact,
    formal_gate_artifact_sha256,
    payload_sha256,
    retained_package,
    verification_report_payload,
)
from app.run_lifecycle.macro_region import (
    MACRO_REGION_SCOPE_UNRESOLVED,
    PROVINCES,
    MacroRegionError,
    RegionCatalog,
    clarification_payload,
    detect_macro_region,
    load_region_catalog,
    mentions_macro_region,
    validate_region_answer,
)
from app.run_lifecycle.models import (
    AlgorithmPreviewPayload,
    AnswerFields,
    ChatMessage,
    ChatMessageInput,
    ChatSessionSummary,
    ChatSessionView,
    ConfirmProfileAnswer,
    ConversationSubmission,
    CancelRunCommand,
    DeclineFieldsAnswer,
    InterruptView,
    PolicyQaInput,
    RegionAnswer,
    RejectOrEditProfileAnswer,
    ResumeRunCommand,
    ResultReference,
    RunEvent,
    RunLifecycleError,
    RunReceipt,
    RunView,
    SessionRunSummary,
    StructuredProfileInput,
    SubmitRunCommand,
    WishlistDraft,
    WishlistDeletionReceipt,
    WishlistDraftGroup,
    WishlistDraftRisk,
    WishlistSummary,
)
from app.run_lifecycle.profile import ProfileRuntime, build_profile_fields
from app.run_lifecycle.profile_graph import build_profile_graph
from app.run_lifecycle.report import (
    ReportNarrativeRequest,
    ReportNarrativeRewriteRequired,
    ReportNarrativeUnavailable,
    ReportNarrativeViolation,
    ReportNarrativeWriter,
    apply_report_fallback,
    apply_report_narrative,
    build_formal_report,
    filter_stripped_report_claims,
)
from evidence import (
    DerivedEvidenceRecomputer,
    EntailmentVerifier,
    EvidencePackage,
    QualificationCoverage,
    QualificationGroup,
    QualificationMajor,
    QualificationSummary,
    StructuredEvidenceAuthority,
    VerificationReport,
    VerificationScope,
    VerifyPackage,
)
from evidence.canonical import sha256_payload
from rag.policy_retrieval import (
    ApplicabilityScope,
    PolicyRetrievalError,
    PolicyRetrievalQuery,
    PolicyRetrievalResult,
)

ALGO_BUSINESS_ERROR_REASONS = frozenset({
    "INVALID_SUBJECTS",
    "SCORE_ABOVE_MAX",
    "SCORE_BELOW_CONTROL_LINE",
    "NO_RELIABLE_DATA",
})

_CONVERSATION_CITIES = (
    "长沙", "武汉", "广州", "深圳", "北京", "上海", "南京",
    "西安", "成都", "重庆", "杭州", "天津", "苏州", "厦门",
    "青岛", "郑州", "济南", "合肥", "南昌", "昆明", "哈尔滨",
    "大连", "沈阳", "长春", "福州",
)
_CONVERSATION_PROVINCES = PROVINCES
_CITY_PROVINCE = {
    "长沙": "湖南",
    "武汉": "湖北",
    "广州": "广东",
    "深圳": "广东",
    "北京": "北京",
    "上海": "上海",
    "南京": "江苏",
    "苏州": "江苏",
    "西安": "陕西",
    "成都": "四川",
    "重庆": "重庆",
    "杭州": "浙江",
    "天津": "天津",
    "厦门": "福建",
    "福州": "福建",
    "青岛": "山东",
    "济南": "山东",
    "郑州": "河南",
    "合肥": "安徽",
    "南昌": "江西",
    "昆明": "云南",
    "哈尔滨": "黑龙江",
    "大连": "辽宁",
    "沈阳": "辽宁",
    "长春": "吉林",
}
_CONVERSATION_MAJOR_KEYWORDS = (
    "计算机", "电子信息", "自动化", "人工智能", "软件工程",
    "临床医学", "医学", "法学", "金融", "会计", "数学", "物理",
    "化学", "汉语言", "英语", "机械", "土木",
)
_CONVERSATION_MAJOR_EXCLUSION_KEYWORDS = ("师范",)
_CONVERSATION_EXACT_MAJOR_NAMES = (
    "康复物理治疗", "地球物理学类", "地球物理学", "应用物理学",
    "材料物理", "工程物理", "核物理", "物理学类", "物理学",
)
_PHYSICS_RELATED_MAJOR_INTERESTS = (
    "物理学", "物理学类", "应用物理学", "核物理", "材料物理",
    "地球物理学", "工程物理",
)
_MORE_RUSH_PATTERN = re.compile(
    r"(?:"
    r"(?:增加|多加|多放|多来|再加|加几个|多几个|多一些)"
    r".{0,8}(?:冲|冲刺)"
    r"|"
    r"(?:冲|冲刺)(?:档|比例|学校|候选|专业组|方案)?"
    r".{0,8}(?:太少|少了|不够)"
    r".{0,12}(?:增加|多加|多放|多来|再加|加几个|多几个|多一些)"
    r")"
)

logger = logging.getLogger(__name__)


class PolicyRetriever(Protocol):
    async def retrieve(
        self,
        query: PolicyRetrievalQuery,
    ) -> PolicyRetrievalResult: ...


class FormalPublishGuard(Protocol):
    async def before_publish(self, run_id: UUID) -> None: ...


class RunLifecycle:
    """调用方与测试共同使用的唯一持久任务 seam。"""

    def __init__(
        self,
        dsn: str,
        *,
        auto_profile: bool = False,
        worker_owner: str | None = None,
        lease_duration: timedelta = timedelta(seconds=30),
        clock: Callable[[], datetime] | None = None,
        algo_client: AlgoClient | None = None,
        policy_retriever: PolicyRetriever | None = None,
        entailment_verifier: EntailmentVerifier | None = None,
        structured_evidence_authority: StructuredEvidenceAuthority | None = None,
        derived_evidence_recomputer: DerivedEvidenceRecomputer | None = None,
        formal_verifier_version: str = "evidence-verifier-v1",
        formal_publish_guard: FormalPublishGuard | None = None,
        consent_policy_version: str = CONSENT_POLICY_VERSION,
        report_narrative_writer: ReportNarrativeWriter | None = None,
        conversation_intent_classifier: ConversationIntentClassifier | None = None,
    ) -> None:
        if not dsn:
            raise ValueError("RunLifecycle 需要 PostgreSQL DSN")
        self._dsn = dsn
        self._auto_profile = auto_profile
        self._worker_owner = worker_owner or f"orchestrator-{uuid4()}"
        self._lease_duration = lease_duration
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._algo_client = algo_client
        self._policy_retriever = policy_retriever
        self._entailment_verifier = entailment_verifier
        self._structured_evidence_authority = structured_evidence_authority
        self._derived_evidence_recomputer = derived_evidence_recomputer
        self._formal_verifier_version = formal_verifier_version
        self._formal_publish_guard = formal_publish_guard
        self._report_narrative_writer = report_narrative_writer
        self._conversation_intent_classifier = conversation_intent_classifier
        if not consent_policy_version.strip():
            raise ValueError("consent_policy_version 不能为空")
        self._consent_policy_version = consent_policy_version
        self._saver_cm = None
        self._saver: AsyncPostgresSaver | None = None
        self._profile_graph = None
        self._runtime_lock = asyncio.Lock()
        self._profile = ProfileRuntime()

    async def _after_claim_hook(self, run_id: UUID, lease_epoch: int) -> None:
        """测试 adapter 可覆写的执行点；生产默认无副作用。"""

    def _checkpoint_persisted_hook(self, run_id: UUID) -> None:
        """测试 adapter 可覆写的崩溃注入点。"""

    async def _resume_accepted_hook(self, run_id: UUID) -> None:
        """测试 adapter 可覆写的提交后崩溃注入点。"""

    async def close(self) -> None:
        if self._saver_cm is not None:
            await self._saver_cm.__aexit__(None, None, None)
            self._saver_cm = None
            self._saver = None
            self._profile_graph = None

    async def _connect(self, *, autocommit: bool = False) -> psycopg.AsyncConnection:
        return await psycopg.AsyncConnection.connect(
            self._dsn,
            row_factory=dict_row,
            autocommit=autocommit,
        )

    async def SubmitRun(self, command: SubmitRunCommand) -> RunReceipt:
        if command.task_kind == "POLICY_QA" and not isinstance(
            command.input,
            PolicyQaInput,
        ):
            raise RunLifecycleError(
                "RETRIEVAL_SCOPE_INVALID",
                "POLICY_QA requires PolicyQaInput",
            )
        if command.task_kind in {"GENERATE_PLAN", "REVISE_PLAN", "EXPLAIN_RESULT"} and not isinstance(
            command.input,
            StructuredProfileInput,
        ):
            raise RunLifecycleError(
                "PROFILE_UNSUPPORTED_INPUT",
                f"{command.task_kind} requires StructuredProfileInput",
            )
        if command.task_kind in {"REVISE_PLAN", "EXPLAIN_RESULT"}:
            if command.parent_result_version is None:
                raise RunLifecycleError("PARENT_RESULT_REQUIRED")
            if command.session_id is None:
                raise RunLifecycleError("SESSION_REQUIRED")
        if command.task_kind == "EXPLAIN_RESULT" and not command.user_message.strip():
            raise RunLifecycleError("MESSAGE_REQUIRED")
        if isinstance(command.input, PolicyQaInput):
            if not command.input.query_text.strip():
                raise RunLifecycleError("RETRIEVAL_QUERY_REQUIRED")
            if (
                command.input.application_year is None
                or not command.input.province
                or not command.input.category
                or not command.input.batch
            ):
                raise RunLifecycleError("RETRIEVAL_SCOPE_REQUIRED")
        request_payload = self._submit_request_payload(command)
        request_hash = self._request_hash(request_payload)
        scope_key = f"session:{command.session_id}" if command.session_id else "session:new"
        receipt_lock = f"{command.principal_id}:SUBMIT_RUN:{scope_key}:{command.idempotency_key}"

        conn = await self._connect(autocommit=True)
        try:
            async with conn.transaction():
                await self._raise_if_principal_unavailable(
                    conn,
                    command.principal_id,
                )
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (receipt_lock,),
                )
                replay = await self._find_receipt(
                    conn,
                    principal_id=command.principal_id,
                    operation="SUBMIT_RUN",
                    scope_key=scope_key,
                    idempotency_key=command.idempotency_key,
                )
                if replay is not None:
                    if replay["request_hash"] != request_hash:
                        raise RunLifecycleError("IDEMPOTENCY_CONFLICT")
                    receipt = self._receipt_from_response(replay["response"], replayed=True)
                    if self._auto_profile:
                        await self._ensure_profile_runtime()
                        await self._process_profile_run(receipt.run_id)
                    return receipt

                release = await self._select_active_release(
                    conn,
                    policy_input=(
                        command.input
                        if isinstance(command.input, PolicyQaInput)
                        else None
                    ),
                )
                session_id = await self._resolve_session(conn, command)
                if command.parent_result_version is not None:
                    await self._validate_parent_result(
                        conn,
                        principal_id=command.principal_id,
                        session_id=session_id,
                        parent_result_version=command.parent_result_version,
                    )
                run_id = uuid4()
                await conn.execute(
                    """
                    INSERT INTO run (
                        run_id, session_id, principal_id, parent_result_version,
                        task_kind, task_input,
                        state, stage, state_version, release_id
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, 'QUEUED', 'INTAKE', 1, %s)
                    """,
                    (
                        run_id,
                        session_id,
                        command.principal_id,
                        command.parent_result_version,
                        command.task_kind,
                        Jsonb(asdict(command.input)),
                        release,
                    ),
                )
                if command.user_message.strip() and not any(
                    message.role == "USER"
                    and message.content.strip() == command.user_message.strip()
                    for message in command.messages
                ):
                    await self._store_messages(
                        conn,
                        session_id=session_id,
                        run_id=run_id,
                        messages=(
                            ChatMessageInput(
                                client_message_id=(
                                    f"server:{run_id}:user"
                                ),
                                role="USER",
                                kind="TEXT",
                                content=command.user_message.strip(),
                            ),
                        ),
                    )
                await self._store_messages(
                    conn,
                    session_id=session_id,
                    run_id=run_id,
                    messages=command.messages,
                )
                await self._update_session_for_run(
                    conn,
                    session_id=session_id,
                    command=command,
                    run_id=run_id,
                )
                await conn.execute(
                    """
                    INSERT INTO run_event (
                        run_id, event_seq, event_type, stage, state_version,
                        payload_version, payload
                    )
                    VALUES (%s, 1, 'STATE_CHANGED', 'INTAKE', 1, 1, %s)
                    """,
                    (
                        run_id,
                        Jsonb({"from_state": None, "to_state": "QUEUED"}),
                    ),
                )
                receipt = RunReceipt(
                    accepted=True,
                    reason=None,
                    run_id=run_id,
                    session_id=session_id,
                    state="QUEUED",
                    stage="INTAKE",
                    state_version=1,
                    first_event_seq=1,
                )
                await self._save_receipt(
                    conn,
                    principal_id=command.principal_id,
                    operation="SUBMIT_RUN",
                    scope_key=scope_key,
                    idempotency_key=command.idempotency_key,
                    request_hash=request_hash,
                    receipt=receipt,
                    target_run_id=run_id,
                )
                accepted_receipt = receipt
            if self._auto_profile:
                await self._ensure_profile_runtime()
                await self._process_profile_run(run_id)
            return accepted_receipt
        finally:
            await conn.close()

    async def GetRun(self, *, principal_id: str, run_id: UUID) -> RunView:
        conn = await self._connect()
        try:
            await self._raise_if_principal_unavailable(conn, principal_id)
            row = await (
                await conn.execute(
                    """
                    SELECT r.run_id, r.session_id, r.task_kind, r.state, r.stage,
                           r.terminal_reason, r.state_version, r.release_id,
                           r.confirmed_profile_version,
                           r.parent_result_version,
                           r.active_execution_remaining_ms, r.cancel_requested_at,
                           r.result_version, r.result_kind,
                           rr.payload AS result_payload,
                           package.package_sha256,
                           report.report_sha256,
                           report.report_json AS verification_report,
                           result_state.status AS result_state,
                           result_state.reason AS stale_reason,
                           current_interrupt.interrupt_id,
                           current_interrupt.interrupt_kind,
                           current_interrupt.interrupt_state,
                           current_interrupt.interrupt_question_key,
                           current_interrupt.interrupt_payload_version,
                           current_interrupt.interrupt_payload,
                           current_interrupt.interrupt_created_at,
                           current_interrupt.interrupt_expires_at,
                           r.created_at, r.updated_at, r.expires_at
                    FROM run r
                    LEFT JOIN run_result rr ON rr.result_version = r.result_version
                    LEFT JOIN run_evidence_package package
                           ON package.result_version = rr.result_version
                    LEFT JOIN run_verification_report report
                           ON report.result_version = rr.result_version
                    LEFT JOIN LATERAL (
                        SELECT state.status, state.reason
                        FROM run_result_state_event state
                        WHERE state.result_version = rr.result_version
                        ORDER BY state.event_id DESC
                        LIMIT 1
                    ) result_state ON TRUE
                           LEFT JOIN LATERAL (
                               SELECT interrupt_id,
                                      kind AS interrupt_kind,
                                      state AS interrupt_state,
                                      question_key AS interrupt_question_key,
                                      payload_version AS interrupt_payload_version,
                                      payload AS interrupt_payload,
                                      created_at AS interrupt_created_at,
                                      expires_at AS interrupt_expires_at
                               FROM run_interrupt
                               WHERE run_id = r.run_id
                                 AND state = 'OPEN'
                               ORDER BY created_at DESC
                               LIMIT 1
                           ) current_interrupt ON TRUE
                    WHERE r.run_id = %s AND r.principal_id = %s
                    """,
                    (run_id, principal_id),
                )
            ).fetchone()
            if row is None:
                raise RunLifecycleError("RUN_NOT_FOUND")
            result = None
            if row["result_version"] is not None and row["result_kind"] is not None:
                result = ResultReference(
                    result_version=row["result_version"],
                    result_kind=row["result_kind"],
                )
            formal_result = None
            if (
                result is not None
                and result.result_kind == "V1_FORMAL"
                and row["package_sha256"] is not None
                and row["report_sha256"] is not None
                and row["verification_report"] is not None
            ):
                formal_result = FormalResultView(
                    result_version=result.result_version,
                    result_kind=result.result_kind,
                    release_id=row["release_id"],
                    confirmed_profile_version=row["confirmed_profile_version"],
                    evidence_package_sha256=row["package_sha256"],
                    verification_report_sha256=row["report_sha256"],
                    verification_report=row["verification_report"],
                    report=(row["result_payload"] or {}).get("report", {}),
                    risk_notice=(row["result_payload"] or {}).get(
                        "risk_notice",
                        "",
                    ),
                    disclaimer=(row["result_payload"] or {}).get(
                        "disclaimer",
                        "",
                    ),
                    no_admission_guarantee=(
                        (row["result_payload"] or {}).get(
                            "no_admission_guarantee",
                            "",
                        )
                    ),
                    result_state=row["result_state"] or "ACTIVE",
                    stale_reason=row["stale_reason"],
                )
            conversation_answer = None
            if (
                result is not None
                and result.result_kind == "EXPLAIN_RESULT"
            ):
                conversation_answer = (
                    row["result_payload"] or {}
                ).get("answer")
            current_interrupt = None
            if row["interrupt_id"] is not None:
                current_interrupt = InterruptView(
                    interrupt_id=row["interrupt_id"],
                    kind=row["interrupt_kind"],
                    state=row["interrupt_state"],
                    question_key=row["interrupt_question_key"],
                    payload_version=row["interrupt_payload_version"],
                    payload=row["interrupt_payload"],
                    created_at=row["interrupt_created_at"],
                    expires_at=row["interrupt_expires_at"],
                )
            return RunView(
                run_id=row["run_id"],
                session_id=row["session_id"],
                task_kind=row["task_kind"],
                state=row["state"],
                stage=row["stage"],
                terminal_reason=row["terminal_reason"],
                state_version=row["state_version"],
                release_id=row["release_id"],
                confirmed_profile_version=row["confirmed_profile_version"],
                active_execution_remaining_ms=row["active_execution_remaining_ms"],
                cancel_requested_at=row["cancel_requested_at"],
                current_interrupt=current_interrupt,
                result=result,
                result_payload=row["result_payload"],
                formal_result=formal_result,
                parent_result_version=row["parent_result_version"],
                conversation_answer=(
                    str(conversation_answer)
                    if conversation_answer is not None
                    else None
                ),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                expires_at=row["expires_at"],
            )
        finally:
            await conn.close()

    async def ListSessions(
        self,
        *,
        principal_id: str,
        limit: int = 20,
    ) -> tuple[ChatSessionSummary, ...]:
        limit = max(1, min(limit, 100))
        conn = await self._connect()
        try:
            await self._raise_if_principal_unavailable(conn, principal_id)
            rows = await (
                await conn.execute(
                    """
                    SELECT cs.id AS session_id,
                           COALESCE(NULLIF(btrim(cs.title), ''), '新的志愿对话') AS title,
                           latest.run_id AS latest_run_id,
                           latest.state AS latest_state,
                           latest.result_version AS latest_result_version,
                           latest.result_kind AS latest_result_kind,
                           counts.run_count,
                           cs.created_at, cs.updated_at
                    FROM chat_session cs
                    LEFT JOIN LATERAL (
                        SELECT run_id, state, result_version, result_kind
                        FROM run
                        WHERE session_id = cs.id
                        ORDER BY created_at DESC, run_id DESC
                        LIMIT 1
                    ) latest ON TRUE
                    LEFT JOIN LATERAL (
                        SELECT COUNT(*)::int AS run_count
                        FROM run
                        WHERE session_id = cs.id
                    ) counts ON TRUE
                    WHERE cs.principal_id = %s
                      AND cs.status <> 'retention_expired'
                    ORDER BY cs.updated_at DESC, cs.id DESC
                    LIMIT %s
                    """,
                    (principal_id, limit),
                )
            ).fetchall()
            return tuple(self._session_summary(row) for row in rows)
        finally:
            await conn.close()

    async def GetSession(
        self,
        *,
        principal_id: str,
        session_id: UUID,
    ) -> ChatSessionView:
        conn = await self._connect()
        try:
            await self._raise_if_principal_unavailable(conn, principal_id)
            session = await (
                await conn.execute(
                    """
                    SELECT cs.id AS session_id,
                           COALESCE(NULLIF(btrim(cs.title), ''), '新的志愿对话') AS title,
                           latest.run_id AS latest_run_id,
                           latest.state AS latest_state,
                           latest.result_version AS latest_result_version,
                           latest.result_kind AS latest_result_kind,
                           counts.run_count,
                           cs.created_at, cs.updated_at
                    FROM chat_session cs
                    LEFT JOIN LATERAL (
                        SELECT run_id, state, result_version, result_kind
                        FROM run
                        WHERE session_id = cs.id
                        ORDER BY created_at DESC, run_id DESC
                        LIMIT 1
                    ) latest ON TRUE
                    LEFT JOIN LATERAL (
                        SELECT COUNT(*)::int AS run_count
                        FROM run
                        WHERE session_id = cs.id
                    ) counts ON TRUE
                    WHERE cs.id = %s AND cs.principal_id = %s
                    """,
                    (session_id, principal_id),
                )
            ).fetchone()
            if session is None:
                raise RunLifecycleError("SESSION_NOT_FOUND")
            message_rows = await (
                await conn.execute(
                    """
                    SELECT message_id, session_id, run_id, role, kind,
                           content, metadata, created_at
                    FROM chat_message
                    WHERE session_id = %s
                    ORDER BY message_seq
                    """,
                    (session_id,),
                )
            ).fetchall()
            run_rows = await (
                await conn.execute(
                    """
                    SELECT run_id, task_kind, state, stage, terminal_reason,
                           parent_result_version, result_version, result_kind,
                           created_at, updated_at
                    FROM run
                    WHERE session_id = %s
                    ORDER BY created_at, run_id
                    """,
                    (session_id,),
                )
            ).fetchall()
            profile = await (
                await conn.execute(
                    """
                    SELECT fields
                    FROM profile_revision
                    WHERE session_id = %s
                    ORDER BY revision_no DESC
                    LIMIT 1
                    """,
                    (session_id,),
                )
            ).fetchone()
            return ChatSessionView(
                summary=self._session_summary(session),
                messages=tuple(
                    ChatMessage(
                        message_id=row["message_id"],
                        session_id=row["session_id"],
                        run_id=row["run_id"],
                        role=row["role"],
                        kind=row["kind"],
                        content=row["content"],
                        metadata=dict(row["metadata"] or {}),
                        created_at=row["created_at"],
                    )
                    for row in message_rows
                ),
                runs=tuple(
                    SessionRunSummary(
                        run_id=row["run_id"],
                        task_kind=row["task_kind"],
                        state=row["state"],
                        stage=row["stage"],
                        terminal_reason=row["terminal_reason"],
                        parent_result_version=row["parent_result_version"],
                        result_version=row["result_version"],
                        result_kind=row["result_kind"],
                        created_at=row["created_at"],
                        updated_at=row["updated_at"],
                    )
                    for row in run_rows
                ),
                profile_fields=(
                    dict(profile["fields"]) if profile is not None else {}
                ),
            )
        finally:
            await conn.close()

    async def GetWishlistDraft(
        self,
        *,
        principal_id: str,
        result_version: UUID,
    ) -> WishlistDraft | None:
        conn = await self._connect()
        try:
            await self._raise_if_principal_unavailable(conn, principal_id)
            result_owner = await (
                await conn.execute(
                    """
                    SELECT 1
                    FROM run_result rr
                    JOIN run r ON r.run_id = rr.run_id
                    WHERE rr.result_version = %s
                      AND rr.result_kind = 'V1_FORMAL'
                      AND r.principal_id = %s
                    """,
                    (result_version, principal_id),
                )
            ).fetchone()
            if result_owner is None:
                raise RunLifecycleError("RESULT_NOT_FOUND")
            row = await (
                await conn.execute(
                    """
                    SELECT draft.draft_version_id, draft.result_version,
                           draft.run_id, draft.release_id, draft.draft_version,
                           draft.selections, draft.risk_summary,
                           draft.session_id, draft.created_at,
                           result.payload AS result_payload
                    FROM wishlist_draft_version AS draft
                    JOIN run_result AS result
                      ON result.result_version = draft.result_version
                    WHERE draft.principal_id = %s
                      AND draft.result_version = %s
                    ORDER BY draft.draft_version DESC
                    LIMIT 1
                    """,
                    (principal_id, result_version),
                )
            ).fetchone()
            if row is None:
                return None
            draft = self._wishlist_draft_from_row(row)
            if draft.risk is not None or not row.get("result_payload"):
                return draft
            try:
                risk = await self._summarize_draft_risk(
                    row["result_payload"],
                    draft.groups,
                    release_id=draft.release_id,
                    session_id=row["session_id"],
                )
            except RunLifecycleError as exc:
                log.warning(
                    "无法为历史草稿重算风险摘要: %s",
                    exc.reason,
                )
                return draft
            return replace(draft, risk=risk)
        finally:
            await conn.close()

    async def ListWishlists(
        self,
        *,
        principal_id: str,
        limit: int = 20,
        offset: int = 0,
        status_filter: str = "",
    ) -> tuple[WishlistSummary, ...]:
        if limit < 1 or limit > 100 or offset < 0:
            raise RunLifecycleError("DRAFT_COMMAND_INVALID")
        normalized_filter = status_filter.strip().upper()
        if normalized_filter not in {"", "CURRENT", "SUPERSEDED", "STALE"}:
            raise RunLifecycleError("DRAFT_COMMAND_INVALID")
        conn = await self._connect()
        try:
            await self._raise_if_principal_unavailable(conn, principal_id)
            rows = await (
                await conn.execute(
                    """
                    WITH latest AS (
                        SELECT DISTINCT ON (draft.result_version)
                               draft.draft_version_id,
                               draft.result_version,
                               draft.run_id,
                               draft.release_id,
                               draft.draft_version,
                               draft.selections,
                               draft.created_at
                        FROM wishlist_draft_version AS draft
                        WHERE draft.principal_id = %s
                        ORDER BY draft.result_version, draft.draft_version DESC
                    ),
                    classified AS (
                        SELECT latest.*,
                               run.session_id,
                               run.task_input,
                               COALESCE(result_state.status, 'ACTIVE') AS result_state,
                               result_state.reason AS result_state_reason,
                               release.status AS release_status,
                               successor.run_id AS successor_run_id,
                               CASE
                                   WHEN COALESCE(result_state.status, 'ACTIVE') = 'STALE'
                                        OR release.status <> 'PUBLISHED'
                                   THEN 'STALE'
                                   WHEN successor.run_id IS NOT NULL
                                   THEN 'SUPERSEDED'
                                   ELSE 'CURRENT'
                               END AS status
                        FROM latest
                        JOIN run ON run.run_id = latest.run_id
                        JOIN release ON release.release_id = latest.release_id
                        LEFT JOIN LATERAL (
                            SELECT state.status, state.reason
                            FROM run_result_state_event AS state
                            WHERE state.result_version = latest.result_version
                            ORDER BY state.event_id DESC
                            LIMIT 1
                        ) AS result_state ON TRUE
                        LEFT JOIN LATERAL (
                            SELECT child.run_id
                            FROM run AS child
                            JOIN run_result AS child_result
                              ON child_result.run_id = child.run_id
                             AND child_result.result_kind = 'V1_FORMAL'
                            WHERE child.session_id = run.session_id
                              AND child.task_kind = 'REVISE_PLAN'
                              AND child.parent_result_version = latest.result_version
                              AND child.state = 'SUCCEEDED'
                            ORDER BY child.created_at, child.run_id
                            LIMIT 1
                        ) AS successor ON TRUE
                    )
                    SELECT *
                    FROM classified
                    WHERE (%s = '' OR status = %s)
                    ORDER BY created_at DESC, result_version
                    LIMIT %s OFFSET %s
                    """,
                    (principal_id, normalized_filter, normalized_filter, limit, offset),
                )
            ).fetchall()
        finally:
            await conn.close()
        items: list[WishlistSummary] = []
        for row in rows:
            task_input = row["task_input"] or {}
            application_year = int(task_input.get("application_year") or 0)
            category = str(task_input.get("category") or "")
            batch = str(task_input.get("batch") or "")
            stale_reason = row["result_state_reason"]
            if row["status"] == "STALE" and not stale_reason:
                stale_reason = "RELEASE_REVOKED"
            title_parts = [
                str(application_year) if application_year else "",
                category,
                batch,
                "志愿表",
            ]
            items.append(
                WishlistSummary(
                    draft_version_id=row["draft_version_id"],
                    result_version=row["result_version"],
                    run_id=row["run_id"],
                    release_id=row["release_id"],
                    draft_version=int(row["draft_version"]),
                    title=" ".join(value for value in title_parts if value),
                    application_year=application_year,
                    category=category,
                    batch=batch,
                    group_count=len(row["selections"]),
                    major_count=sum(
                        len(group.get("major_codes") or ())
                        for group in row["selections"]
                    ),
                    status=row["status"],
                    stale_reason=stale_reason,
                    successor_run_id=row["successor_run_id"],
                    saved_at=row["created_at"],
                )
            )
        return tuple(items)

    async def SaveWishlistDraft(
        self,
        *,
        principal_id: str,
        result_version: UUID,
        expected_version: int,
        idempotency_key: str,
        groups: tuple[WishlistDraftGroup, ...],
    ) -> WishlistDraft:
        if not idempotency_key.strip() or expected_version < 0:
            raise RunLifecycleError("DRAFT_COMMAND_INVALID")
        normalized = self._normalize_draft_groups(groups)
        conn = await self._connect(autocommit=True)
        try:
            await self._raise_if_principal_unavailable(conn, principal_id)
            async with conn.transaction():
                await (
                    await conn.execute(
                        """
                        SELECT pg_advisory_xact_lock(hashtext(%s))
                        """,
                        (f"{principal_id}|{result_version}",),
                    )
                ).fetchone()
                result_row = await (
                    await conn.execute(
                        """
                        SELECT rr.run_id, rr.release_id, r.session_id, rr.payload,
                               rel.status AS release_status,
                               result_state.status AS result_state
                        FROM run_result rr
                        JOIN run r ON r.run_id = rr.run_id
                        JOIN release rel ON rel.release_id = rr.release_id
                        LEFT JOIN LATERAL (
                            SELECT state.status
                            FROM run_result_state_event state
                            WHERE state.result_version = rr.result_version
                            ORDER BY state.event_id DESC
                            LIMIT 1
                        ) result_state ON TRUE
                        WHERE rr.result_version = %s
                          AND rr.result_kind = 'V1_FORMAL'
                          AND r.principal_id = %s
                        """,
                        (result_version, principal_id),
                    )
                ).fetchone()
                if result_row is None:
                    raise RunLifecycleError("RESULT_NOT_FOUND")
                if result_row["release_status"] != "PUBLISHED":
                    raise RunLifecycleError("RELEASE_REVOKED")
                if result_row["result_state"] == "STALE":
                    raise RunLifecycleError("RESULT_STALE")
                validated = self._validate_draft_selections(
                    result_row["payload"],
                    normalized,
                )
                request_sha256 = sha256_payload(
                    {
                        "result_version": str(result_version),
                        "groups": [
                            {
                                "university_code": group.university_code,
                                "major_group": group.major_group,
                                "major_codes": list(group.major_codes),
                            }
                            for group in validated
                        ],
                    }
                )
                replayed = await (
                    await conn.execute(
                        """
                        SELECT draft_version_id, result_version, run_id,
                               release_id, draft_version, selections,
                               risk_summary, request_sha256, created_at
                        FROM wishlist_draft_version
                        WHERE principal_id = %s
                          AND result_version = %s
                          AND idempotency_key = %s
                        """,
                        (
                            principal_id,
                            result_version,
                            idempotency_key,
                        ),
                    )
                ).fetchone()
                if replayed is not None:
                    if replayed["request_sha256"] != request_sha256:
                        raise RunLifecycleError("IDEMPOTENCY_CONFLICT")
                    return self._wishlist_draft_from_row(replayed)
                latest = await (
                    await conn.execute(
                        """
                        SELECT draft_version_id, result_version, run_id,
                               release_id, draft_version, selections,
                               risk_summary, idempotency_key, request_sha256,
                               created_at
                        FROM wishlist_draft_version
                        WHERE principal_id = %s AND result_version = %s
                        ORDER BY draft_version DESC
                        LIMIT 1
                        """,
                        (principal_id, result_version),
                    )
                ).fetchone()
                current_version = (
                    int(latest["draft_version"]) if latest is not None else 0
                )
                if current_version != expected_version:
                    raise RunLifecycleError(
                        "DRAFT_VERSION_CONFLICT",
                        f"current draft version is {current_version}",
                    )
                risk = await self._summarize_draft_risk(
                    result_row["payload"],
                    validated,
                    release_id=result_row["release_id"],
                    session_id=result_row["session_id"],
                )
                draft_version_id = uuid4()
                draft_version = current_version + 1
                row = await (
                    await conn.execute(
                        """
                        INSERT INTO wishlist_draft_version (
                            draft_version_id, principal_id, session_id,
                            run_id, result_version, release_id,
                            draft_version, selections, risk_summary,
                            idempotency_key, request_sha256
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING draft_version_id, result_version, run_id,
                                  release_id, draft_version, selections,
                                  risk_summary, created_at
                        """,
                        (
                            draft_version_id,
                            principal_id,
                            result_row["session_id"],
                            result_row["run_id"],
                            result_version,
                            result_row["release_id"],
                            draft_version,
                            Jsonb([
                                {
                                    "university_code": group.university_code,
                                    "major_group": group.major_group,
                                    "major_codes": list(group.major_codes),
                                }
                                for group in validated
                            ]),
                            Jsonb(self._draft_risk_payload(risk)),
                            idempotency_key,
                            request_sha256,
                        ),
                    )
                ).fetchone()
                return self._wishlist_draft_from_row(row)
        finally:
            await conn.close()

    async def ListWishlistHistory(
        self,
        *,
        principal_id: str,
        result_version: UUID,
    ) -> tuple[WishlistDraft, ...]:
        conn = await self._connect()
        try:
            await self._raise_if_principal_unavailable(conn, principal_id)
            rows = await (
                await conn.execute(
                    """
                    SELECT draft.draft_version_id, draft.result_version,
                           draft.run_id, draft.release_id, draft.draft_version,
                           draft.selections, draft.risk_summary,
                           draft.created_at
                    FROM wishlist_draft_version AS draft
                    JOIN run AS run ON run.run_id = draft.run_id
                    WHERE draft.principal_id = %s
                      AND draft.result_version = %s
                      AND run.principal_id = %s
                    ORDER BY draft.draft_version DESC
                    """,
                    (principal_id, result_version, principal_id),
                )
            ).fetchall()
        finally:
            await conn.close()
        if not rows:
            raise RunLifecycleError("RESULT_NOT_FOUND")
        return tuple(self._wishlist_draft_from_row(row) for row in rows)

    async def RestoreWishlistDraftVersion(
        self,
        *,
        principal_id: str,
        result_version: UUID,
        draft_version: int,
        idempotency_key: str,
    ) -> WishlistDraft:
        if draft_version < 1 or not idempotency_key.strip():
            raise RunLifecycleError("DRAFT_COMMAND_INVALID")
        conn = await self._connect()
        try:
            await self._raise_if_principal_unavailable(conn, principal_id)
            source = await (
                await conn.execute(
                    """
                    SELECT draft.draft_version_id, draft.result_version,
                           draft.run_id, draft.release_id, draft.draft_version,
                           draft.selections, draft.risk_summary,
                           draft.created_at
                    FROM wishlist_draft_version AS draft
                    JOIN run AS run ON run.run_id = draft.run_id
                    WHERE draft.principal_id = %s
                      AND draft.result_version = %s
                      AND draft.draft_version = %s
                      AND run.principal_id = %s
                    """,
                    (
                        principal_id,
                        result_version,
                        draft_version,
                        principal_id,
                    ),
                )
            ).fetchone()
            latest = await (
                await conn.execute(
                    """
                    SELECT draft_version
                    FROM wishlist_draft_version
                    WHERE principal_id = %s AND result_version = %s
                    ORDER BY draft_version DESC
                    LIMIT 1
                    """,
                    (principal_id, result_version),
                )
            ).fetchone()
        finally:
            await conn.close()
        if source is None:
            raise RunLifecycleError("RESULT_NOT_FOUND")
        source_draft = self._wishlist_draft_from_row(source)
        return await self.SaveWishlistDraft(
            principal_id=principal_id,
            result_version=result_version,
            expected_version=(
                int(latest["draft_version"]) if latest is not None else 0
            ),
            idempotency_key=idempotency_key,
            groups=source_draft.groups,
        )

    async def DeleteWishlist(
        self,
        *,
        principal_id: str,
        result_version: UUID,
        request_id: str,
    ) -> WishlistDeletionReceipt:
        if not request_id.strip():
            raise RunLifecycleError("DRAFT_COMMAND_INVALID")
        conn = await self._connect(autocommit=True)
        try:
            await self._raise_if_principal_unavailable(conn, principal_id)
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"wishlist-delete:{principal_id}:{result_version}",),
                )
                existing = await (
                    await conn.execute(
                        """
                        SELECT request_id
                        FROM wishlist_deletion
                        WHERE principal_id = %s AND result_version = %s
                        """,
                        (principal_id, result_version),
                    )
                ).fetchone()
                if existing is not None:
                    return WishlistDeletionReceipt(
                        deleted=True,
                        request_id=existing["request_id"],
                    )
                owner = await (
                    await conn.execute(
                        """
                        SELECT rr.run_id
                        FROM run_result AS rr
                        JOIN run AS run ON run.run_id = rr.run_id
                        WHERE rr.result_version = %s
                          AND rr.result_kind = 'V1_FORMAL'
                          AND run.principal_id = %s
                        """,
                        (result_version, principal_id),
                    )
                ).fetchone()
                if owner is None:
                    raise RunLifecycleError("RESULT_NOT_FOUND")
                await conn.execute(
                    """
                    INSERT INTO wishlist_deletion (
                        principal_id, result_version, run_id, request_id
                    )
                    VALUES (%s, %s, %s, %s)
                    """,
                    (principal_id, result_version, owner["run_id"], request_id),
                )
                await conn.execute(
                    """
                    DELETE FROM wishlist_draft_version
                    WHERE principal_id = %s AND result_version = %s
                    """,
                    (principal_id, result_version),
                )
                return WishlistDeletionReceipt(
                    deleted=True,
                    request_id=request_id,
                )
        finally:
            await conn.close()

    async def RevokeSensitiveConsent(self, *, principal_id: str) -> int:
        conn = await self._connect()
        try:
            await self._raise_if_principal_unavailable(conn, principal_id)
            result = await conn.execute(
                """
                UPDATE consent_record
                SET revoked_at = COALESCE(revoked_at, NOW())
                WHERE principal_id = %s
                  AND purpose = 'SENSITIVE_ELIGIBILITY_EVALUATION'
                  AND revoked_at IS NULL
                """,
                (principal_id,),
            )
            return result.rowcount
        finally:
            await conn.close()

    @staticmethod
    def _normalize_draft_groups(
        groups: tuple[WishlistDraftGroup, ...],
    ) -> tuple[WishlistDraftGroup, ...]:
        if not 1 <= len(groups) <= 45:
            raise RunLifecycleError("DRAFT_SELECTION_INVALID")
        normalized = []
        seen_groups: set[tuple[str, str]] = set()
        for group in groups:
            university_code = group.university_code.strip()
            major_group = group.major_group.strip()
            major_codes = tuple(code.strip() for code in group.major_codes)
            if (
                not university_code
                or not major_group
                or not 1 <= len(major_codes) <= 6
                or any(not code for code in major_codes)
                or len(set(major_codes)) != len(major_codes)
            ):
                raise RunLifecycleError("DRAFT_SELECTION_INVALID")
            identity = (university_code, major_group)
            if identity in seen_groups:
                raise RunLifecycleError("DRAFT_SELECTION_INVALID")
            seen_groups.add(identity)
            normalized.append(WishlistDraftGroup(
                university_code=university_code,
                major_group=major_group,
                major_codes=major_codes,
            ))
        return tuple(normalized)

    @staticmethod
    def _validate_draft_selections(
        result_payload: dict[str, Any],
        groups: tuple[WishlistDraftGroup, ...],
    ) -> tuple[WishlistDraftGroup, ...]:
        sections = (
            (result_payload or {}).get("report", {}).get("sections", ())
        )
        candidate_groups: dict[tuple[str, str], dict[str, Any]] = {}
        for section in sections:
            if not str(section.get("section_key", "")).startswith("candidate:"):
                continue
            university_code = str(section.get("university_code") or "")
            major_group = str(section.get("major_group") or "")
            if not university_code or not major_group:
                raise RunLifecycleError("REPORT_STRUCTURED_MAJORS_MISSING")
            identity = (university_code, major_group)
            majors = {
                str(major.get("major_code") or ""): major
                for major in section.get("majors", ())
            }
            if not majors:
                raise RunLifecycleError("REPORT_STRUCTURED_MAJORS_MISSING")
            candidate_groups[identity] = {
                "final_rank": int(section.get("final_rank") or 0),
                "majors": majors,
                "university_name": str(
                    section.get("university_name") or ""
                ),
            }
        if not candidate_groups:
            raise RunLifecycleError("REPORT_STRUCTURED_MAJORS_MISSING")
        validated = []
        for group in groups:
            identity = (group.university_code, group.major_group)
            candidate = candidate_groups.get(identity)
            if candidate is None:
                raise RunLifecycleError("DRAFT_SELECTION_INVALID")
            for code in group.major_codes:
                major = candidate["majors"].get(code)
                if (
                    major is None
                    or not bool(major.get("selectable", False))
                ):
                    raise RunLifecycleError("DRAFT_SELECTION_INVALID")
            validated.append(group)
        return tuple(validated)

    async def _summarize_draft_risk(
        self,
        result_payload: dict[str, Any],
        groups: tuple[WishlistDraftGroup, ...],
        *,
        release_id: str,
        session_id: UUID,
    ) -> WishlistDraftRisk:
        if self._algo_client is None:
            raise RunLifecycleError(
                "DEPENDENCY_UNAVAILABLE",
                "svc-algo 不可用，无法计算草稿风险",
            )
        risk_groups = self._draft_risk_groups(result_payload, groups)
        request = algo_pb2.SummarizeWishlistDraftRequest(
            trace=common_pb2.Trace(
                trace_id=release_id,
                session_id=str(session_id),
                node="wishlist_draft_risk",
            ),
            release_id=release_id,
            groups=risk_groups,
        )
        try:
            reply = await self._algo_client.SummarizeWishlistDraft(request)
        except grpc.RpcError as exc:
            reason = grpc_error_reason(exc)
            if reason == "INVALID_CANDIDATE":
                raise RunLifecycleError(
                    "DRAFT_SELECTION_INVALID",
                    "草稿风险输入非法",
                ) from exc
            raise RunLifecycleError(
                "DEPENDENCY_UNAVAILABLE",
                f"svc-algo 不可用: {exc.code().name}",
            ) from exc
        if not reply.HasField("summary"):
            raise RunLifecycleError(
                "VALIDATION_FAILED",
                "svc-algo 未返回草稿风险摘要",
            )
        summary = reply.summary
        if summary.group_count != len(groups):
            raise RunLifecycleError(
                "VALIDATION_FAILED",
                "草稿风险摘要组数不一致",
            )
        if (
            min(
                summary.rush_count,
                summary.stable_count,
                summary.safe_tier_count,
                summary.safe_guard_count,
                summary.safe_guard_target,
            )
            < 0
            or not 0 <= summary.slip_rate <= 1
        ):
            raise RunLifecycleError(
                "VALIDATION_FAILED",
                "草稿风险摘要数值非法",
            )
        expected = None
        if summary.HasField("expected_satisfaction"):
            expected = float(summary.expected_satisfaction)
            if not 0 <= expected <= 1:
                raise RunLifecycleError(
                    "VALIDATION_FAILED",
                    "草稿期望满意度非法",
                )
        return WishlistDraftRisk(
            group_count=summary.group_count,
            rush_count=summary.rush_count,
            stable_count=summary.stable_count,
            safe_tier_count=summary.safe_tier_count,
            safe_guard_count=summary.safe_guard_count,
            safe_guard_target=summary.safe_guard_target,
            safe_guard_ok=bool(summary.safe_guard_ok),
            slip_rate=float(summary.slip_rate),
            expected_satisfaction=expected,
        )

    @staticmethod
    def _draft_risk_groups(
        result_payload: dict[str, Any],
        groups: tuple[WishlistDraftGroup, ...],
    ) -> list[Any]:
        sections = (
            (result_payload or {}).get("report", {}).get("sections", ())
        )
        by_identity: dict[tuple[str, str], dict[str, Any]] = {}
        for section in sections:
            if not str(section.get("section_key", "")).startswith("candidate:"):
                continue
            identity = (
                str(section.get("university_code") or ""),
                str(section.get("major_group") or ""),
            )
            by_identity[identity] = section
        result = []
        for group in groups:
            section = by_identity.get(
                (group.university_code, group.major_group)
            )
            if section is None:
                raise RunLifecycleError("DRAFT_SELECTION_INVALID")
            probability = section.get("probability")
            category = str(section.get("strategy_tier") or "")
            if (
                not isinstance(probability, (int, float))
                or isinstance(probability, bool)
                or not 0.01 <= float(probability) <= 0.99
                or category not in {"冲", "稳", "保"}
            ):
                raise RunLifecycleError(
                    "REPORT_STRUCTURED_MAJORS_MISSING",
                )
            item = algo_pb2.DraftGroupRiskInput(
                university_code=group.university_code,
                major_group=group.major_group,
                probability=float(probability),
                category=category,
            )
            utility = section.get("utility_score")
            if utility is not None:
                if (
                    not isinstance(utility, (int, float))
                    or isinstance(utility, bool)
                    or not 0 <= float(utility) <= 1
                ):
                    raise RunLifecycleError("REPORT_STRUCTURED_MAJORS_MISSING")
                item.utility_score = float(utility)
            result.append(item)
        return result

    @staticmethod
    def _draft_risk_payload(risk: WishlistDraftRisk) -> dict[str, Any]:
        return {
            "group_count": risk.group_count,
            "rush_count": risk.rush_count,
            "stable_count": risk.stable_count,
            "safe_tier_count": risk.safe_tier_count,
            "safe_guard_count": risk.safe_guard_count,
            "safe_guard_target": risk.safe_guard_target,
            "safe_guard_ok": risk.safe_guard_ok,
            "slip_rate": risk.slip_rate,
            "expected_satisfaction": risk.expected_satisfaction,
        }

    @staticmethod
    def _draft_risk_from_dict(value: Any) -> WishlistDraftRisk | None:
        if not isinstance(value, dict):
            return None
        count_fields = (
            "group_count",
            "rush_count",
            "stable_count",
            "safe_tier_count",
            "safe_guard_count",
            "safe_guard_target",
        )
        counts = {}
        for field in count_fields:
            raw = value.get(field)
            if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
                return None
            counts[field] = raw
        slip_rate = value.get("slip_rate")
        if (
            not isinstance(slip_rate, (int, float))
            or isinstance(slip_rate, bool)
            or not 0 <= float(slip_rate) <= 1
            or not isinstance(value.get("safe_guard_ok"), bool)
        ):
            return None
        expected = value.get("expected_satisfaction")
        if expected is not None and (
            not isinstance(expected, (int, float))
            or isinstance(expected, bool)
            or not 0 <= float(expected) <= 1
        ):
            return None
        return WishlistDraftRisk(
            **counts,
            safe_guard_ok=value["safe_guard_ok"],
            slip_rate=float(slip_rate),
            expected_satisfaction=(
                float(expected) if expected is not None else None
            ),
        )

    @staticmethod
    def _wishlist_draft_from_row(row: dict[str, Any]) -> WishlistDraft:
        groups = tuple(
            WishlistDraftGroup(
                university_code=str(item.get("university_code") or ""),
                major_group=str(item.get("major_group") or ""),
                major_codes=tuple(
                    str(code)
                    for code in item.get("major_codes", ())
                ),
            )
            for item in row["selections"]
        )
        return WishlistDraft(
            draft_version_id=row["draft_version_id"],
            result_version=row["result_version"],
            run_id=row["run_id"],
            release_id=row["release_id"],
            draft_version=int(row["draft_version"]),
            groups=groups,
            created_at=row["created_at"],
            risk=RunLifecycle._draft_risk_from_dict(
                row.get("risk_summary")
            ),
        )

    async def SubmitConversationMessage(
        self,
        *,
        principal_id: str,
        session_id: UUID,
        profile: StructuredProfileInput,
        parent_result_version: UUID,
        message_text: str,
        idempotency_key: str,
        region_answer: RegionAnswer | None = None,
    ) -> ConversationSubmission:
        text = message_text.strip()
        if not text:
            raise RunLifecycleError("MESSAGE_REQUIRED")
        safety_message = self._safety_response(text)
        if safety_message is not None:
            return await self._conversation_only_response(
                principal_id=principal_id,
                session_id=session_id,
                message_text=text,
                idempotency_key=idempotency_key,
                status="safety_support",
                assistant_message=safety_message,
            )
        active_region = await self._active_region_clarification(
            principal_id=principal_id,
            session_id=session_id,
        )
        region_resolution_metadata: dict[str, Any] | None = None
        if region_answer is not None:
            if active_region is None:
                raise RunLifecycleError("REGION_CLARIFICATION_NOT_FOUND")
            clarification = active_region["clarification"]
            if region_answer.clarification_id != clarification["clarification_id"]:
                raise RunLifecycleError("REGION_CLARIFICATION_CONFLICT")
            parent_context = await self._parent_result_context(
                principal_id=principal_id,
                session_id=session_id,
                parent_result_version=parent_result_version,
            )
            if parent_context is None:
                raise RunLifecycleError("PARENT_RESULT_NOT_FOUND")
            release_id = parent_context[0]
            catalog = await self._region_catalog(release_id)
            try:
                provinces = validate_region_answer(
                    catalog=catalog,
                    catalog_version=region_answer.catalog_version,
                    provinces=region_answer.provinces,
                    preset_ids=region_answer.preset_ids,
                    cancel_region_restriction=(
                        region_answer.cancel_region_restriction
                    ),
                )
            except MacroRegionError as exc:
                if exc.reason == "REGION_ANSWER_INVALID":
                    return await self._region_clarification_response(
                        principal_id=principal_id,
                        session_id=session_id,
                        message_text=text,
                        idempotency_key=idempotency_key,
                        clarification=clarification,
                        assistant_message=(
                            "我没能确认这次省份选择。请重新选择具体省份；"
                            "如果确实不想继续追问，请明确选择“取消该地域限制”。"
                        ),
                    )
                raise RunLifecycleError(exc.reason) from exc
            region_resolution_metadata = {
                "region_clarification_id": clarification["clarification_id"],
                "selected_preset_ids": list(region_answer.preset_ids),
                "region_catalog_version": catalog.version,
                "resolved_provinces": list(provinces),
            }
            if region_answer.cancel_region_restriction:
                return await self._conversation_only_response(
                    principal_id=principal_id,
                    session_id=session_id,
                    message_text=text,
                    idempotency_key=idempotency_key,
                    status="clarification_resolved",
                    assistant_message=(
                        "已取消这次地域限制；当前方案保持不变。"
                        "你可以继续提出其他筛选条件。"
                    ),
                    user_metadata={
                        "resolves_region_clarification_id": (
                            clarification["clarification_id"]
                        ),
                        **region_resolution_metadata,
                    },
                )
            revised_profile = self._apply_resolved_region_to_profile(
                profile,
                operation=clarification["operation"],
                provinces=provinces,
            )
            decision = "REVISE_PLAN"
        elif active_region is not None:
            clarification = active_region["clarification"]
            if self._cancels_region_restriction(text):
                return await self._conversation_only_response(
                    principal_id=principal_id,
                    session_id=session_id,
                    message_text=text,
                    idempotency_key=idempotency_key,
                    status="clarification_resolved",
                    assistant_message=(
                        "已取消这次地域限制；当前方案保持不变。"
                        "你可以继续提出其他筛选条件。"
                    ),
                    user_metadata={
                        "resolves_region_clarification_id": (
                            clarification["clarification_id"]
                        )
                    },
                )
            return await self._region_clarification_response(
                principal_id=principal_id,
                session_id=session_id,
                message_text=text,
                idempotency_key=idempotency_key,
                clarification=clarification,
                assistant_message=(
                    "还需要先确认具体省份：你不想去哪些省份？"
                    "可以多选省份，也可以使用上方的大区快捷组。"
                ),
            )
        else:
            macro_region = detect_macro_region(text)
            if macro_region is not None:
                parent_context = await self._parent_result_context(
                    principal_id=principal_id,
                    session_id=session_id,
                    parent_result_version=parent_result_version,
                )
                if parent_context is None:
                    raise RunLifecycleError("PARENT_RESULT_NOT_FOUND")
                catalog = await self._region_catalog(parent_context[0])
                clarification = clarification_payload(
                    clarification_id=str(uuid4()),
                    expression=macro_region,
                    catalog=catalog,
                )
                return await self._region_clarification_response(
                    principal_id=principal_id,
                    session_id=session_id,
                    message_text=text,
                    idempotency_key=idempotency_key,
                    clarification=clarification,
                    assistant_message=self._region_question(macro_region.term),
                )
            revised_profile = profile
        decision = (
            "REVISE_PLAN"
            if region_answer is not None
            else self._conversation_decision(text)
        )
        more_rush_requested = self._requests_more_rush(text)
        llm_patch: dict[str, Any] | None = None
        intent_degraded_reason: str | None = None
        if region_answer is not None:
            more_rush_requested = False
        if decision == "REVISE_PLAN":
            if region_answer is None:
                revised_profile = self._apply_conversation_constraints(
                    profile,
                    text,
                )
            if more_rush_requested:
                revised_profile = replace(
                    revised_profile,
                    risk_appetite="aggressive",
                    allocation_request="MORE_RUSH",
                )
            if revised_profile == profile:
                decision = "CLARIFY"

        if decision == "CLARIFY" and self._conversation_intent_classifier is not None:
            parent_context = await self._parent_result_context(
                principal_id=principal_id,
                session_id=session_id,
                parent_result_version=parent_result_version,
            )
            if parent_context:
                (
                    release_id,
                    parent_run_id,
                    rush_count,
                    stable_count,
                    safe_count,
                ) = parent_context
                try:
                    intent = (
                        await self._conversation_intent_classifier.classify(
                            ConversationIntentRequest(
                                principal_id=principal_id,
                                session_id=str(session_id),
                                run_id=str(parent_run_id),
                                request_id=idempotency_key,
                                parent_result_version=str(
                                    parent_result_version
                                ),
                                release_id=release_id,
                                message_text=text,
                                risk_appetite=(
                                    profile.risk_appetite or "balanced"
                                ),
                                rush_count=rush_count,
                                stable_count=stable_count,
                                safe_count=safe_count,
                            )
                        )
                    )
                except Exception as exc:
                    intent = None
                    intent_degraded_reason = "LLM_UNAVAILABLE"
                    logger.warning(
                        "conversation intent deterministic fallback: %s",
                        type(exc).__name__,
                    )
                if intent is not None:
                    decision = intent.decision
                    if decision == "REVISE_PLAN":
                        llm_patch = intent.patch
                        try:
                            revised_profile = self._apply_conversation_patch(
                                profile,
                                llm_patch,
                            )
                        except RunLifecycleError:
                            decision = "CLARIFY"
                            intent_degraded_reason = "LLM_OUTPUT_REJECTED"
                        else:
                            if revised_profile == profile:
                                decision = "CLARIFY"
                                intent_degraded_reason = (
                                    "LLM_NO_PROFILE_PATCH"
                                )

        if decision in {"CLARIFY", "OFF_TOPIC", "OUT_OF_SCOPE"}:
            assistant_message, status = {
                "CLARIFY": (
                    "你是想解释当前报告中的排序和概率，还是按冲稳保比例、"
                    "城市、专业、学校层次等条件重新生成一版方案？",
                    "clarification_required",
                ),
                "OFF_TOPIC": (
                    "我主要负责湖南 2025 本科批的志愿规划、院校专业和"
                    "报告解释，不能可靠回答这个问题。你可以继续问录取规则、"
                    "专业方向，或让我调整当前志愿方案。",
                    "off_topic",
                ),
                "OUT_OF_SCOPE": (
                    "当前仅支持湖南 2025 本科批物理类/历史类志愿规划，"
                    "其他省份、年份、升学类型或艺术体育等范围还不能可靠处理。"
                    "你可以继续问本范围内的院校专业、录取规则，或调整当前方案。",
                    "out_of_scope",
                ),
            }[decision]
            return await self._conversation_only_response(
                principal_id=principal_id,
                session_id=session_id,
                message_text=text,
                idempotency_key=idempotency_key,
                status=status,
                assistant_message=assistant_message,
                intent_degraded_reason=intent_degraded_reason,
                intent_decision=decision,
            )

        if (
            decision == "REVISE_PLAN"
            and revised_profile.risk_appetite == "aggressive"
            and profile.risk_appetite != "aggressive"
        ):
            revised_profile = replace(
                revised_profile,
                allocation_request="MORE_RUSH",
            )

        if decision != "REVISE_PLAN":
            assistant_message = "正在根据当前已核验报告生成解释。"
        elif more_rush_requested:
            assistant_message = (
                "已收到增加冲档请求，正在核验是否还有可用冲档候选。"
                "请确认更新后的画像。"
            )
        else:
            assistant_message = (
                "已收到筛选条件，正在重新计算方案。请确认更新后的画像。"
            )
        receipt = await self.SubmitRun(
            SubmitRunCommand(
                principal_id=principal_id,
                session_id=session_id,
                task_kind=decision,
                input=revised_profile,
                idempotency_key=idempotency_key,
                parent_result_version=parent_result_version,
                messages=(
                    *(
                        (
                            ChatMessageInput(
                                client_message_id=(
                                    f"conversation:{idempotency_key}:user-region"
                                ),
                                role="USER",
                                kind="TEXT",
                                content=text,
                                metadata=region_resolution_metadata,
                            ),
                        )
                        if region_resolution_metadata
                        else ()
                    ),
                    ChatMessageInput(
                        client_message_id=(
                            f"conversation:{idempotency_key}:assistant"
                        ),
                        role="ASSISTANT",
                        kind="TEXT",
                        content=assistant_message,
                        metadata=(
                            {
                                "region_clarification_resolved": True,
                                **region_resolution_metadata,
                            }
                            if region_resolution_metadata
                            else None
                        ),
                    ),
                ),
                user_message=text,
            )
        )
        return ConversationSubmission(
            session_id=receipt.session_id,
            run_id=receipt.run_id,
            task_kind=decision,
            status="accepted",
            assistant_message=assistant_message,
        )

    async def _conversation_only_response(
        self,
        *,
        principal_id: str,
        session_id: UUID,
        message_text: str,
        idempotency_key: str,
        status: str,
        assistant_message: str,
        intent_degraded_reason: str | None = None,
        intent_decision: str | None = None,
        user_metadata: dict[str, Any] | None = None,
        assistant_metadata: dict[str, Any] | None = None,
        region_clarification: dict[str, Any] | None = None,
    ) -> ConversationSubmission:
        conn = await self._connect()
        try:
            async with conn.transaction():
                await self._raise_if_principal_unavailable(conn, principal_id)
                session = await (
                    await conn.execute(
                        """
                        SELECT id FROM chat_session
                        WHERE id = %s AND principal_id = %s
                        """,
                        (session_id, principal_id),
                    )
                ).fetchone()
                if session is None:
                    raise RunLifecycleError("RUN_FORBIDDEN")
                metadata = {}
                if intent_degraded_reason:
                    metadata.update(
                        {
                            "generation_mode": "DETERMINISTIC_FALLBACK",
                            "degradation_reason": intent_degraded_reason,
                        }
                    )
                if intent_decision:
                    metadata["intent_decision"] = intent_decision
                metadata.update(assistant_metadata or {})
                await self._store_messages(
                    conn,
                    session_id=session_id,
                    run_id=None,
                    messages=(
                        ChatMessageInput(
                            client_message_id=(
                                f"conversation:{idempotency_key}:user"
                            ),
                            role="USER",
                            kind="TEXT",
                            content=message_text,
                            metadata=user_metadata,
                        ),
                        ChatMessageInput(
                            client_message_id=(
                                f"conversation:{idempotency_key}:assistant"
                            ),
                            role="ASSISTANT",
                            kind="TEXT",
                            content=assistant_message,
                            metadata=metadata or None,
                        ),
                    ),
                )
                await conn.execute(
                    "UPDATE chat_session SET updated_at = NOW() WHERE id = %s",
                    (session_id,),
                )
        finally:
            await conn.close()
        return ConversationSubmission(
            session_id=session_id,
            run_id=None,
            task_kind=None,
            status=status,
            assistant_message=assistant_message,
            region_clarification=region_clarification,
        )

    async def _region_clarification_response(
        self,
        *,
        principal_id: str,
        session_id: UUID,
        message_text: str,
        idempotency_key: str,
        clarification: dict[str, Any],
        assistant_message: str,
    ) -> ConversationSubmission:
        return await self._conversation_only_response(
            principal_id=principal_id,
            session_id=session_id,
            message_text=message_text,
            idempotency_key=idempotency_key,
            status="clarification_required",
            assistant_message=assistant_message,
            intent_decision="CLARIFY",
            assistant_metadata={
                "clarification_kind": "SELECT_PROVINCES",
                "reason_code": MACRO_REGION_SCOPE_UNRESOLVED,
                "region_clarification": clarification,
            },
            region_clarification=clarification,
        )

    async def _region_catalog(self, release_id: str) -> RegionCatalog:
        conn = await self._connect()
        try:
            return await load_region_catalog(conn, release_id)
        except MacroRegionError as exc:
            raise RunLifecycleError(exc.reason) from exc
        finally:
            await conn.close()

    async def _unresolved_run_macro_region(
        self,
        conn: psycopg.AsyncConnection,
        *,
        run_id: UUID,
        fields: dict[str, Any],
    ) -> tuple[Any, RegionCatalog] | None:
        required = (fields.get("preferences.required_provinces") or {}).get(
            "value"
        ) or ()
        excluded = (fields.get("preferences.excluded_provinces") or {}).get(
            "value"
        ) or ()
        if required or excluded:
            return None
        handled = await (
            await conn.execute(
                """
                SELECT 1
                FROM run_interrupt
                WHERE run_id = %s
                  AND applied_at IS NOT NULL
                  AND answer_payload ->> 'type' = 'SELECT_PROVINCES'
                LIMIT 1
                """,
                (run_id,),
            )
        ).fetchone()
        if handled is not None:
            return None
        rows = await (
            await conn.execute(
                """
                SELECT content
                FROM chat_message
                WHERE run_id = %s AND role = 'USER'
                ORDER BY message_seq
                """,
                (run_id,),
            )
        ).fetchall()
        expression = next(
            (
                detected
                for detected in (
                    detect_macro_region(str(row["content"])) for row in rows
                )
                if detected is not None
            ),
            None,
        )
        if expression is None:
            return None
        release = await (
            await conn.execute(
                "SELECT release_id FROM run WHERE run_id = %s",
                (run_id,),
            )
        ).fetchone()
        if release is None:
            raise RunLifecycleError("RUN_NOT_FOUND")
        try:
            catalog = await load_region_catalog(conn, release["release_id"])
        except MacroRegionError as exc:
            raise RunLifecycleError(exc.reason) from exc
        return expression, catalog

    async def _active_region_clarification(
        self,
        *,
        principal_id: str,
        session_id: UUID,
    ) -> dict[str, Any] | None:
        conn = await self._connect()
        try:
            await self._raise_if_principal_unavailable(conn, principal_id)
            row = await (
                await conn.execute(
                    """
                    SELECT message_seq,
                           metadata -> 'region_clarification'
                               AS region_clarification
                    FROM chat_message
                    WHERE session_id = %s
                      AND role = 'ASSISTANT'
                      AND metadata ? 'region_clarification'
                    ORDER BY message_seq DESC
                    LIMIT 1
                    """,
                    (session_id,),
                )
            ).fetchone()
            if row is None:
                return None
            clarification = row["region_clarification"]
            if not isinstance(clarification, dict):
                return None
            clarification_id = clarification.get("clarification_id")
            if not isinstance(clarification_id, str) or not clarification_id:
                return None
            resolved = await (
                await conn.execute(
                    """
                    SELECT 1
                    FROM chat_message
                    WHERE session_id = %s
                      AND role = 'USER'
                      AND message_seq > %s
                      AND metadata ->> 'resolves_region_clarification_id' = %s
                    LIMIT 1
                    """,
                    (session_id, row["message_seq"], clarification_id),
                )
            ).fetchone()
            if resolved is not None:
                return None
            consumed = await (
                await conn.execute(
                    """
                    SELECT 1
                    FROM run_interrupt interrupt
                    JOIN run ON run.run_id = interrupt.run_id
                    WHERE run.session_id = %s
                      AND interrupt.state = 'CONSUMED'
                      AND interrupt.applied_at IS NOT NULL
                      AND interrupt.answer_payload ->> 'clarification_id' = %s
                    LIMIT 1
                    """,
                    (session_id, clarification_id),
                )
            ).fetchone()
            if consumed is not None:
                return None
            return {
                "message_seq": row["message_seq"],
                "clarification": clarification,
            }
        finally:
            await conn.close()

    @staticmethod
    def _region_question(term: str) -> str:
        return (
            f"“{term}”的覆盖范围并不唯一。你说的具体范围包括哪些省份？"
            "请多选省份，或选择一个会明确展开省份的快捷组；"
            "确认后我才会把它作为地域筛选条件。"
        )

    @staticmethod
    def _cancels_region_restriction(text: str) -> bool:
        return re.search(
            r"(先不管这个|取消.{0,6}(?:地域|地区|省份).{0,4}限制|"
            r"不限(?:地域|地区|省份)|这个限制不要了)",
            text,
        ) is not None

    @staticmethod
    def _apply_resolved_region_to_profile(
        profile: StructuredProfileInput,
        *,
        operation: str,
        provinces: tuple[str, ...],
    ) -> StructuredProfileInput:
        selected = set(provinces)
        required = set(profile.required_provinces)
        excluded = set(profile.excluded_provinces)
        if operation == "exclude":
            excluded.update(selected)
            required.difference_update(selected)
        elif operation == "require":
            required.update(selected)
            excluded.difference_update(selected)
        elif operation == "withdraw":
            required.difference_update(selected)
            excluded.difference_update(selected)
        else:
            raise RunLifecycleError("REGION_ANSWER_INVALID")
        preferred_cities = tuple(
            city
            for city in profile.preferred_cities
            if _CITY_PROVINCE.get(city) not in excluded
        )
        required_cities = tuple(
            city
            for city in profile.required_cities
            if _CITY_PROVINCE.get(city) not in excluded
        )
        return replace(
            profile,
            required_provinces=tuple(
                value for value in _CONVERSATION_PROVINCES if value in required
            ),
            excluded_provinces=tuple(
                value for value in _CONVERSATION_PROVINCES if value in excluded
            ),
            preferred_cities=preferred_cities,
            required_cities=required_cities,
            excluded_cities=tuple(
                city
                for city in profile.excluded_cities
                if city not in required_cities
            ),
        )

    @staticmethod
    def _profile_from_fields(fields: dict[str, Any]) -> StructuredProfileInput:
        def value(key: str, default: Any) -> Any:
            field = fields.get(key) or {}
            if field.get("state") in {"UNKNOWN", "DECLINED"}:
                return default
            return field.get("value", default)

        def values(key: str) -> tuple[str, ...]:
            raw = value(key, ())
            return tuple(str(item) for item in (raw or ()))

        return StructuredProfileInput(
            province=str(value("province", "")),
            application_year=value("application_year", None),
            category=str(value("category", "")),
            batch=str(value("batch", "")),
            score=value("score", None),
            subjects=values("subjects"),
            rank=value("rank", None),
            risk_appetite=str(value("preferences.risk_appetite", "")),
            priority_dims=values("preferences.priority_dims"),
            interests=values("preferences.major_interests"),
            preferred_cities=values("preferences.preferred_cities"),
            preferred_tiers=values("preferences.preferred_tiers"),
            required_provinces=values("preferences.required_provinces"),
            excluded_provinces=values("preferences.excluded_provinces"),
            required_cities=values("preferences.required_cities"),
            excluded_cities=values("preferences.excluded_cities"),
            required_tiers=values("preferences.required_tiers"),
            required_major_keywords=values(
                "preferences.required_major_keywords"
            ),
            required_major_names=values("preferences.required_major_names"),
            excluded_major_keywords=values(
                "preferences.excluded_major_keywords"
            ),
            excluded_major_names=values(
                "preferences.excluded_major_names"
            ),
            excluded_universities=values(
                "preferences.excluded_universities"
            ),
        )

    @staticmethod
    def _safety_response(text: str) -> str | None:
        if not re.search(
            r"(自杀|轻生|不想活|结束生命|伤害自己|伤害他人|"
            r"杀人|有生命危险|活不下去)",
            text,
        ):
            return None
        return (
            "听起来你现在可能处在很危险的状态。请先确保自己安全，"
            "立即联系身边可信赖的人，并联系当地急救或心理援助。"
            "在中国大陆可拨打 120 或 110。你可以告诉我现在是否安全、"
            "身边是否有人；志愿填报问题可以稍后再处理。"
        )

    @staticmethod
    def _session_summary(row: dict[str, Any]) -> ChatSessionSummary:
        return ChatSessionSummary(
            session_id=row["session_id"],
            title=row["title"],
            latest_run_id=row["latest_run_id"],
            latest_state=row["latest_state"] or "EMPTY",
            latest_result_version=row["latest_result_version"],
            latest_result_kind=row["latest_result_kind"],
            run_count=int(row["run_count"] or 0),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _conversation_decision(text: str) -> str:
        action = re.search(
            r"(不要|排除|去掉|删除|只看|只想|想看|只学|只读|只要|仅限|只考虑|优先|喜欢|"
            r"想学|想读|想报|感兴趣|更稳|保守|冲刺|改成|换成|再加|增加|减少|筛选|过滤|"
            r"不想去|不想在|不想学|不想读|不想报|不去|不学|不报|"
            r"也可以|能接受|接受|可以考虑)",
            text,
        )
        if action is not None:
            return "REVISE_PLAN"
        asks_question = re.search(
            r"(为什么|怎么|如何|解释|依据|排名|概率|风险|限制|"
            r"是否|能不能|可以吗|吗[？?]?|呢[？?]?|[？?])",
            text,
        )
        refers_to_report = re.search(
            r"(当前|这份|这个方案|报告|排序|排名|概率|风险|保底|"
            r"冲档|稳档|推荐|学校|专业组|结果|排在|放在|"
            r"这所学校|这所大学)",
            text,
        )
        if asks_question is not None and refers_to_report is not None:
            return "EXPLAIN_RESULT"
        return "CLARIFY"

    @staticmethod
    def _requests_more_rush(text: str) -> bool:
        return _MORE_RUSH_PATTERN.search(text) is not None

    async def _parent_result_context(
        self,
        *,
        principal_id: str,
        session_id: UUID,
        parent_result_version: UUID,
    ) -> tuple[str, UUID, int, int, int] | None:
        conn = await self._connect()
        try:
            row = await (
                await conn.execute(
                    """
                    SELECT r.release_id, r.run_id, rr.payload
                    FROM run r
                    JOIN chat_session cs ON cs.id = r.session_id
                    JOIN run_result rr
                      ON rr.run_id = r.run_id
                     AND rr.result_version = r.result_version
                    WHERE r.result_version = %s
                      AND r.session_id = %s
                      AND cs.principal_id = %s
                      AND r.result_kind = 'V1_FORMAL'
                      AND r.state = 'SUCCEEDED'
                    """,
                    (parent_result_version, session_id, principal_id),
                )
            ).fetchone()
        finally:
            await conn.close()
        if row is None:
            return None
        rush_count, stable_count, safe_count = (
            self._conversation_tier_counts(row["payload"])
        )
        return (
            str(row["release_id"]),
            row["run_id"],
            rush_count,
            stable_count,
            safe_count,
        )

    @staticmethod
    def _conversation_tier_counts(payload: dict[str, Any]) -> tuple[int, int, int]:
        counts = {"冲": 0, "稳": 0, "保": 0}
        report = payload.get("report")
        sections = report.get("sections", ()) if isinstance(report, dict) else ()
        for section in sections:
            if not isinstance(section, dict):
                continue
            if not str(section.get("section_key") or "").startswith(
                "candidate:"
            ):
                continue
            match = re.search(
                r"策略分档[：:]\s*(冲|稳|保)",
                str(section.get("body") or ""),
            )
            if match is not None:
                counts[match.group(1)] += 1
        return counts["冲"], counts["稳"], counts["保"]

    @classmethod
    async def _formal_tier_counts(
        cls,
        conn,
        result_version: UUID,
    ) -> tuple[int, int, int] | None:
        row = await (
            await conn.execute(
                """
                SELECT payload
                FROM run_result
                WHERE result_version = %s AND result_kind = 'V1_FORMAL'
                """,
                (result_version,),
            )
        ).fetchone()
        if row is None:
            return None
        return cls._conversation_tier_counts(row["payload"])

    @staticmethod
    def _apply_conversation_patch(
        profile: StructuredProfileInput,
        patch: dict[str, Any],
    ) -> StructuredProfileInput:
        allowed = {
            "score",
            "risk_appetite",
            "preferred_cities",
            "required_cities",
            "excluded_cities",
            "required_provinces",
            "excluded_provinces",
            "required_tiers",
            "major_interests",
            "required_major_keywords",
            "required_major_names",
            "excluded_major_keywords",
            "excluded_major_names",
            "excluded_universities",
        }
        if set(patch) - allowed:
            raise RunLifecycleError("CONVERSATION_PATCH_INVALID")

        def string_list(
            key: str,
            current: tuple[str, ...],
            *,
            allowed_values: frozenset[str] | None = None,
        ) -> tuple[str, ...]:
            if key not in patch:
                return current
            value = patch[key]
            if (
                not isinstance(value, list)
                or len(value) > 20
                or not all(
                    isinstance(item, str)
                    and item.strip()
                    and len(item.strip()) <= 100
                    and re.search(r"[\x00-\x1f]", item) is None
                    for item in value
                )
            ):
                raise RunLifecycleError("CONVERSATION_PATCH_INVALID")
            normalized = tuple(dict.fromkeys(item.strip() for item in value))
            if allowed_values is not None and any(
                item not in allowed_values for item in normalized
            ):
                raise RunLifecycleError("CONVERSATION_PATCH_INVALID")
            return normalized

        score = profile.score
        if "score" in patch:
            value = patch["score"]
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
                or value > 750
            ):
                raise RunLifecycleError("CONVERSATION_PATCH_INVALID")
            score = value

        risk = profile.risk_appetite
        if "risk_appetite" in patch:
            value = patch["risk_appetite"]
            if value not in {"conservative", "balanced", "aggressive"}:
                raise RunLifecycleError("CONVERSATION_PATCH_INVALID")
            risk = value

        preferred_cities = string_list(
            "preferred_cities",
            profile.preferred_cities,
            allowed_values=frozenset(_CONVERSATION_CITIES),
        )
        required_cities = string_list(
            "required_cities",
            profile.required_cities,
            allowed_values=frozenset(_CONVERSATION_CITIES),
        )
        excluded_cities = string_list(
            "excluded_cities",
            profile.excluded_cities,
            allowed_values=frozenset(_CONVERSATION_CITIES),
        )
        required_provinces = string_list(
            "required_provinces",
            profile.required_provinces,
            allowed_values=frozenset(_CONVERSATION_PROVINCES),
        )
        excluded_provinces = string_list(
            "excluded_provinces",
            profile.excluded_provinces,
            allowed_values=frozenset(_CONVERSATION_PROVINCES),
        )
        required_tiers = string_list(
            "required_tiers",
            profile.required_tiers,
            allowed_values=frozenset({"985", "211", "双一流"}),
        )
        major_interests = string_list(
            "major_interests",
            profile.interests,
        )
        required_majors = string_list(
            "required_major_keywords",
            profile.required_major_keywords,
        )
        required_major_names = string_list(
            "required_major_names",
            profile.required_major_names,
        )
        excluded_majors = string_list(
            "excluded_major_keywords",
            profile.excluded_major_keywords,
        )
        excluded_major_names = string_list(
            "excluded_major_names",
            profile.excluded_major_names,
        )
        excluded_universities = string_list(
            "excluded_universities",
            profile.excluded_universities,
        )

        if set(required_cities) & set(excluded_cities):
            raise RunLifecycleError("CONVERSATION_PATCH_CONFLICT")
        if set(required_provinces) & set(excluded_provinces):
            raise RunLifecycleError("CONVERSATION_PATCH_CONFLICT")

        preferred_cities = tuple(
            city for city in preferred_cities if city not in excluded_cities
        )
        required_cities = tuple(
            city for city in required_cities if city not in excluded_cities
        )
        required_provinces = tuple(
            province
            for province in required_provinces
            if province not in excluded_provinces
        )
        preferred_cities = tuple(
            city
            for city in preferred_cities
            if _CITY_PROVINCE.get(city) not in excluded_provinces
        )
        required_cities = tuple(
            city
            for city in required_cities
            if _CITY_PROVINCE.get(city) not in excluded_provinces
        )
        return replace(
            profile,
            score=score,
            risk_appetite=risk,
            preferred_cities=preferred_cities,
            required_cities=required_cities,
            excluded_cities=excluded_cities,
            required_provinces=required_provinces,
            excluded_provinces=excluded_provinces,
            required_tiers=required_tiers,
            interests=major_interests,
            required_major_keywords=required_majors,
            required_major_names=required_major_names,
            excluded_major_keywords=excluded_majors,
            excluded_major_names=excluded_major_names,
            excluded_universities=excluded_universities,
        )

    @classmethod
    def _apply_conversation_constraints(
        cls,
        profile: StructuredProfileInput,
        text: str,
    ) -> StructuredProfileInput:
        cities = _CONVERSATION_CITIES
        majors = _CONVERSATION_MAJOR_KEYWORDS
        required_cities = list(profile.required_cities)
        excluded_cities = list(profile.excluded_cities)
        required_provinces = list(profile.required_provinces)
        excluded_provinces = list(profile.excluded_provinces)
        preferred_cities = list(profile.preferred_cities)
        interests = list(profile.interests)
        required_majors = list(profile.required_major_keywords)
        required_major_names = list(profile.required_major_names)
        excluded_majors = list(profile.excluded_major_keywords)
        excluded_major_names = list(profile.excluded_major_names)
        excluded_universities = list(profile.excluded_universities)
        preferred_tiers = list(profile.preferred_tiers)
        required_tiers = list(profile.required_tiers)
        risk = profile.risk_appetite

        for province in _CONVERSATION_PROVINCES:
            if province in _CONVERSATION_CITIES and not re.search(
                rf"{re.escape(province)}(?:省|市|地区)",
                text,
            ):
                continue
            if not re.search(
                rf"(不要|排除|去掉|不去|不考虑|不想去|不想在)"
                rf"\s*{re.escape(province)}(?:省|市|自治区|地区)?",
                text,
            ):
                if not re.search(
                    rf"{re.escape(province)}(?:省|市|自治区|地区)?"
                    r".{0,8}(太远|不想去|不去|不考虑|排除)",
                    text,
                ):
                    continue
            excluded_provinces = cls._append_unique(
                excluded_provinces,
                province,
            )
            required_provinces = [
                value for value in required_provinces if value != province
            ]
            preferred_cities = [
                city
                for city in preferred_cities
                if _CITY_PROVINCE.get(city) != province
            ]
            required_cities = [
                city
                for city in required_cities
                if _CITY_PROVINCE.get(city) != province
            ]
        for province in _CONVERSATION_PROVINCES:
            if re.search(
                rf"{re.escape(province)}(?:省|自治区|地区)?"
                r".{0,8}(也可以|能接受|可以考虑)",
                text,
            ):
                excluded_provinces = [
                    value for value in excluded_provinces if value != province
                ]
        for province in _CONVERSATION_PROVINCES:
            if not re.search(
                rf"(只看|只要|仅限|只考虑|必须(?:在|去))"
                rf"\s*{re.escape(province)}(?:省|自治区|地区)?",
                text,
            ):
                continue
            required_provinces = cls._append_unique(
                required_provinces,
                province,
            )
            excluded_provinces = [
                value for value in excluded_provinces if value != province
            ]

        for city in cities:
            if not re.search(
                rf"(不要|排除|去掉|不去|不考虑|不想去|不想在)"
                rf"\s*{re.escape(city)}",
                text,
            ):
                if not re.search(
                    rf"{re.escape(city)}.{{0,8}}"
                    r"(太远|不想去|不去|不考虑|排除)",
                    text,
                ):
                    continue
            excluded_cities = cls._append_unique(excluded_cities, city)
            preferred_cities = [value for value in preferred_cities if value != city]
            required_cities = [value for value in required_cities if value != city]
        for city in cities:
            if re.search(
                rf"{re.escape(city)}.{{0,8}}(也可以|能接受|可以考虑)",
                text,
            ):
                excluded_cities = [
                    value for value in excluded_cities if value != city
                ]
                preferred_cities = cls._append_unique(
                    preferred_cities,
                    city,
                )
        for city in cities:
            if not re.search(
                rf"(只看|只要|仅限|只考虑|必须(?:在|去))\s*{re.escape(city)}",
                text,
            ):
                continue
            required_cities = cls._append_unique(required_cities, city)
            preferred_cities = cls._append_unique(preferred_cities, city)
            excluded_cities = [value for value in excluded_cities if value != city]
        for city in cities:
            if city in text and not any(
                city in values
                for values in (
                    required_cities,
                    excluded_cities,
                )
            ):
                if re.search(r"(优先|想去|喜欢|倾向)", text):
                    preferred_cities = cls._append_unique(
                        preferred_cities,
                        city,
                    )

        exact_majors = _CONVERSATION_EXACT_MAJOR_NAMES
        matched_exact_majors = []
        for major in exact_majors:
            if re.search(
                rf"(不要|排除|去掉|不考虑|不想学|不想读|不想报|"
                rf"不学|不报)\s*(?:任何)?{re.escape(major)}(?:专业)?",
                text,
            ):
                excluded_major_names = cls._append_unique(
                    excluded_major_names,
                    major,
                )
                matched_exact_majors.append(major)
                interests = [value for value in interests if value != major]
                required_majors = [
                    value for value in required_majors if value != major
                ]
            elif re.search(
                rf"(?:只看|只想|想看|只学|只读|只要|仅限|只考虑)\s*"
                rf"(?:本科专业)?{re.escape(major)}(?:专业)?",
                text,
            ):
                required_major_names = cls._append_unique(
                    required_major_names,
                    major,
                )
                interests = cls._append_unique(interests, major)
                matched_exact_majors.append(major)
            elif re.search(
                rf"(?:想学|想读|想报|喜欢|感兴趣|兴趣|优先)\s*"
                rf"(?:本科专业)?{re.escape(major)}(?:专业)?",
                text,
            ):
                interests = cls._append_unique(interests, major)
                matched_exact_majors.append(major)

        if "物理学" in interests and re.search(
            r"(?:也|可以)?接受\s*(?:物理)?相关专业",
            text,
        ):
            for major in _PHYSICS_RELATED_MAJOR_INTERESTS:
                interests = cls._append_unique(interests, major)

        for major in majors:
            if any(major in exact for exact in matched_exact_majors):
                continue
            if re.search(
                rf"(不要|排除|去掉|不考虑|不想学|不想读|不想报|"
                rf"不学|不报)\s*{re.escape(major)}",
                text,
            ):
                excluded_majors = cls._append_unique(excluded_majors, major)
                interests = [value for value in interests if value != major]
                required_majors = [
                    value for value in required_majors if value != major
                ]
            elif major in text and re.search(
                r"(只看|只要|仅限|只考虑|只想)",
                text,
            ):
                required_majors = cls._append_unique(required_majors, major)
                interests = cls._append_unique(interests, major)
                excluded_majors = [
                    value for value in excluded_majors if value != major
                ]
            elif major in text and re.search(r"(喜欢|想学|优先|兴趣)", text):
                interests = cls._append_unique(interests, major)

        for keyword in _CONVERSATION_MAJOR_EXCLUSION_KEYWORDS:
            if re.search(
                rf"(不要|排除|去掉|不考虑|不想学|不想读|不想报|"
                rf"不学|不报)\s*(?:任何)?{re.escape(keyword)}(?:类|专业)?",
                text,
            ):
                excluded_majors = cls._append_unique(
                    excluded_majors,
                    keyword,
                )
                interests = [
                    value
                    for value in interests
                    if keyword not in value
                ]
                required_majors = [
                    value
                    for value in required_majors
                    if keyword not in value
                ]

        if "物理" in interests:
            interests = [
                "物理学" if value == "物理" else value
                for value in interests
            ]
        if "物理" in required_majors:
            required_majors = [
                value for value in required_majors if value != "物理"
            ]
            required_major_names = cls._append_unique(
                required_major_names,
                "物理学",
            )
            interests = cls._append_unique(interests, "物理学")

        for tier in ("985", "211", "双一流"):
            if tier not in text:
                continue
            if re.search(rf"(只看|只要|仅限|只考虑)\s*{tier}", text):
                required_tiers = cls._append_unique(required_tiers, tier)
            if tier not in required_tiers:
                preferred_tiers = cls._append_unique(preferred_tiers, tier)

        for match in re.finditer(
            r"(?:不要|排除|去掉|不考虑)"
            r"([\u4e00-\u9fff]{2,30}(?:大学|学院))",
            text,
        ):
            excluded_universities = cls._append_unique(
                excluded_universities,
                match.group(1),
            )

        if re.search(r"(更稳|稳妥|保守|保底)", text):
            risk = "conservative"
        elif re.search(r"(冲刺|激进|冲一冲)", text):
            risk = "aggressive"
        elif re.search(r"(均衡|平衡)", text):
            risk = "balanced"

        score_match = re.search(
            r"(?:改成|变为|换成|分数是)\s*(\d{3})\s*分",
            text,
        )
        score = profile.score
        if score_match is not None:
            candidate_score = int(score_match.group(1))
            if 0 <= candidate_score <= 750:
                score = candidate_score

        return replace(
            profile,
            score=score,
            risk_appetite=risk,
            preferred_cities=tuple(preferred_cities),
            required_cities=tuple(required_cities),
            excluded_cities=tuple(excluded_cities),
            required_provinces=tuple(required_provinces),
            excluded_provinces=tuple(excluded_provinces),
            interests=tuple(interests),
            preferred_tiers=tuple(preferred_tiers),
            required_tiers=tuple(required_tiers),
            required_major_keywords=tuple(required_majors),
            required_major_names=tuple(required_major_names),
            excluded_major_keywords=tuple(excluded_majors),
            excluded_major_names=tuple(excluded_major_names),
            excluded_universities=tuple(excluded_universities),
        )

    @staticmethod
    def _append_unique(values: list[str], value: str) -> list[str]:
        if value not in values:
            return [*values, value]
        return values

    @staticmethod
    async def _raise_if_principal_unavailable(
        conn: psycopg.AsyncConnection,
        principal_id: str,
    ) -> None:
        restore = await (
            await conn.execute(
                """
                SELECT status
                FROM data_lifecycle_restore_state
                WHERE restore_key = 'SERVICE'
                """
            )
        ).fetchone()
        if restore is not None and restore["status"] != "OPEN":
            raise RunLifecycleError("RESTORE_IN_PROGRESS")
        row = await (
            await conn.execute(
                """
                SELECT status
                FROM principal_data_state
                WHERE principal_id = %s
                """,
                (principal_id,),
            )
        ).fetchone()
        if row is None:
            return
        if row["status"] == "DELETION_PENDING":
            raise RunLifecycleError("DELETION_IN_PROGRESS")
        if row["status"] == "DELETED":
            raise RunLifecycleError("PRINCIPAL_DELETED")

    async def GetRetrievalRunBinding(
        self,
        *,
        principal_id: str,
        run_id: UUID,
        mode: str = "hybrid",
        top_k: int = 5,
        pool_k: int = 20,
    ):
        """从持久 run 的权威记录构造在线检索 binding。

        scope、principal、release、学校与文档类型只读取持久 run/任务记录；
        调用方只能提供检索通道和 top/pool。
        """
        from graph.nodes.retrieve import (
            ConfirmedProfileScope,
            RetrievalRunBinding,
        )
        from rag.policy_retrieval import ApplicabilityScope

        conn = await self._connect()
        try:
            await self._raise_if_principal_unavailable(conn, principal_id)
            row = await (
                await conn.execute(
                    """
                    SELECT run_id, principal_id, task_kind, task_input,
                           release_id, confirmed_profile_version
                    FROM run
                    WHERE run_id = %s AND principal_id = %s
                    """,
                    (run_id, principal_id),
                )
            ).fetchone()
            if row is None:
                raise RunLifecycleError("RUN_NOT_FOUND")

            confirmed_profile = None
            task_scope = None
            task_input = dict(row["task_input"])
            school_ids = self._trusted_string_tuple(
                task_input.get("school_ids", ()),
                "school_ids",
            )
            document_types = self._trusted_string_tuple(
                task_input.get("document_types", ()),
                "document_types",
            )
            if row["task_kind"] == "POLICY_QA":
                required = (
                    task_input.get("province"),
                    task_input.get("application_year"),
                    task_input.get("category"),
                    task_input.get("batch"),
                )
                if any(value in (None, "") for value in required):
                    raise RunLifecycleError("RETRIEVAL_SCOPE_REQUIRED")
                task_scope = ApplicabilityScope(
                    province=str(required[0]),
                    application_year=int(required[1]),
                    category=str(required[2]),
                    batch=str(required[3]),
                )
            else:
                profile_version = row["confirmed_profile_version"]
                if profile_version is None:
                    raise RunLifecycleError("PROFILE_NOT_CONFIRMED")
                profile = await (
                    await conn.execute(
                        """
                        SELECT fields
                        FROM profile_revision
                        WHERE profile_version = %s
                          AND lifecycle = 'CONFIRMED'
                        """,
                        (profile_version,),
                    )
                ).fetchone()
                if profile is None:
                    raise RunLifecycleError("PROFILE_NOT_CONFIRMED")
                fields = profile["fields"]
                required = (
                    fields.get("province", {}).get("value"),
                    fields.get("application_year", {}).get("value"),
                    fields.get("category", {}).get("value"),
                    fields.get("batch", {}).get("value"),
                )
                if any(value in (None, "") for value in required):
                    raise RunLifecycleError("RETRIEVAL_SCOPE_REQUIRED")
                confirmed_profile = ConfirmedProfileScope(
                    profile_version=str(profile_version),
                    province=str(required[0]),
                    application_year=int(required[1]),
                    category=str(required[2]),
                    batch=str(required[3]),
                )
            return RetrievalRunBinding(
                principal_id=row["principal_id"],
                run_id=str(row["run_id"]),
                release_id=row["release_id"],
                task_kind=row["task_kind"],
                confirmed_profile=confirmed_profile,
                task_scope=task_scope,
                school_ids=school_ids,
                document_types=document_types,
                mode=mode,
                top_k=top_k,
                pool_k=pool_k,
            )
        finally:
            await conn.close()

    async def PublishFormalResult(
        self,
        command: PublishFormalResultCommand,
    ) -> FormalPublication:
        """消费 VerifyPackage 结论并原子发布不可变 V1_FORMAL 结果。"""
        try:
            result_version = UUID(command.package.result_version)
            profile_version = UUID(command.package.profile_version)
        except (TypeError, ValueError) as exc:
            raise RunLifecycleError(
                "VALIDATION_FAILED",
                "正式结果版本或 Profile 版本非法",
            ) from exc

        source_package_sha256 = payload_sha256(
            canonical_source_package(command.package)
        )
        conn = await self._connect()
        try:
            existing = await self._find_formal_publication(
                conn,
                principal_id=command.principal_id,
                run_id=command.run_id,
                result_version=result_version,
            )
        finally:
            await conn.close()
        if existing is not None:
            return self._replayed_formal_publication(
                existing,
                source_package_sha256=source_package_sha256,
            )

        artifact = await self._load_formal_gate_artifact(
            principal_id=command.principal_id,
            run_id=command.run_id,
        )
        self._require_formal_gate_artifact_match(
            package=command.package,
            artifact=artifact,
        )
        report = VerifyPackage(
            command.package,
            entailment_verifier=self._entailment_verifier,
            structured_authority=self._structured_evidence_authority,
            derived_recomputer=self._derived_evidence_recomputer,
        )
        if self._formal_publish_guard is not None:
            await self._formal_publish_guard.before_publish(
                command.run_id
            )
        if report.status == "BLOCKED":
            return await self._fail_blocked_formal_package(
                command,
                report=report,
                result_version=result_version,
            )

        published_package = canonical_source_package(
            retained_package(command.package, report)
        )
        package_sha256 = payload_sha256(published_package)
        report_payload = verification_report_payload(report)
        report_sha256 = payload_sha256(report)
        published_report = filter_stripped_report_claims(
            command.report or {},
            report.stripped_claim_ids,
        )
        notice_texts = {
            notice.notice_type: notice.normalized_text
            for notice in command.package.gate_context.required_notices
        }
        result_summary = {
            "result_version": str(result_version),
            "result_kind": "V1_FORMAL",
            "release_id": command.package.release_id,
            "confirmed_profile_version": str(profile_version),
            "evidence_package_sha256": package_sha256,
            "verification_report_sha256": report_sha256,
            "verification_report": report_payload,
            "report": published_report,
            "risk_notice": notice_texts["RISK_NOTICE"],
            "disclaimer": notice_texts["DISCLAIMER"],
            "no_admission_guarantee": notice_texts[
                "NO_ADMISSION_GUARANTEE"
            ],
            "result_state": "ACTIVE",
            "stale_reason": None,
        }

        conn = await self._connect()
        try:
            async with conn.transaction():
                run = await (
                    await conn.execute(
                        """
                        SELECT run_id, session_id, state, stage, state_version,
                               release_id, confirmed_profile_version,
                               lease_owner, lease_epoch, cancel_requested_at,
                               active_execution_remaining_ms,
                               active_execution_started_at
                        FROM run
                        WHERE run_id = %s AND principal_id = %s
                        FOR UPDATE
                        """,
                        (command.run_id, command.principal_id),
                    )
                ).fetchone()
                if run is None:
                    raise RunLifecycleError("RUN_NOT_FOUND")
                remaining = run["active_execution_remaining_ms"]
                if run["active_execution_started_at"] is not None:
                    remaining = max(
                        0,
                        remaining
                        - int(
                            (
                                self._clock()
                                - run["active_execution_started_at"]
                            ).total_seconds()
                            * 1000
                        ),
                    )
                if remaining <= 0:
                    raise RunLifecycleError(
                        "EXECUTION_TIMEOUT",
                        "主动执行额度在正式发布前耗尽",
                    )

                concurrent = await self._find_formal_publication(
                    conn,
                    principal_id=command.principal_id,
                    run_id=command.run_id,
                    result_version=result_version,
                )
                if concurrent is not None:
                    return self._replayed_formal_publication(
                        concurrent,
                        source_package_sha256=source_package_sha256,
                    )

                if (
                    run["state"] == "RUNNING"
                    and run["lease_owner"] == self._worker_owner
                    and run["lease_epoch"] == command.expected_lease_epoch
                    and run["cancel_requested_at"] is not None
                ):
                    cancelled_version = run["state_version"] + 1
                    cancelled = await (
                        await conn.execute(
                            """
                            UPDATE run
                            SET state = 'CANCELLED',
                                terminal_reason = 'CANCELLED_BY_USER',
                                state_version = %s,
                                lease_owner = NULL,
                                lease_expires_at = NULL,
                                active_execution_started_at = NULL,
                                updated_at = NOW()
                            WHERE run_id = %s
                              AND state = 'RUNNING'
                              AND state_version = %s
                              AND lease_owner = %s
                              AND lease_epoch = %s
                              AND cancel_requested_at IS NOT NULL
                            RETURNING run_id
                            """,
                            (
                                cancelled_version,
                                command.run_id,
                                run["state_version"],
                                self._worker_owner,
                                command.expected_lease_epoch,
                            ),
                        )
                    ).fetchone()
                    if cancelled is None:
                        raise RunLifecycleError("RUN_STATE_CONFLICT")
                    await append_event(
                        conn,
                        run_id=command.run_id,
                        event_type="STATE_CHANGED",
                        stage=run["stage"],
                        state_version=cancelled_version,
                        payload={
                            "from_state": "RUNNING",
                            "to_state": "CANCELLED",
                            "terminal_reason": "CANCELLED_BY_USER",
                        },
                    )
                    return FormalPublication(
                        accepted=False,
                        reason="CANCELLED",
                        run_id=command.run_id,
                        result_version=result_version,
                        result_kind=None,
                        verification_status=report.status,
                        evidence_package_sha256="",
                        verification_report_sha256=report_sha256,
                        stripped_claim_ids=(),
                    )

                try:
                    await self._require_formal_publish_binding(
                        conn,
                        command=command,
                        run=run,
                        profile_version=profile_version,
                    )
                except RunLifecycleError as exc:
                    if exc.reason != "RELEASE_REVOKED":
                        raise
                    failed_version = run["state_version"] + 1
                    failed = await (
                        await conn.execute(
                            """
                            UPDATE run
                            SET state = 'FAILED',
                                stage = 'PUBLISH_BLOCKED',
                                terminal_reason = 'RELEASE_REVOKED',
                                state_version = %s,
                                lease_owner = NULL,
                                lease_expires_at = NULL,
                                active_execution_started_at = NULL,
                                updated_at = NOW()
                            WHERE run_id = %s
                              AND state = 'RUNNING'
                              AND state_version = %s
                              AND lease_owner = %s
                              AND lease_epoch = %s
                            RETURNING run_id
                            """,
                            (
                                failed_version,
                                command.run_id,
                                run["state_version"],
                                self._worker_owner,
                                command.expected_lease_epoch,
                            ),
                        )
                    ).fetchone()
                    if failed is None:
                        raise RunLifecycleError("RUN_STATE_CONFLICT")
                    await append_event(
                        conn,
                        run_id=command.run_id,
                        event_type="ERROR",
                        stage="PUBLISH_BLOCKED",
                        state_version=failed_version,
                        payload={
                            "code": "RELEASE_REVOKED",
                            "message": "绑定 release 已撤销，正式结果不得发布",
                            "retryable": False,
                        },
                    )
                    return FormalPublication(
                        accepted=False,
                        reason="RELEASE_REVOKED",
                        run_id=command.run_id,
                        result_version=result_version,
                        result_kind=None,
                        verification_status=report.status,
                        evidence_package_sha256="",
                        verification_report_sha256=report_sha256,
                        stripped_claim_ids=(),
                    )
                next_version = run["state_version"] + 1
                inserted_result = await (
                    await conn.execute(
                        """
                        INSERT INTO run_result (
                            result_version, run_id, release_id,
                            confirmed_profile_version, result_kind, payload,
                            verification_summary, unverified_conditions,
                            source_package_sha256, evidence_package_sha256,
                            verification_report_sha256
                        )
                        VALUES (
                            %s, %s, %s, %s, 'V1_FORMAL',
                            %s, %s, %s, %s, %s, %s
                        )
                        ON CONFLICT DO NOTHING
                        RETURNING result_version
                        """,
                        (
                            result_version,
                            command.run_id,
                            command.package.release_id,
                            profile_version,
                            Jsonb(result_summary),
                            Jsonb(report_payload),
                            Jsonb(list(report.blocking_reasons)),
                            source_package_sha256,
                            package_sha256,
                            report_sha256,
                        ),
                    )
                ).fetchone()
                if inserted_result is None:
                    raise RunLifecycleError("RESULT_VERSION_CONFLICT")
                await conn.execute(
                    """
                    INSERT INTO run_evidence_package (
                        result_version, run_id, release_id,
                        confirmed_profile_version, result_kind,
                        source_package_sha256, package_sha256, package_json
                    )
                    VALUES (%s, %s, %s, %s, 'V1_FORMAL', %s, %s, %s)
                    """,
                    (
                        result_version,
                        command.run_id,
                        command.package.release_id,
                        profile_version,
                        source_package_sha256,
                        package_sha256,
                        Jsonb(canonical_payload(published_package)),
                    ),
                )
                await conn.execute(
                    """
                    INSERT INTO run_verification_report (
                        result_version, run_id, status, report_sha256,
                        report_json, stripped_claim_ids
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        result_version,
                        command.run_id,
                        report.status,
                        report_sha256,
                        Jsonb(report_payload),
                        Jsonb(list(report.stripped_claim_ids)),
                    ),
                )
                await conn.execute(
                    """
                    INSERT INTO run_result_state_event (
                        result_version, status, reason
                    )
                    VALUES (%s, 'ACTIVE', NULL)
                    """,
                    (result_version,),
                )
                updated = await (
                    await conn.execute(
                        """
                        UPDATE run
                        SET state = 'SUCCEEDED',
                            stage = 'FORMAL_READY',
                            terminal_reason = NULL,
                            state_version = %s,
                            result_version = %s,
                            result_kind = 'V1_FORMAL',
                            lease_owner = NULL,
                            lease_expires_at = NULL,
                            active_execution_started_at = NULL,
                            updated_at = NOW()
                        WHERE run_id = %s
                          AND state = 'RUNNING'
                          AND state_version = %s
                          AND lease_owner = %s
                          AND lease_epoch = %s
                          AND release_id = %s
                          AND confirmed_profile_version = %s
                          AND cancel_requested_at IS NULL
                        RETURNING run_id
                        """,
                        (
                            next_version,
                            result_version,
                            command.run_id,
                            command.expected_state_version,
                            self._worker_owner,
                            command.expected_lease_epoch,
                            command.package.release_id,
                            profile_version,
                        ),
                    )
                ).fetchone()
                if updated is None:
                    raise RunLifecycleError("RUN_STATE_CONFLICT")
                await append_event(
                    conn,
                    run_id=command.run_id,
                    event_type="STATE_CHANGED",
                    stage="FORMAL_READY",
                    state_version=next_version,
                    payload={
                        "from_state": "RUNNING",
                        "to_state": "SUCCEEDED",
                    },
                )
                await append_event(
                    conn,
                    run_id=command.run_id,
                    event_type="RESULT",
                    stage="FORMAL_READY",
                    state_version=next_version,
                    payload={
                        "result_version": str(result_version),
                        "result_kind": "V1_FORMAL",
                        "formal_result": result_summary,
                    },
                    payload_version=3,
                )
                await self._store_messages(
                    conn,
                    session_id=run["session_id"],
                    run_id=command.run_id,
                    messages=(
                        ChatMessageInput(
                            client_message_id=(
                                f"server:{command.run_id}:formal-ready"
                            ),
                            role="ASSISTANT",
                            kind="RESULT",
                            content=(
                                "正式推荐方案已生成。你可以继续询问报告依据，"
                                "或按城市、专业、学校层次和风险偏好调整筛选条件。"
                            ),
                        ),
                    ),
                )
                await conn.execute(
                    "UPDATE chat_session SET updated_at = NOW() WHERE id = %s",
                    (run["session_id"],),
                )
        finally:
            await conn.close()

        return FormalPublication(
            accepted=True,
            reason=None,
            run_id=command.run_id,
            result_version=result_version,
            result_kind="V1_FORMAL",
            verification_status=report.status,
            evidence_package_sha256=package_sha256,
            verification_report_sha256=report_sha256,
            stripped_claim_ids=report.stripped_claim_ids,
        )

    async def _load_formal_gate_artifact(
        self,
        *,
        principal_id: str,
        run_id: UUID,
    ) -> dict[str, Any]:
        conn = await self._connect()
        try:
            row = await (
                await conn.execute(
                    """
                    SELECT artifact.artifact_sha256, artifact.artifact_json
                    FROM run_formal_gate_artifact artifact
                    JOIN run ON run.run_id = artifact.run_id
                    WHERE artifact.run_id = %s
                      AND run.principal_id = %s
                    """,
                    (run_id, principal_id),
                )
            ).fetchone()
        finally:
            await conn.close()
        if row is None:
            raise RunLifecycleError("FORMAL_GATE_ARTIFACT_MISSING")
        artifact = row["artifact_json"]
        if row["artifact_sha256"] != formal_gate_artifact_sha256(artifact):
            raise RunLifecycleError("FORMAL_GATE_ARTIFACT_MISMATCH")
        return artifact

    @staticmethod
    def _require_formal_gate_artifact_match(
        *,
        package: EvidencePackage,
        artifact: dict[str, Any],
    ) -> None:
        context = package.gate_context
        if context is None:
            raise RunLifecycleError("FORMAL_GATE_CONTEXT_MISSING")
        if artifact["release_id"] != package.release_id:
            raise RunLifecycleError("RELEASE_MISMATCH")
        if artifact["profile_version"] != package.profile_version:
            raise RunLifecycleError("PROFILE_VERSION_MISMATCH")
        if artifact["verifier_version"] != context.verifier_version:
            raise RunLifecycleError("VERIFIER_VERSION_MISMATCH")
        if artifact["applicable_as_of"] != context.applicable_as_of.isoformat():
            raise RunLifecycleError("APPLICABLE_DATE_MISMATCH")
        if (
            artifact["algorithm_config_version"]
            != context.algorithm_config_version
            or artifact["algorithm_config_sha256"]
            != context.algorithm_config_sha256
        ):
            raise RunLifecycleError("ALGORITHM_CONFIG_MISMATCH")
        if (
            artifact["qualification"]
            != canonical_payload(context.qualification)
        ):
            raise RunLifecycleError("QUALIFICATION_ARTIFACT_MISMATCH")

    async def _require_formal_publish_binding(
        self,
        conn,
        *,
        command: PublishFormalResultCommand,
        run: dict[str, Any],
        profile_version: UUID,
    ) -> None:
        if (
            run["state"] != "RUNNING"
            or run["state_version"] != command.expected_state_version
            or run["lease_owner"] != self._worker_owner
            or run["lease_epoch"] != command.expected_lease_epoch
            or run["cancel_requested_at"] is not None
        ):
            raise RunLifecycleError("RUN_STATE_CONFLICT")
        if run["release_id"] != command.package.release_id:
            raise RunLifecycleError("RELEASE_MISMATCH")
        if run["confirmed_profile_version"] != profile_version:
            raise RunLifecycleError("PROFILE_VERSION_MISMATCH")
        profile = await (
            await conn.execute(
                """
                SELECT revision.lifecycle, confirmation.confirmation_id
                FROM profile_revision revision
                JOIN profile_confirmation confirmation
                  ON confirmation.profile_version = revision.profile_version
                WHERE revision.profile_version = %s
                """,
                (profile_version,),
            )
        ).fetchone()
        if profile is None or profile["lifecycle"] != "CONFIRMED":
            raise RunLifecycleError("PROFILE_NOT_CONFIRMED")
        release = await (
            await conn.execute(
                """
                SELECT status, revoked_at
                FROM release
                WHERE release_id = %s
                FOR SHARE
                """,
                (command.package.release_id,),
            )
        ).fetchone()
        if (
            release is None
            or release["status"] != "PUBLISHED"
            or release["revoked_at"] is not None
        ):
            raise RunLifecycleError("RELEASE_REVOKED")

    async def _find_formal_publication(
        self,
        conn,
        *,
        principal_id: str,
        run_id: UUID,
        result_version: UUID,
    ) -> dict[str, Any] | None:
        return await (
            await conn.execute(
                """
                SELECT result.result_version, result.run_id, result.release_id,
                       result.confirmed_profile_version, result.result_kind,
                       result.source_package_sha256,
                       package.package_sha256,
                       report.report_sha256, report.status,
                       report.stripped_claim_ids,
                       run.state
                FROM run_result result
                JOIN run ON run.run_id = result.run_id
                JOIN run_evidence_package package
                  ON package.result_version = result.result_version
                JOIN run_verification_report report
                  ON report.result_version = result.result_version
                WHERE result.result_version = %s
                  AND result.run_id = %s
                  AND run.principal_id = %s
                """,
                (result_version, run_id, principal_id),
            )
        ).fetchone()

    def _replayed_formal_publication(
        self,
        row: dict[str, Any],
        *,
        source_package_sha256: str,
    ) -> FormalPublication:
        if (
            row["state"] != "SUCCEEDED"
            or row["source_package_sha256"] != source_package_sha256
        ):
            raise RunLifecycleError("RESULT_VERSION_CONFLICT")
        return FormalPublication(
            accepted=True,
            reason=None,
            run_id=row["run_id"],
            result_version=row["result_version"],
            result_kind=row["result_kind"],
            verification_status=row["status"],
            evidence_package_sha256=row["package_sha256"],
            verification_report_sha256=row["report_sha256"],
            stripped_claim_ids=tuple(row["stripped_claim_ids"]),
            replayed=True,
        )

    async def _fail_blocked_formal_package(
        self,
        command: PublishFormalResultCommand,
        *,
        report: VerificationReport,
        result_version: UUID,
    ) -> FormalPublication:
        report_payload = verification_report_payload(report)
        conn = await self._connect()
        try:
            async with conn.transaction():
                run = await (
                    await conn.execute(
                        """
                        SELECT run_id, state, stage, state_version, release_id,
                               confirmed_profile_version, lease_owner,
                               lease_epoch, cancel_requested_at
                        FROM run
                        WHERE run_id = %s AND principal_id = %s
                        FOR UPDATE
                        """,
                        (command.run_id, command.principal_id),
                    )
                ).fetchone()
                if run is None:
                    raise RunLifecycleError("RUN_NOT_FOUND")
                if (
                    run["state"] != "RUNNING"
                    or run["state_version"] != command.expected_state_version
                    or run["lease_owner"] != self._worker_owner
                    or run["lease_epoch"] != command.expected_lease_epoch
                    or run["release_id"] != command.package.release_id
                    or (
                        run["confirmed_profile_version"] is not None
                        and str(run["confirmed_profile_version"])
                        != str(command.package.profile_version)
                    )
                ):
                    raise RunLifecycleError("RUN_STATE_CONFLICT")
                if run["cancel_requested_at"] is not None:
                    raise RunLifecycleError("RUN_STATE_CONFLICT")
                next_version = run["state_version"] + 1
                failed = await (
                    await conn.execute(
                        """
                        UPDATE run
                        SET state = 'FAILED',
                            stage = 'VERIFICATION_BLOCKED',
                            terminal_reason = 'VALIDATION_FAILED',
                            state_version = %s,
                            lease_owner = NULL,
                            lease_expires_at = NULL,
                            active_execution_started_at = NULL,
                            updated_at = NOW()
                        WHERE run_id = %s
                          AND state = 'RUNNING'
                          AND state_version = %s
                          AND lease_owner = %s
                          AND lease_epoch = %s
                          AND cancel_requested_at IS NULL
                        RETURNING run_id
                        """,
                        (
                            next_version,
                            command.run_id,
                            command.expected_state_version,
                            self._worker_owner,
                            command.expected_lease_epoch,
                        ),
                    )
                ).fetchone()
                if failed is None:
                    raise RunLifecycleError("RUN_STATE_CONFLICT")
                await append_event(
                    conn,
                    run_id=command.run_id,
                    event_type="ERROR",
                    stage="VERIFICATION_BLOCKED",
                    state_version=next_version,
                    payload={
                        "code": "VALIDATION_FAILED",
                        "message": "正式证据包未通过发布门禁",
                        "retryable": False,
                        "verification_report": report_payload,
                    },
                    payload_version=2,
                )
        finally:
            await conn.close()
        return FormalPublication(
            accepted=False,
            reason="VALIDATION_FAILED",
            run_id=command.run_id,
            result_version=result_version,
            result_kind=None,
            verification_status="BLOCKED",
            evidence_package_sha256="",
            verification_report_sha256=payload_sha256(report),
            stripped_claim_ids=(),
        )

    @staticmethod
    def _trusted_string_tuple(value, field: str) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)) or not all(
            isinstance(item, str) and item
            for item in value
        ):
            raise RunLifecycleError(
                "RETRIEVAL_SCOPE_INVALID",
                f"{field} must be a list of non-empty strings",
            )
        return tuple(value)

    async def _expire_due_hitl(self, *, principal_id: str, run_id: UUID) -> bool:
        conn = await self._connect()
        try:
            async with conn.transaction():
                run = await (
                    await conn.execute(
                        """
                        SELECT run_id, state, stage, state_version,
                               active_execution_remaining_ms
                        FROM run
                        WHERE run_id = %s AND principal_id = %s
                        FOR UPDATE
                        """,
                        (run_id, principal_id),
                    )
                ).fetchone()
                if run is None or run["state"] != "WAITING_HITL":
                    return False
                interrupt = await (
                    await conn.execute(
                        """
                        SELECT interrupt_id, expires_at
                        FROM run_interrupt
                        WHERE run_id = %s AND state = 'OPEN'
                        ORDER BY created_at DESC
                        LIMIT 1
                        FOR UPDATE
                        """,
                        (run_id,),
                    )
                ).fetchone()
                if interrupt is None or interrupt["expires_at"] > self._clock():
                    return False
                next_version = run["state_version"] + 1
                await conn.execute(
                    """
                    UPDATE run_interrupt
                    SET state = 'EXPIRED'
                    WHERE interrupt_id = %s AND state = 'OPEN'
                    """,
                    (interrupt["interrupt_id"],),
                )
                expired = await (
                    await conn.execute(
                        """
                        UPDATE run
                        SET state = 'EXPIRED',
                            terminal_reason = 'HITL_TIMEOUT',
                            state_version = %s,
                            updated_at = NOW()
                        WHERE run_id = %s
                          AND state = 'WAITING_HITL'
                          AND state_version = %s
                        RETURNING run_id
                        """,
                        (next_version, run_id, run["state_version"]),
                    )
                ).fetchone()
                if expired is None:
                    return False
                next_seq = await self._next_event_seq(conn, run_id)
                await conn.execute(
                    """
                    INSERT INTO run_event (
                        run_id, event_seq, event_type, stage, state_version,
                        payload_version, payload
                    )
                    VALUES (%s, %s, 'STATE_CHANGED', %s, %s, 1, %s)
                    """,
                    (
                        run_id,
                        next_seq,
                        run["stage"],
                        next_version,
                        Jsonb(
                            {
                                "from_state": "WAITING_HITL",
                                "to_state": "EXPIRED",
                                "terminal_reason": "HITL_TIMEOUT",
                            }
                        ),
                    ),
                )
                return True
        finally:
            await conn.close()

    async def _run_due_work_once(self) -> None:
        """内部 scheduler adapter 的单次扫描；不是公开 seam。"""
        now = self._clock()
        conn = await self._connect()
        try:
            runnable = await (
                await conn.execute(
                    """
                    SELECT run_id
                    FROM run
                    WHERE (
                        state = 'QUEUED'
                    ) OR (
                        state = 'RUNNING'
                        AND (
                            lease_expires_at IS NULL
                            OR lease_expires_at <= %s
                        )
                    )
                    ORDER BY created_at, run_id
                    LIMIT 20
                    """,
                    (now,),
                )
            ).fetchall()
            expired_hitl = await (
                await conn.execute(
                    """
                    SELECT r.run_id, r.principal_id
                    FROM run r
                    JOIN run_interrupt i ON i.run_id = r.run_id
                    WHERE r.state = 'WAITING_HITL'
                      AND i.state = 'OPEN'
                      AND i.expires_at <= %s
                    ORDER BY i.expires_at, r.run_id
                    LIMIT 20
                    """,
                    (now,),
                )
            ).fetchall()
        finally:
            await conn.close()

        for row in runnable:
            await self._process_profile_run(row["run_id"])
        for row in expired_hitl:
            await self._expire_due_hitl(
                principal_id=row["principal_id"],
                run_id=row["run_id"],
            )

    async def run_due_work_once(self) -> None:
        """内部 worker scheduler 的公开协作方法；仍由 RunLifecycle 掌握状态机。"""
        await self._run_due_work_once()

    async def ResumeRun(self, command: ResumeRunCommand) -> RunReceipt:
        if await self._expire_due_hitl(
            principal_id=command.principal_id,
            run_id=command.run_id,
        ):
            raise RunLifecycleError("INTERRUPT_EXPIRED")
        answer_payload = self._resume_answer_payload(command)
        request_hash = self._request_hash(
            {
                "run_id": str(command.run_id),
                "interrupt_id": str(command.interrupt_id),
                "expected_state_version": command.expected_state_version,
                "profile_version": (
                    str(command.profile_version) if command.profile_version else None
                ),
                "answer": answer_payload,
            }
        )
        scope_key = f"run:{command.run_id}"
        receipt_lock = (
            f"{command.principal_id}:RESUME_RUN:{scope_key}:{command.idempotency_key}"
        )

        conn = await self._connect()
        try:
            async with conn.transaction():
                await self._raise_if_principal_unavailable(
                    conn,
                    command.principal_id,
                )
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (receipt_lock,),
                )
                replay = await self._find_receipt(
                    conn,
                    principal_id=command.principal_id,
                    operation="RESUME_RUN",
                    scope_key=scope_key,
                    idempotency_key=command.idempotency_key,
                )
                if replay is not None:
                    if replay["request_hash"] != request_hash:
                        raise RunLifecycleError("IDEMPOTENCY_CONFLICT")
                    receipt = self._receipt_from_response(replay["response"], replayed=True)
                    accepted_receipt = receipt
                else:
                    run = await (
                        await conn.execute(
                            """
                            SELECT run_id, session_id, state, stage, state_version,
                                   release_id, working_profile_version
                            FROM run
                            WHERE run_id = %s AND principal_id = %s
                            FOR UPDATE
                            """,
                            (command.run_id, command.principal_id),
                        )
                    ).fetchone()
                    if run is None:
                        raise RunLifecycleError("RUN_NOT_FOUND")
                    if run["state"] in {"EXPIRED", "SUCCEEDED", "FAILED", "CANCELLED"}:
                        raise RunLifecycleError("RUN_ALREADY_TERMINAL")
                    if run["state"] != "WAITING_HITL":
                        existing_interrupt = await (
                            await conn.execute(
                                """
                                SELECT state
                                FROM run_interrupt
                                WHERE interrupt_id = %s AND run_id = %s
                                """,
                                (command.interrupt_id, command.run_id),
                            )
                        ).fetchone()
                        if (
                            existing_interrupt is not None
                            and existing_interrupt["state"] != "OPEN"
                        ):
                            raise RunLifecycleError("INTERRUPT_CONSUMED")
                        raise RunLifecycleError("RUN_STATE_CONFLICT")
                    if run["state_version"] != command.expected_state_version:
                        raise RunLifecycleError("RUN_STATE_CONFLICT")

                    interrupt = await (
                        await conn.execute(
                            """
                            SELECT interrupt_id, kind, state, payload, expires_at
                            FROM run_interrupt
                            WHERE interrupt_id = %s AND run_id = %s
                            FOR UPDATE
                            """,
                            (command.interrupt_id, command.run_id),
                        )
                    ).fetchone()
                    if interrupt is None:
                        raise RunLifecycleError("INTERRUPT_NOT_FOUND")
                    if interrupt["state"] != "OPEN":
                        raise RunLifecycleError("INTERRUPT_CONSUMED")
                    if interrupt["expires_at"] <= self._clock():
                        raise RunLifecycleError("INTERRUPT_EXPIRED")

                    release = await (
                        await conn.execute(
                            """
                            SELECT status, revoked_at
                            FROM release
                            WHERE release_id = %s
                            """,
                            (run["release_id"],),
                        )
                    ).fetchone()
                    if (
                        release is None
                        or release["status"] == "REVOKED"
                        or release["revoked_at"] is not None
                    ):
                        raise RunLifecycleError("RELEASE_REVOKED")

                    interrupt_version = interrupt["payload"].get("profile_version")
                    if interrupt["kind"] == "PROFILE_CONFIRM":
                        if not isinstance(
                            command.answer,
                            (ConfirmProfileAnswer, RejectOrEditProfileAnswer),
                        ):
                            raise RunLifecycleError("RUN_STATE_CONFLICT")
                        if (
                            command.profile_version is None
                            or str(command.profile_version) != interrupt_version
                            or command.profile_version != run["working_profile_version"]
                        ):
                            raise RunLifecycleError("PROFILE_VERSION_CONFLICT")
                    else:
                        if (
                            command.profile_version is None
                            or command.profile_version != run["working_profile_version"]
                        ):
                            raise RunLifecycleError("PROFILE_VERSION_CONFLICT")

                    command_id = uuid4()
                    next_version = run["state_version"] + 1
                    next_seq = await self._next_event_seq(conn, run["run_id"])
                    await conn.execute(
                        """
                        UPDATE run_interrupt
                        SET state = 'CONSUMED',
                            consumed_by_command_id = %s,
                            answer_payload = %s
                        WHERE interrupt_id = %s
                          AND state = 'OPEN'
                        """,
                        (command_id, Jsonb(answer_payload), command.interrupt_id),
                    )
                    await conn.execute(
                        """
                        UPDATE run
                        SET state = 'QUEUED',
                            state_version = %s,
                            updated_at = NOW()
                        WHERE run_id = %s
                          AND state = 'WAITING_HITL'
                          AND state_version = %s
                        """,
                        (next_version, run["run_id"], run["state_version"]),
                    )
                    await conn.execute(
                        """
                        INSERT INTO run_event (
                            run_id, event_seq, event_type, stage, state_version,
                            payload_version, payload
                        )
                        VALUES (%s, %s, 'STATE_CHANGED', %s, %s, 1, %s)
                        """,
                        (
                            run["run_id"],
                            next_seq,
                            run["stage"],
                            next_version,
                            Jsonb(
                                {
                                    "from_state": "WAITING_HITL",
                                    "to_state": "QUEUED",
                                    "interrupt_id": str(command.interrupt_id),
                                }
                            ),
                        ),
                    )
                    accepted_receipt = RunReceipt(
                        accepted=True,
                        reason=None,
                        run_id=run["run_id"],
                        session_id=run["session_id"],
                        state="QUEUED",
                        stage=run["stage"],
                        state_version=next_version,
                        first_event_seq=next_seq,
                    )
                    await self._save_receipt(
                        conn,
                        principal_id=command.principal_id,
                        operation="RESUME_RUN",
                        scope_key=scope_key,
                        idempotency_key=command.idempotency_key,
                        request_hash=request_hash,
                        receipt=accepted_receipt,
                        target_run_id=run["run_id"],
                    )
                    await self._store_messages(
                        conn,
                        session_id=run["session_id"],
                        run_id=run["run_id"],
                        messages=(
                            ChatMessageInput(
                                client_message_id=(
                                    f"server:{run['run_id']}:"
                                    f"{command.interrupt_id}:user"
                                ),
                                role="USER",
                                kind="TEXT",
                                content="确认画像",
                            ),
                            ChatMessageInput(
                                client_message_id=(
                                    f"server:{run['run_id']}:"
                                    f"{command.interrupt_id}:assistant"
                                ),
                                role="ASSISTANT",
                                kind="TEXT",
                                content=(
                                    "已确认画像，正在核验招生计划与资格规则。"
                                ),
                            ),
                        ),
                    )
                    await conn.execute(
                        "UPDATE chat_session SET updated_at = NOW() WHERE id = %s",
                        (run["session_id"],),
                    )
            if self._auto_profile:
                if accepted_receipt.accepted:
                    await self._resume_accepted_hook(command.run_id)
                await self._process_profile_run(command.run_id)
            return accepted_receipt
        finally:
            await conn.close()

    async def _ensure_profile_runtime(self) -> None:
        async with self._runtime_lock:
            if self._saver is not None:
                return
            self._saver_cm = AsyncPostgresSaver.from_conn_string(self._dsn)
            self._saver = await self._saver_cm.__aenter__()
            await self._saver.setup()
            self._profile_graph = build_profile_graph(
                self._saver,
                self._algorithm_preview_for_graph,
            )

    async def _claim_run(self, conn, run_id: UUID) -> dict[str, Any] | None:
        """取得执行权；租约未过期的其他 owner 不会被抢占。"""
        now = self._clock()
        async with conn.transaction():
            row = await (
                await conn.execute(
                    """
                    SELECT run_id, session_id, principal_id, task_kind,
                           parent_result_version,
                           task_input, state, stage,
                           state_version, release_id, working_profile_version,
                           confirmed_profile_version, lease_owner, lease_epoch,
                           lease_expires_at, active_execution_remaining_ms,
                           active_execution_started_at, created_at
                    FROM run
                    WHERE run_id = %s
                    FOR UPDATE
                    """,
                    (run_id,),
                )
            ).fetchone()
            if row is None or row["state"] not in {"QUEUED", "RUNNING"}:
                return None
            if (
                row["state"] == "QUEUED"
                and row["confirmed_profile_version"] is not None
                and self._algo_client is None
            ):
                return None
            if row["state"] == "RUNNING":
                expires_at = row["lease_expires_at"]
                if expires_at is not None and expires_at > now:
                    return None
                remaining = row["active_execution_remaining_ms"]
                started_at = row["active_execution_started_at"]
                if started_at is not None:
                    elapsed_ms = max(0, int((now - started_at).total_seconds() * 1000))
                    remaining = max(0, remaining - elapsed_ms)
                if remaining <= 0:
                    return await self._expire_claimed_run(
                        conn,
                        row,
                        state_version=row["state_version"] + 1,
                        terminal_reason="EXECUTION_TIMEOUT",
                    )
            else:
                remaining = row["active_execution_remaining_ms"]

            next_version = row["state_version"] + 1
            next_epoch = row["lease_epoch"] + 1
            next_expiry = now + self._lease_duration
            claimed = await (
                await conn.execute(
                        """
                    UPDATE run
                    SET state = 'RUNNING',
                        state_version = %s,
                        lease_owner = %s,
                        lease_epoch = %s,
                        lease_expires_at = %s,
                        active_execution_remaining_ms = %s,
                        active_execution_started_at = %s,
                        updated_at = NOW()
                    WHERE run_id = %s
                      AND state = %s
                      AND state_version = %s
                      AND lease_epoch = %s
                    RETURNING run_id, session_id, principal_id, task_kind,
                              parent_result_version,
                              task_input, state, stage,
                              state_version, release_id, working_profile_version,
                              confirmed_profile_version, lease_owner, lease_epoch,
                              lease_expires_at, active_execution_remaining_ms,
                              active_execution_started_at, created_at
                    """,
                    (
                        next_version,
                        self._worker_owner,
                        next_epoch,
                        next_expiry,
                        remaining,
                        now,
                        run_id,
                        row["state"],
                        row["state_version"],
                        row["lease_epoch"],
                    ),
                )
            ).fetchone()
            if claimed is None:
                return None
            next_seq = await self._next_event_seq(conn, run_id)
            await conn.execute(
                """
                INSERT INTO run_event (
                    run_id, event_seq, event_type, stage, state_version,
                    payload_version, payload
                )
                VALUES (%s, %s, 'STATE_CHANGED', %s, %s, 1, %s)
                """,
                (
                    run_id,
                    next_seq,
                    claimed["stage"],
                    next_version,
                    Jsonb(
                        {
                            "from_state": row["state"],
                            "to_state": "RUNNING",
                            "lease_owner": self._worker_owner,
                            "lease_epoch": next_epoch,
                        }
                    ),
                ),
            )
            return dict(claimed)

    async def _expire_claimed_run(
        self,
        conn,
        row,
        *,
        state_version: int,
        terminal_reason: str,
    ):
        await conn.execute(
            """
            UPDATE run
            SET state = 'FAILED',
                terminal_reason = %s,
                state_version = %s,
                lease_owner = NULL,
                lease_expires_at = NULL,
                active_execution_started_at = NULL,
                updated_at = NOW()
            WHERE run_id = %s
              AND state = %s
              AND state_version = %s
              AND lease_epoch = %s
            """,
            (
                terminal_reason,
                state_version,
                row["run_id"],
                row["state"],
                row["state_version"],
                row["lease_epoch"],
            ),
        )
        next_seq = await self._next_event_seq(conn, row["run_id"])
        await conn.execute(
            """
            INSERT INTO run_event (
                run_id, event_seq, event_type, stage, state_version,
                payload_version, payload
            )
            VALUES (%s, %s, 'ERROR', %s, %s, 1, %s)
            """,
            (
                row["run_id"],
                next_seq,
                row["stage"],
                state_version,
                Jsonb({"terminal_reason": terminal_reason}),
            ),
        )
        return None

    async def _park_claim(
        self,
        conn,
        row,
        *,
        target_state: str,
        stage: str,
        terminal_reason: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any] | None:
        """释放当前 lease；仅当前 owner/epoch 可以提交。"""
        now = self._clock()
        remaining = row["active_execution_remaining_ms"]
        started_at = row["active_execution_started_at"]
        if started_at is not None:
            elapsed_ms = max(0, int((now - started_at).total_seconds() * 1000))
            remaining = max(0, remaining - elapsed_ms)
        next_version = row["state_version"] + 1
        parked = await (
            await conn.execute(
                """
                UPDATE run
                SET state = %s,
                    stage = %s,
                    terminal_reason = %s,
                    state_version = %s,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    active_execution_remaining_ms = %s,
                    active_execution_started_at = NULL,
                    updated_at = NOW()
                WHERE run_id = %s
                  AND state = 'RUNNING'
                  AND state_version = %s
                  AND lease_owner = %s
                  AND lease_epoch = %s
                RETURNING run_id, session_id, principal_id, task_input, state, stage,
                          state_version, release_id, working_profile_version,
                          confirmed_profile_version, lease_owner, lease_epoch,
                          lease_expires_at, active_execution_remaining_ms,
                          active_execution_started_at
                """,
                (
                    target_state,
                    stage,
                    terminal_reason,
                    next_version,
                    remaining,
                    row["run_id"],
                    row["state_version"],
                    self._worker_owner,
                    row["lease_epoch"],
                ),
            )
        ).fetchone()
        if parked is None:
            raise RunLifecycleError("RUN_STATE_CONFLICT")
        await append_event(
            conn,
            run_id=row["run_id"],
            event_type=(
                "ERROR"
                if target_state in {"FAILED", "EXPIRED"}
                else "STATE_CHANGED"
            ),
            stage=stage,
            state_version=next_version,
            payload={
                "from_state": "RUNNING",
                "to_state": target_state,
                "terminal_reason": terminal_reason,
                **({"message": error_message} if error_message else {}),
            },
        )
        return dict(parked)

    async def _consume_active_budget(
        self,
        conn,
        row: dict[str, Any],
    ) -> dict[str, Any] | None:
        now = self._clock()
        started_at = row["active_execution_started_at"]
        remaining = row["active_execution_remaining_ms"]
        if started_at is not None:
            remaining = max(
                0,
                remaining - int((now - started_at).total_seconds() * 1000),
            )
        if remaining <= 0:
            next_version = row["state_version"] + 1
            async with conn.transaction():
                failed = await (
                    await conn.execute(
                        """
                        UPDATE run
                        SET state = 'FAILED',
                            terminal_reason = 'EXECUTION_TIMEOUT',
                            state_version = %s,
                            active_execution_remaining_ms = 0,
                            lease_owner = NULL,
                            lease_expires_at = NULL,
                            active_execution_started_at = NULL,
                            updated_at = NOW()
                        WHERE run_id = %s
                          AND state = 'RUNNING'
                          AND state_version = %s
                          AND lease_owner = %s
                          AND lease_epoch = %s
                        RETURNING run_id
                        """,
                        (
                            next_version,
                            row["run_id"],
                            row["state_version"],
                            self._worker_owner,
                            row["lease_epoch"],
                        ),
                    )
                ).fetchone()
                if failed is None:
                    return None
                await append_event(
                    conn,
                    run_id=row["run_id"],
                    event_type="ERROR",
                    stage=row["stage"],
                    state_version=next_version,
                    payload={
                        "code": "EXECUTION_TIMEOUT",
                        "message": "主动执行额度已耗尽",
                        "retryable": False,
                    },
                )
            return None

        async with conn.transaction():
            updated = await (
                await conn.execute(
                        """
                    UPDATE run
                    SET active_execution_remaining_ms = %s,
                        active_execution_started_at = %s,
                        updated_at = NOW()
                    WHERE run_id = %s
                      AND state = 'RUNNING'
                      AND state_version = %s
                      AND lease_owner = %s
                      AND lease_epoch = %s
                    RETURNING run_id
                    """,
                    (
                        remaining,
                        now,
                        row["run_id"],
                        row["state_version"],
                        self._worker_owner,
                        row["lease_epoch"],
                    ),
                )
            ).fetchone()
        if updated is None:
            return None
        consumed = dict(row)
        consumed["active_execution_remaining_ms"] = remaining
        consumed["active_execution_started_at"] = now
        return consumed

    async def _stop_if_cancelled(self, conn, row: dict[str, Any]) -> bool:
        current = await (
            await conn.execute(
                """
                SELECT state, state_version, lease_owner, lease_epoch,
                       cancel_requested_at
                FROM run
                WHERE run_id = %s
                """,
                (row["run_id"],),
            )
        ).fetchone()
        if current is None or current["cancel_requested_at"] is None:
            return False
        if (
            current["state"] != "RUNNING"
            or current["lease_owner"] != self._worker_owner
            or current["lease_epoch"] != row["lease_epoch"]
        ):
            return False
        next_version = current["state_version"] + 1
        async with conn.transaction():
            stopped = await (
                await conn.execute(
                    """
                    UPDATE run
                    SET state = 'CANCELLED',
                        terminal_reason = 'CANCELLED_BY_USER',
                        state_version = %s,
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        active_execution_started_at = NULL,
                        updated_at = NOW()
                    WHERE run_id = %s
                      AND state = 'RUNNING'
                      AND state_version = %s
                      AND lease_owner = %s
                      AND lease_epoch = %s
                    RETURNING run_id
                    """,
                    (
                        next_version,
                        row["run_id"],
                        current["state_version"],
                        self._worker_owner,
                        row["lease_epoch"],
                    ),
                )
            ).fetchone()
            if stopped is None:
                return False
            await append_event(
                conn,
                run_id=row["run_id"],
                event_type="STATE_CHANGED",
                stage=row["stage"],
                state_version=next_version,
                payload={
                    "from_state": "RUNNING",
                    "to_state": "CANCELLED",
                    "terminal_reason": "CANCELLED_BY_USER",
                },
            )
        return True

    async def _process_policy_qa(
        self,
        conn,
        row: dict[str, Any],
    ) -> None:
        if self._policy_retriever is None:
            await self._park_claim(
                conn,
                row,
                target_state="FAILED",
                stage="RETRIEVING",
                terminal_reason="DEPENDENCY_UNAVAILABLE",
            )
            return
        try:
            input_data = dict(row["task_input"])
            policy_input = PolicyQaInput(
                query_text=str(input_data.get("query_text") or ""),
                province=str(input_data.get("province") or ""),
                application_year=input_data.get("application_year"),
                category=str(input_data.get("category") or ""),
                batch=str(input_data.get("batch") or ""),
                school_ids=tuple(input_data.get("school_ids") or ()),
                document_types=tuple(input_data.get("document_types") or ()),
                mode=str(input_data.get("mode") or "hybrid"),
            )
            if not policy_input.query_text.strip():
                raise RunLifecycleError("RETRIEVAL_QUERY_REQUIRED")
            if (
                policy_input.application_year is None
                or not policy_input.province
                or not policy_input.category
                or not policy_input.batch
            ):
                raise RunLifecycleError("RETRIEVAL_SCOPE_REQUIRED")
            result = await self._policy_retriever.retrieve(
                PolicyRetrievalQuery(
                    principal_id=row["principal_id"],
                    query_text=policy_input.query_text,
                    scope=ApplicabilityScope(
                        province=policy_input.province,
                        application_year=int(policy_input.application_year),
                        category=policy_input.category,
                        batch=policy_input.batch,
                    ),
                    release_id=row["release_id"],
                    mode=policy_input.mode,
                    school_ids=policy_input.school_ids,
                    document_types=policy_input.document_types,
                )
            )
        except PolicyRetrievalError as exc:
            await self._park_claim(
                conn,
                row,
                target_state="FAILED",
                stage="RETRIEVING",
                terminal_reason=exc.reason,
            )
            return
        except RunLifecycleError as exc:
            await self._park_claim(
                conn,
                row,
                target_state="FAILED",
                stage="RETRIEVING",
                terminal_reason=exc.reason,
            )
            return
        except Exception:
            await self._park_claim(
                conn,
                row,
                target_state="FAILED",
                stage="RETRIEVING",
                terminal_reason="RETRIEVAL_UNAVAILABLE",
            )
            return

        if result.status not in {"FOUND", "NO_EVIDENCE"}:
            await self._park_claim(
                conn,
                row,
                target_state="FAILED",
                stage="RETRIEVING",
                terminal_reason=result.status,
            )
            return
        await self._publish_policy_evidence(conn, row=row, result=result)

    async def _publish_policy_evidence(
        self,
        conn,
        *,
        row: dict[str, Any],
        result: PolicyRetrievalResult,
    ) -> None:
        result_version = uuid4()
        payload = {
            "result_version": str(result_version),
            "result_kind": "POLICY_EVIDENCE",
            "run_id": str(row["run_id"]),
            "release_id": result.release_id,
            "retrieval_status": result.status,
            "degraded_reason": result.diagnostics.degraded_reason,
            "vector_candidates": result.diagnostics.vector_candidates,
            "keyword_candidates": result.diagnostics.keyword_candidates,
            "rerank_enabled": result.diagnostics.rerank_enabled,
            "rerank_model": result.diagnostics.rerank_model,
            "rerank_config_version": (
                result.diagnostics.rerank_config_version
            ),
            "rerank_timeout_ms": result.diagnostics.rerank_timeout_ms,
            "relevance_filtered_candidates": (
                result.diagnostics.relevance_filtered_candidates
            ),
            "relevance_policy_version": (
                result.diagnostics.relevance_policy_version
            ),
            "evidence": [
                {
                    "evidence_id": item.evidence_id,
                    "evidence_type": item.evidence_type,
                    "content_sha256": item.content_sha256,
                    "created_at": item.created_at.isoformat(),
                    "producer_version": item.producer_version,
                    "doc_id": item.document.doc_id,
                    "chunk_id": item.document.chunk_id,
                    "title": item.document.title,
                    "document_type": item.document.document_type,
                    "issuer": item.document.issuer,
                    "source_uri": item.document.source_uri,
                    "fetched_at": item.document.fetched_at.isoformat(),
                    "section_path": item.document.section_path,
                    "excerpt": item.document.excerpt,
                    "excerpt_sha256": item.document.excerpt_sha256,
                    "char_start": item.document.char_start,
                    "char_end": item.document.char_end,
                    "regions": list(item.document.applicability.regions),
                    "categories": list(item.document.applicability.categories),
                    "batches": list(item.document.applicability.batches),
                    "valid_from": (
                        item.document.applicability.valid_from.isoformat()
                    ),
                    "valid_to": (
                        item.document.applicability.valid_to.isoformat()
                        if item.document.applicability.valid_to is not None
                        else None
                    ),
                    "release_id": item.release_id,
                }
                for item in result.evidence
            ],
        }
        async with conn.transaction():
            current = await (
                await conn.execute(
                    """
                    SELECT state, state_version, lease_owner, lease_epoch,
                           release_id, cancel_requested_at
                    FROM run
                    WHERE run_id = %s
                    FOR UPDATE
                    """,
                    (row["run_id"],),
                )
            ).fetchone()
            if (
                current is None
                or current["state"] != "RUNNING"
                or current["lease_owner"] != self._worker_owner
                or current["lease_epoch"] != row["lease_epoch"]
                or current["release_id"] != result.release_id
                or current["cancel_requested_at"] is not None
            ):
                raise RunLifecycleError("RUN_STATE_CONFLICT")
            next_version = current["state_version"] + 1
            await conn.execute(
                """
                INSERT INTO run_result (
                    result_version, run_id, release_id,
                    confirmed_profile_version, result_kind, payload,
                    verification_summary, unverified_conditions
                )
                VALUES (%s, %s, %s, NULL, 'POLICY_EVIDENCE', %s, %s, %s)
                """,
                (
                    result_version,
                    row["run_id"],
                    result.release_id,
                    Jsonb(payload),
                    Jsonb([]),
                    Jsonb([]),
                ),
            )
            updated = await (
                await conn.execute(
                    """
                    UPDATE run
                    SET state = 'SUCCEEDED',
                        stage = 'EVIDENCE_READY',
                        terminal_reason = NULL,
                        state_version = %s,
                        result_version = %s,
                        result_kind = 'POLICY_EVIDENCE',
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        active_execution_started_at = NULL,
                        updated_at = NOW()
                    WHERE run_id = %s
                      AND state = 'RUNNING'
                      AND state_version = %s
                      AND lease_owner = %s
                      AND lease_epoch = %s
                    RETURNING run_id
                    """,
                    (
                        next_version,
                        result_version,
                        row["run_id"],
                        current["state_version"],
                        self._worker_owner,
                        row["lease_epoch"],
                    ),
                )
            ).fetchone()
            if updated is None:
                raise RunLifecycleError("RUN_STATE_CONFLICT")
            await append_event(
                conn,
                run_id=row["run_id"],
                event_type="STATE_CHANGED",
                stage="EVIDENCE_READY",
                state_version=next_version,
                payload={"from_state": "RUNNING", "to_state": "SUCCEEDED"},
            )
            await append_event(
                conn,
                run_id=row["run_id"],
                event_type="RESULT",
                stage="EVIDENCE_READY",
                state_version=next_version,
                payload={
                    "result_version": str(result_version),
                    "result_kind": "POLICY_EVIDENCE",
                    "retrieval": payload,
                },
                payload_version=1,
            )

    async def _process_algorithm_preview(self, conn, row: dict[str, Any]) -> None:
        config = {"configurable": {"thread_id": str(row["run_id"])}}
        snapshot = await self._profile_graph.aget_state(config)
        pending = self._pending_interrupt(snapshot)
        pending_value = getattr(pending, "value", None) if pending is not None else None
        if (
            isinstance(pending_value, dict)
            and pending_value.get("kind") == "INTERNAL_ALGORITHM_PREVIEW"
        ):
            await self._profile_graph.ainvoke(
                Command(
                    resume={
                        "type": "START_ALGORITHM_PREVIEW",
                        "run_id": str(row["run_id"]),
                    }
                ),
                config,
            )
            snapshot = await self._profile_graph.aget_state(config)
        preview = snapshot.values.get("preview")
        if preview is None:
            preview = await self._build_algorithm_preview(conn, row)
        revision = await (
            await conn.execute(
                """
                SELECT fields
                FROM profile_revision
                WHERE profile_version = %s
                """,
                (row["confirmed_profile_version"],),
            )
        ).fetchone()
        if revision is None:
            raise RunLifecycleError("PROFILE_NOT_CONFIRMED")
        await require_sensitive_consent(
            conn,
            principal_id=row["principal_id"],
            fields=revision["fields"],
            policy_version=self._consent_policy_version,
        )
        artifact = await self._load_formal_gate_artifact(
            principal_id=row["principal_id"],
            run_id=row["run_id"],
        )
        profile = await (
            await conn.execute(
                """
                SELECT lifecycle, fields
                FROM profile_revision
                WHERE profile_version = %s
                """,
                (row["confirmed_profile_version"],),
            )
        ).fetchone()
        if profile is None or profile["lifecycle"] != "CONFIRMED":
            raise RunLifecycleError("PROFILE_NOT_CONFIRMED")
        fields = profile["fields"]
        try:
            package, report_payload = build_formal_report(
                preview=preview,
                artifact=artifact,
                scope=VerificationScope(
                    province=str(fields["province"]["value"]),
                    application_year=int(fields["application_year"]["value"]),
                    category=str(fields["category"]["value"]),
                    batch=str(fields["batch"]["value"]),
                    school_id=None,
                ),
                applicable_as_of=row["created_at"].date(),
                created_at=row["created_at"],
            )
            if self._report_narrative_writer is not None:
                narrative_request = ReportNarrativeRequest(
                    principal_id=row["principal_id"],
                    session_id=str(row["session_id"]),
                    run_id=str(row["run_id"]),
                    release_id=artifact["release_id"],
                    profile_version=artifact["profile_version"],
                    result_version=preview["result_version"],
                    candidate_count=len(
                        preview["wishlist"]["candidates"]
                    ),
                    scope_label=(
                        f"{fields['province']['value']}"
                        f"{fields['category']['value']}"
                    ),
                    forbidden_fragments=self._report_forbidden_fragments(
                        preview
                    ),
                )
                for rewrite_attempt in range(2):
                    try:
                        narrative = await self._report_narrative_writer.compose(
                            narrative_request
                        )
                        package, report_payload = apply_report_narrative(
                            package=package,
                            report=report_payload,
                            request=narrative_request,
                            narrative=narrative,
                        )
                        break
                    except ReportNarrativeViolation as exc:
                        logger.warning(
                            "report narrative rejected: %s",
                            exc,
                        )
                        raise RunLifecycleError(
                            "VALIDATION_FAILED",
                            f"报告叙事越过确定性事实边界: {exc}",
                        ) from exc
                    except ReportNarrativeUnavailable as exc:
                        report_payload = apply_report_fallback(
                            report_payload,
                            reason=exc.reason,
                        )
                        break
                    except ReportNarrativeRewriteRequired as exc:
                        if rewrite_attempt == 1:
                            report_payload = apply_report_fallback(
                                report_payload,
                                reason=exc.reason,
                            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RunLifecycleError(
                "VALIDATION_FAILED",
                "正式报告输入不完整或无法规范化",
            ) from exc
        publication = await self.PublishFormalResult(
            PublishFormalResultCommand(
                principal_id=row["principal_id"],
                run_id=row["run_id"],
                expected_state_version=row["state_version"],
                expected_lease_epoch=row["lease_epoch"],
                package=package,
                report=report_payload,
            )
        )
        if not publication.accepted:
            return

    @staticmethod
    def _report_forbidden_fragments(
        preview: dict[str, Any],
    ) -> tuple[str, ...]:
        fragments: set[str] = set()
        for item in preview["wishlist"]["candidates"]:
            candidate = item["cwp"]["candidate"]
            for value in (
                candidate.get("university_name"),
                candidate.get("university_code"),
                candidate.get("major_group"),
            ):
                if value:
                    fragments.add(str(value))
            identity = (
                f"{candidate.get('university_code', '')}|"
                f"{candidate.get('major_group', '')}"
            )
            if identity.strip("|"):
                fragments.add(identity)
            for major in candidate.get("majors", ()):
                for value in (
                    major.get("major_name"),
                    major.get("major_code"),
                ):
                    if value:
                        fragments.add(str(value))
        return tuple(sorted(fragments))

    async def _algorithm_preview_for_graph(
        self,
        run_id: str,
    ) -> AlgorithmPreviewPayload:
        conn = await self._connect(autocommit=True)
        try:
            row = await (
                await conn.execute(
                    """
                    SELECT run_id, session_id, principal_id, parent_result_version,
                           task_kind, task_input, state, stage, state_version,
                           release_id, working_profile_version,
                           confirmed_profile_version, lease_owner, lease_epoch,
                           lease_expires_at, active_execution_remaining_ms,
                           active_execution_started_at, created_at
                    FROM run
                    WHERE run_id = %s
                    """,
                    (UUID(run_id),),
                )
            ).fetchone()
            if row is None or row["state"] != "RUNNING":
                raise RunLifecycleError("RUN_STATE_CONFLICT")
            return await self._build_algorithm_preview(conn, row)
        finally:
            await conn.close()

    async def _build_algorithm_preview(
        self,
        conn,
        row: dict[str, Any],
    ) -> AlgorithmPreviewPayload:
        profile_version = row["confirmed_profile_version"]
        revision = await (
            await conn.execute(
                """
                SELECT fields, lifecycle
                FROM profile_revision
                WHERE profile_version = %s
                """,
                (profile_version,),
            )
        ).fetchone()
        if revision is None:
            raise RunLifecycleError("PROFILE_NOT_CONFIRMED")
        await require_sensitive_consent(
            conn,
            principal_id=row["principal_id"],
            fields=revision["fields"],
            policy_version=self._consent_policy_version,
        )
        algo_input = self._profile.ToAlgoInput(
            profile_version=profile_version,
            lifecycle=revision["lifecycle"],
            fields=revision["fields"],
        )
        trace = common_pb2.Trace(
            trace_id=str(row["run_id"]),
            session_id=str(row["session_id"]),
            node="algorithm_preview",
        )
        user_profile = algo_pb2.UserProfile(**algo_input["profile"])
        release_id = row["release_id"]

        try:
            rank = await self._algo_client.RankConvert(
                algo_pb2.RankConvertRequest(
                    trace=trace,
                    province=algo_input["province"],
                    year=algo_input["year"],
                    category=algo_input["category"],
                    score=algo_input["score"],
                    release_id=release_id,
                )
            )
            estimate = await self._algo_client.EstimateProb(
                algo_pb2.EstimateProbRequest(
                    trace=trace,
                    province=algo_input["province"],
                    year=algo_input["year"],
                    category=algo_input["category"],
                    batch=algo_input["batch"],
                    score=algo_input["score"],
                    subjects=algo_input["subjects"],
                    profile=user_profile,
                    release_id=release_id,
                )
            )
            source_candidates = [
                *estimate.rush_candidates,
                *estimate.stable_candidates,
                *estimate.safe_candidates,
            ]
            if not estimate.HasField("basis"):
                raise RunLifecycleError(
                    "VALIDATION_FAILED",
                    "EstimateProb 缺少 AuditBasis",
                )
            if rank.rank <= 0 or estimate.basis.user_rank != rank.rank:
                raise RunLifecycleError(
                    "VALIDATION_FAILED",
                    "位次与 AuditBasis 不一致",
                )
            if not source_candidates:
                raise RunLifecycleError("NO_RELIABLE_DATA", "算法未返回可用候选")
            source_by_identity: dict[tuple[str, str], float] = {}
            for item in source_candidates:
                identity = self._candidate_identity(item)
                if identity in source_by_identity:
                    raise RunLifecycleError(
                        "VALIDATION_FAILED",
                        "EstimateProb 返回重复候选",
                    )
                source_by_identity[identity] = item.prob.probability
            eligibility_fact_dicts = self._profile.ToEligibilityFacts(
                lifecycle=revision["lifecycle"],
                fields=revision["fields"],
            )
            eligibility = await self._algo_client.EvaluateEligibility(
                algo_pb2.EvaluateEligibilityRequest(
                    trace=trace,
                    release_id=release_id,
                    confirmed_profile_version=str(profile_version),
                    profile_lifecycle=(
                        algo_pb2.EligibilityProfileLifecycle
                        .ELIGIBILITY_PROFILE_LIFECYCLE_CONFIRMED
                    ),
                    scope=algo_pb2.EligibilityScope(
                        province=algo_input["province"],
                        application_year=int(algo_input["year"]),
                        category=algo_input["category"],
                        batch=algo_input["batch"],
                    ),
                    profile_facts=[
                        self._eligibility_profile_fact(fact)
                        for fact in eligibility_fact_dicts
                    ],
                    profile_snapshot_sha256=self._eligibility_profile_snapshot_sha256(
                        str(profile_version),
                        eligibility_fact_dicts,
                    ),
                    candidate_groups=self._eligibility_candidate_groups(
                        source_candidates
                    ),
                )
            )
            eligibility_gate = self._validate_eligibility_result(
                eligibility,
                source_candidates=source_candidates,
            )
            await self._record_formal_gate_artifact(
                conn,
                row=row,
                qualification=self._formal_qualification_summary(
                    eligibility
                ),
            )
            eligible_identities = set(
                eligibility_gate["eligible_group_keys"]
            )
            eligible_candidates = [
                item
                for item in source_candidates
                if self._eligibility_group_key(item) in eligible_identities
            ]
            if not eligible_candidates:
                raise RunLifecycleError(
                    "NO_RELIABLE_DATA",
                    "全部候选均未通过结构化资格规则",
                )
            optimized = await self._algo_client.OptimizeWishlist(
                algo_pb2.OptimizeWishlistRequest(
                    trace=trace,
                    candidates=eligible_candidates,
                    profile=user_profile,
                    release_id=release_id,
                )
            )
        except grpc.RpcError as exc:
            reason = grpc_error_reason(exc)
            if reason in ALGO_BUSINESS_ERROR_REASONS:
                message = exc.details().strip() if hasattr(exc, "details") else ""
                raise RunLifecycleError(reason, message or reason) from exc
            raise RunLifecycleError(
                "DEPENDENCY_UNAVAILABLE",
                f"svc-algo 不可用: {exc.code().name}",
            ) from exc

        if not optimized.HasField("wishlist") or not optimized.wishlist.candidates:
            raise RunLifecycleError("NO_RELIABLE_DATA", "算法未返回可用志愿表")

        optimized_identities: set[tuple[str, str]] = set()
        for item in optimized.wishlist.candidates:
            identity = self._candidate_identity(item.cwp)
            if (
                identity in optimized_identities
                or identity not in source_by_identity
                or self._eligibility_group_key(item.cwp)
                not in eligible_identities
                or source_by_identity[identity] != item.cwp.prob.probability
            ):
                raise RunLifecycleError(
                    "VALIDATION_FAILED",
                    "优化引入新候选、重复候选或改写概率",
                )
            optimized_identities.add(identity)

        self._apply_major_eligibility_to_wishlist(
            optimized.wishlist,
            eligibility,
        )

        input_data = dict(row["task_input"])
        if (
            row["task_kind"] == "REVISE_PLAN"
            and input_data.get("allocation_request") == "MORE_RUSH"
        ):
            parent_counts = await self._formal_tier_counts(
                conn,
                row["parent_result_version"],
            )
            if parent_counts is None:
                raise RunLifecycleError(
                    "VALIDATION_FAILED",
                    "冲档修订缺少父报告分档摘要",
                )
            parent_rush_count = parent_counts[0]
            revised_rush_count = optimized.wishlist.report.rush_count
            if revised_rush_count <= parent_rush_count:
                raise RunLifecycleError(
                    "TARGET_ALLOCATION_UNSATISFIED",
                    (
                        f"当前仅 {estimate.rush_total} 个冲档候选，"
                        f"新版仍为 {revised_rush_count} 个；"
                        "无法在现有硬约束下继续增加冲档"
                    ),
                )

        allocation_warning = ""
        if (
            algo_input["profile"].get("risk_appetite") == "aggressive"
            and optimized.wishlist.report.rush_count >= estimate.rush_total
        ):
            allocation_warning = (
                f"当前冲档候选仅 {estimate.rush_total} 个，"
                "且已全部纳入最终志愿表；无法继续增加冲档。"
            )

        result_version = uuid4()
        preview = self._algorithm_preview_payload(
            result_version=result_version,
            row=row,
            rank=rank,
            estimate=estimate,
            eligibility=eligibility,
            eligibility_gate=eligibility_gate,
            eligible_candidates=eligible_candidates,
            optimized=optimized,
            allocation_warning=allocation_warning,
        )
        return preview

    @staticmethod
    def _apply_major_eligibility_to_wishlist(wishlist, eligibility) -> None:
        group_status = {
            group.group_key: {
                major.major_code: major
                for major in group.majors
            }
            for group in eligibility.groups
        }
        status_names = {
            algo_pb2.MajorEligibilityStatus.MAJOR_ELIGIBILITY_STATUS_PASS:
                "PASS",
            algo_pb2.MajorEligibilityStatus.MAJOR_ELIGIBILITY_STATUS_FAIL:
                "FAIL",
            algo_pb2.MajorEligibilityStatus.MAJOR_ELIGIBILITY_STATUS_UNKNOWN:
                "UNKNOWN",
            algo_pb2.MajorEligibilityStatus.MAJOR_ELIGIBILITY_STATUS_NOT_APPLICABLE:
                "NOT_APPLICABLE",
            algo_pb2.MajorEligibilityStatus
            .MAJOR_ELIGIBILITY_STATUS_MISSING_REQUIRED_FACT:
                "MISSING_REQUIRED_FACT",
        }
        for item in wishlist.candidates:
            candidate = item.cwp.candidate
            key = f"{candidate.university_code}|{candidate.major_group}"
            majors_by_code = group_status.get(key)
            if majors_by_code is None:
                raise RunLifecycleError(
                    "VALIDATION_FAILED",
                    "优化志愿组缺少专业资格结果",
                )
            initial_recommendation = {}
            for major in candidate.majors:
                result = majors_by_code.get(major.major_code)
                if result is None:
                    raise RunLifecycleError(
                        "VALIDATION_FAILED",
                        "优化专业缺少资格结果",
                    )
                status = status_names.get(result.status, "UNKNOWN")
                initial_recommendation[major.major_code] = (
                    major.recommended,
                    major.recommendation_rank,
                )
                major.recommended = False
                major.recommendation_rank = 0
                major.selectable = status == "PASS"
                if status == "PASS":
                    major.unavailable_reason = ""
                else:
                    reasons = tuple(result.failure_rule_keys) + tuple(
                        result.unknown_reasons
                    ) + tuple(result.missing_fields)
                    detail = "、".join(reason for reason in reasons if reason)
                    major.unavailable_reason = (
                        f"{status}：{detail}" if detail else status
                    )

            eligible = []
            for major in candidate.majors:
                result = majors_by_code[major.major_code]
                status = status_names.get(result.status, "UNKNOWN")
                if status != "PASS":
                    continue
                was_recommended, original_rank = initial_recommendation[
                    major.major_code
                ]
                eligible.append((
                    0 if was_recommended else 1,
                    original_rank or 2**31 - 1,
                    major.major_code,
                    major,
                ))
            eligible.sort(key=lambda item: item[:3])
            for rank, (_, _, _, major) in enumerate(eligible[:6], start=1):
                major.recommended = True
                major.recommendation_rank = rank

    async def _record_formal_gate_artifact(
        self,
        conn,
        *,
        row: dict[str, Any],
        qualification: QualificationSummary,
    ) -> None:
        release = await (
            await conn.execute(
                """
                SELECT manifest_sha256
                FROM release
                WHERE release_id = %s
                """,
                (row["release_id"],),
            )
        ).fetchone()
        if release is None:
            raise RunLifecycleError("RELEASE_UNAVAILABLE")
        algorithm_config_sha256 = sha256_payload(
            {
                "release_id": row["release_id"],
                "manifest_sha256": release["manifest_sha256"],
            }
        )
        artifact = formal_gate_artifact(
            release_id=row["release_id"],
            profile_version=str(row["confirmed_profile_version"]),
            verifier_version=self._formal_verifier_version,
            applicable_as_of=row["created_at"].date(),
            algorithm_config_version=row["release_id"],
            algorithm_config_sha256=algorithm_config_sha256,
            qualification=qualification,
        )
        artifact_sha256 = formal_gate_artifact_sha256(artifact)
        inserted = await (
            await conn.execute(
                """
                INSERT INTO run_formal_gate_artifact (
                    run_id, release_id, confirmed_profile_version,
                    verifier_version, applicable_as_of,
                    algorithm_config_version, algorithm_config_sha256,
                    artifact_sha256, artifact_json
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_id) DO NOTHING
                RETURNING run_id
                """,
                (
                    row["run_id"],
                    row["release_id"],
                    row["confirmed_profile_version"],
                    artifact["verifier_version"],
                    artifact["applicable_as_of"],
                    artifact["algorithm_config_version"],
                    artifact["algorithm_config_sha256"],
                    artifact_sha256,
                    Jsonb(artifact),
                ),
            )
        ).fetchone()
        if inserted is not None:
            return
        existing = await (
            await conn.execute(
                """
                SELECT artifact_sha256
                FROM run_formal_gate_artifact
                WHERE run_id = %s
                """,
                (row["run_id"],),
            )
        ).fetchone()
        if (
            existing is None
            or existing["artifact_sha256"] != artifact_sha256
        ):
            raise RunLifecycleError("FORMAL_GATE_ARTIFACT_CONFLICT")

    @classmethod
    def _formal_qualification_summary(cls, eligibility):
        def status_name(enum_type, prefix, value):
            return enum_type.Name(value).removeprefix(prefix)

        coverage = tuple(
            sorted(
                (
                    (
                        declaration.rule_type,
                        status_name(
                            algo_pb2.RuleCoverageStatus,
                            "RULE_COVERAGE_STATUS_",
                            declaration.status,
                        ),
                    )
                    for declaration in eligibility.coverage
                ),
                key=lambda item: item[0],
            )
        )
        eligible_statuses = {
            algo_pb2.GroupEligibilityStatus.GROUP_ELIGIBILITY_STATUS_PASS,
            algo_pb2.GroupEligibilityStatus.GROUP_ELIGIBILITY_STATUS_PASS_PARTIAL,
        }
        groups = []
        for group in eligibility.groups:
            if group.status not in eligible_statuses:
                continue
            majors = tuple(
                sorted(
                    (
                        (
                            major.major_code,
                            status_name(
                                algo_pb2.MajorEligibilityStatus,
                                "MAJOR_ELIGIBILITY_STATUS_",
                                major.status,
                            ),
                        )
                        for major in group.majors
                    ),
                    key=lambda item: item[0],
                )
            )
            restricted_codes = tuple(
                code
                for code, status in majors
                if status in {"FAIL", "NOT_APPLICABLE"}
            )
            groups.append(
                (
                    group.group_key,
                    status_name(
                        algo_pb2.GroupEligibilityStatus,
                        "GROUP_ELIGIBILITY_STATUS_",
                        group.status,
                    ),
                    majors,
                    tuple(
                        f"受限专业：{code}"
                        for code in restricted_codes
                    ),
                )
            )
        return QualificationSummary(
            status=status_name(
                algo_pb2.EligibilityResultStatus,
                "ELIGIBILITY_RESULT_STATUS_",
                eligibility.status,
            ),
            coverage=tuple(
                QualificationCoverage(
                    rule_type=rule_type,
                    status=status,
                )
                for rule_type, status in coverage
            ),
            groups=tuple(
                QualificationGroup(
                    group_key=group_key,
                    status=status,
                    majors=tuple(
                        QualificationMajor(
                            major_code=code,
                            status=major_status,
                        )
                        for code, major_status in majors
                    ),
                    restrictions=restrictions,
                )
                for group_key, status, majors, restrictions in sorted(
                    groups,
                    key=lambda item: item[0],
                )
            ),
            conflict_count=eligibility.diagnostics.conflict_count,
            unknown_count=eligibility.diagnostics.unknown_count,
        )

    @staticmethod
    def _candidate_identity(candidate) -> tuple[str, str]:
        if not candidate.HasField("candidate") or not candidate.HasField("prob"):
            raise RunLifecycleError("VALIDATION_FAILED", "候选缺少身份或概率")
        identity = (
            candidate.candidate.university_code,
            candidate.candidate.major_group,
        )
        probability = candidate.prob.probability
        if (
            not all(identity)
            or not math.isfinite(probability)
            or not 0.01 <= probability <= 0.99
        ):
            raise RunLifecycleError("VALIDATION_FAILED", "候选身份或概率非法")
        return identity

    @staticmethod
    def _eligibility_group_key(candidate) -> str:
        if not candidate.HasField("candidate"):
            raise RunLifecycleError("VALIDATION_FAILED", "候选缺少身份")
        identity = (
            candidate.candidate.university_code,
            candidate.candidate.major_group,
        )
        if not all(identity):
            raise RunLifecycleError("VALIDATION_FAILED", "候选身份不完整")
        return "|".join(identity)

    @staticmethod
    def _eligibility_profile_fact(fact: dict[str, Any]):
        state_name = "ELIGIBILITY_PROFILE_FIELD_STATE_" + str(fact["state"])
        try:
            state = algo_pb2.EligibilityProfileFieldState.Value(state_name)
        except ValueError as exc:
            raise RunLifecycleError(
                "PROFILE_INVALID_VALUE",
                f"资格字段状态非法: {fact['state']}",
            ) from exc
        converted = algo_pb2.EligibilityProfileFact(
            field_key=str(fact["field_key"]),
            state=state,
        )
        value = fact.get("value")
        if isinstance(value, list):
            converted.string_values.extend(value)
        elif isinstance(value, int) and not isinstance(value, bool):
            converted.int_value = value
        elif isinstance(value, str):
            converted.string_value = value
        elif value is not None:
            raise RunLifecycleError(
                "PROFILE_INVALID_VALUE",
                f"资格字段值类型非法: {fact['field_key']}",
            )
        return converted

    @staticmethod
    def _eligibility_profile_snapshot_sha256(
        profile_version: str,
        facts: list[dict[str, Any]],
    ) -> str:
        def encode(value: str) -> str:
            return base64.urlsafe_b64encode(value.encode("utf-8")).decode(
                "ascii"
            ).rstrip("=")

        lines = []
        for fact in facts:
            value = fact.get("value")
            string_values: list[str] = []
            string_value = ""
            int_value = 0
            if isinstance(value, list):
                string_values = [str(item) for item in value]
            elif isinstance(value, int) and not isinstance(value, bool):
                int_value = value
            elif isinstance(value, str):
                string_value = value
            lines.append(
                "|".join(
                    [
                        encode(str(fact["field_key"])),
                        str(fact["state"]),
                        encode(string_value),
                        str(int_value),
                        ",".join(encode(item) for item in string_values),
                    ]
                )
            )
        lines.sort()
        payload = encode(profile_version) + "\n" + "\n".join(lines) + "\n"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _eligibility_candidate_groups(candidates):
        groups = []
        for item in candidates:
            candidate = item.candidate
            if not candidate.majors:
                raise RunLifecycleError(
                    "VALIDATION_FAILED",
                    "资格评估候选缺少专业明细",
                )
            seen_major_codes: set[str] = set()
            majors = []
            for major in candidate.majors:
                if not major.major_code or major.major_code in seen_major_codes:
                    raise RunLifecycleError(
                        "VALIDATION_FAILED",
                        "资格评估专业身份为空或重复",
                    )
                seen_major_codes.add(major.major_code)
                majors.append(
                    algo_pb2.EligibilityCandidateMajor(
                        major_code=major.major_code,
                        major_name=major.major_name,
                        subject_requirement=major.subject_req,
                        in_current_plan=major.in_current_plan,
                    )
                )
            groups.append(
                algo_pb2.EligibilityCandidateGroup(
                    university_id=candidate.university_id,
                    university_code=candidate.university_code,
                    university_name=candidate.university_name,
                    major_group=candidate.major_group,
                    majors=sorted(majors, key=lambda major: major.major_code),
                )
            )
        return groups

    @classmethod
    def _validate_eligibility_result(
        cls,
        eligibility,
        *,
        source_candidates,
    ) -> dict[str, Any]:
        status = eligibility.status
        blocked = (
            algo_pb2.EligibilityResultStatus
            .ELIGIBILITY_RESULT_STATUS_BLOCKED
        )
        complete = (
            algo_pb2.EligibilityResultStatus
            .ELIGIBILITY_RESULT_STATUS_COMPLETE
        )
        partial = (
            algo_pb2.EligibilityResultStatus
            .ELIGIBILITY_RESULT_STATUS_PARTIAL
        )
        if status == blocked:
            reasons = {
                reason
                for group in eligibility.groups
                for major in group.majors
                for reason in major.unknown_reasons
            }
            if "RELEASE_REVOKED" in reasons:
                raise RunLifecycleError("RELEASE_REVOKED")
            detail = ",".join(sorted(reasons)) or "资格规则返回阻断"
            raise RunLifecycleError("VALIDATION_FAILED", detail)
        if status not in {complete, partial}:
            raise RunLifecycleError(
                "VALIDATION_FAILED",
                "资格评估返回未定义状态",
            )
        if not eligibility.groups:
            raise RunLifecycleError(
                "NO_RELIABLE_DATA",
                "资格评估未返回候选组",
            )

        source_major_codes: dict[str, set[str]] = {}
        for item in source_candidates:
            key = cls._eligibility_group_key(item)
            source_major_codes[key] = {
                major.major_code for major in item.candidate.majors
            }
        source_keys = set(source_major_codes)

        result_by_key = {}
        for group in eligibility.groups:
            if not group.group_key or group.group_key in result_by_key:
                raise RunLifecycleError(
                    "VALIDATION_FAILED",
                    "资格结果包含空或重复候选组",
                )
            if group.group_key not in source_keys:
                raise RunLifecycleError(
                    "VALIDATION_FAILED",
                    "资格结果引入未估计候选组",
                )
            major_codes = [major.major_code for major in group.majors]
            if (
                not major_codes
                or any(not code for code in major_codes)
                or len(major_codes) != len(set(major_codes))
                or set(major_codes) != source_major_codes[group.group_key]
            ):
                raise RunLifecycleError(
                    "VALIDATION_FAILED",
                    "资格结果专业身份与估计候选不一致",
                )
            if any(
                major.status
                not in {
                    algo_pb2.MajorEligibilityStatus
                    .MAJOR_ELIGIBILITY_STATUS_PASS,
                    algo_pb2.MajorEligibilityStatus
                    .MAJOR_ELIGIBILITY_STATUS_FAIL,
                    algo_pb2.MajorEligibilityStatus
                    .MAJOR_ELIGIBILITY_STATUS_UNKNOWN,
                    algo_pb2.MajorEligibilityStatus
                    .MAJOR_ELIGIBILITY_STATUS_NOT_APPLICABLE,
                    algo_pb2.MajorEligibilityStatus
                    .MAJOR_ELIGIBILITY_STATUS_MISSING_REQUIRED_FACT,
                }
                for major in group.majors
            ):
                raise RunLifecycleError(
                    "VALIDATION_FAILED",
                    "资格结果包含未定义专业状态",
                )
            result_by_key[group.group_key] = group
        if set(result_by_key) != source_keys:
            raise RunLifecycleError(
                "VALIDATION_FAILED",
                "资格结果遗漏估计候选组",
            )

        pass_status = algo_pb2.MajorEligibilityStatus.MAJOR_ELIGIBILITY_STATUS_PASS
        unknown_major_statuses = {
            algo_pb2.MajorEligibilityStatus.MAJOR_ELIGIBILITY_STATUS_UNKNOWN,
            algo_pb2.MajorEligibilityStatus
            .MAJOR_ELIGIBILITY_STATUS_MISSING_REQUIRED_FACT,
        }
        group_pass = algo_pb2.GroupEligibilityStatus.GROUP_ELIGIBILITY_STATUS_PASS
        group_partial = (
            algo_pb2.GroupEligibilityStatus
            .GROUP_ELIGIBILITY_STATUS_PASS_PARTIAL
        )
        group_fail = algo_pb2.GroupEligibilityStatus.GROUP_ELIGIBILITY_STATUS_FAIL
        group_unknown = (
            algo_pb2.GroupEligibilityStatus
            .GROUP_ELIGIBILITY_STATUS_UNKNOWN
        )
        eligible_keys = []
        restricted_keys = []
        unknown_keys = []
        failed_keys = []
        has_unknown_or_missing = False
        for key, group in result_by_key.items():
            major_statuses = [major.status for major in group.majors]
            group_has_unknown = any(
                status in unknown_major_statuses for status in major_statuses
            )
            has_unknown_or_missing = has_unknown_or_missing or group_has_unknown
            if group.status == group_pass:
                if any(status != pass_status for status in major_statuses):
                    raise RunLifecycleError(
                        "VALIDATION_FAILED",
                        "PASS 组包含非 PASS 专业",
                    )
                eligible_keys.append(key)
            elif group.status == group_partial:
                if (
                    pass_status not in major_statuses
                    or group_has_unknown
                    or any(
                        status
                        not in {
                            pass_status,
                            algo_pb2.MajorEligibilityStatus
                            .MAJOR_ELIGIBILITY_STATUS_FAIL,
                            algo_pb2.MajorEligibilityStatus
                            .MAJOR_ELIGIBILITY_STATUS_NOT_APPLICABLE,
                        }
                        for status in major_statuses
                    )
                ):
                    raise RunLifecycleError(
                        "VALIDATION_FAILED",
                        "PASS_PARTIAL 组状态不合法",
                    )
                eligible_keys.append(key)
                restricted_keys.append(key)
            elif group.status == group_fail:
                if pass_status in major_statuses or group_has_unknown:
                    raise RunLifecycleError(
                        "VALIDATION_FAILED",
                        "FAIL 组状态不合法",
                    )
                failed_keys.append(key)
            elif group.status == group_unknown:
                unknown_keys.append(key)
            else:
                raise RunLifecycleError(
                    "VALIDATION_FAILED",
                    "资格结果包含未定义组状态",
                )

        if status == complete and (
            unknown_keys or has_unknown_or_missing
        ):
            raise RunLifecycleError(
                "VALIDATION_FAILED",
                "COMPLETE 资格结果仍包含 UNKNOWN",
            )
        if status == complete and eligibility.diagnostics.unknown_count != 0:
            raise RunLifecycleError(
                "VALIDATION_FAILED",
                "COMPLETE 资格诊断仍包含 UNKNOWN",
            )
        if status == partial and eligibility.diagnostics.unknown_count == 0:
            raise RunLifecycleError(
                "VALIDATION_FAILED",
                "PARTIAL 资格结果缺少 UNKNOWN 诊断",
            )
        coverage_full = bool(eligibility.coverage) and all(
            declaration.status
            == algo_pb2.RuleCoverageStatus.RULE_COVERAGE_STATUS_FULL
            for declaration in eligibility.coverage
        )
        if not eligible_keys:
            raise RunLifecycleError(
                "NO_RELIABLE_DATA",
                "没有通过资格的候选组",
            )
        return {
            "formal_eligibility_ready": status == complete and coverage_full,
            "eligible_group_keys": sorted(eligible_keys),
            "restricted_group_keys": sorted(restricted_keys),
            "unknown_group_keys": sorted(unknown_keys),
            "failed_group_keys": sorted(failed_keys),
        }

    @staticmethod
    def _algorithm_preview_payload(
        *,
        result_version: UUID,
        row: dict[str, Any],
        rank,
        estimate,
        eligibility,
        eligibility_gate,
        eligible_candidates,
        optimized,
        allocation_warning,
    ) -> dict[str, Any]:
        message_options = {
            "preserving_proto_field_name": True,
            "use_integers_for_enums": True,
        }
        verification_summary = [
            {
                "key": "confirmed_profile",
                "status": "CHECKED",
                "detail": str(row["confirmed_profile_version"]),
            },
            {
                "key": "release_binding",
                "status": "CHECKED",
                "detail": row["release_id"],
            },
            {
                "key": "rank_audit_basis",
                "status": "CHECKED",
                "detail": f"{rank.rank}/{estimate.basis.user_rank}",
            },
            {
                "key": "candidate_identity_and_probability",
                "status": "CHECKED",
                "detail": "候选与概率由 svc-algo 原样返回",
            },
            {
                "key": "eligibility",
                "status": "CHECKED",
                "detail": (
                    "OptimizeWishlist 仅接收 EvaluateEligibility "
                    "PASS / PASS_PARTIAL 候选组"
                ),
            },
        ]
        unverified_conditions = [
            {
                "code": "CURRENT_YEAR_PLAN_IDENTITY",
                "message": "尚未完整核验当年招生计划身份与组成员变化",
            },
            {
                "code": "ONLINE_RAG_EVIDENCE",
                "message": "未执行在线政策检索与文档证据校验",
            },
            {
                "code": "THREE_CLASS_EVIDENCE_VERIFICATION",
                "message": "未执行三类证据事实校验",
            },
            {
                "code": "LLM_GATEWAY",
                "message": "未接入真实 LLM Gateway",
            },
            {"code": "STRATEGY", "message": "未启用 Strategy"},
            {"code": "CRITIC", "message": "未启用 Critic"},
            {
                "code": "FORMAL_REPORT",
                "message": "未生成或验证正式报告",
            },
        ]
        return {
            "result_version": str(result_version),
            "run_id": str(row["run_id"]),
            "release_id": row["release_id"],
            "confirmed_profile_version": str(row["confirmed_profile_version"]),
            "rank": rank.rank,
            "percentile": rank.percentile,
            "rush_candidates": [
                MessageToDict(item, **message_options)
                for item in estimate.rush_candidates
            ],
            "stable_candidates": [
                MessageToDict(item, **message_options)
                for item in estimate.stable_candidates
            ],
            "safe_candidates": [
                MessageToDict(item, **message_options)
                for item in estimate.safe_candidates
            ],
            "rush_total": estimate.rush_total,
            "stable_total": estimate.stable_total,
            "safe_total": estimate.safe_total,
            "audit_basis": MessageToDict(estimate.basis, **message_options),
            "eligibility": MessageToDict(eligibility, **message_options),
            "eligibility_gate": eligibility_gate,
            "kept_candidates": [
                MessageToDict(item, **message_options)
                for item in eligible_candidates
            ],
            "rejected_candidates": [],
            "wishlist": MessageToDict(optimized.wishlist, **message_options),
            "algorithm_method": estimate.basis.method,
            "algorithm_config_version": row["release_id"],
            "verification_summary": verification_summary,
            "unverified_conditions": unverified_conditions,
            "allocation_warning": allocation_warning,
            "risk_notice": (
                "本结果为内部受控算法预览，仅用于验证确定性算法闭环与可审计输出。"
            ),
            "disclaimer": (
                "结果仅供决策参考，不构成录取保证；不得作为正式志愿方案。"
            ),
            "internal_controlled_only": True,
            "formal_recommendation": False,
            "result_kind": "ALGORITHM_PREVIEW",
        }

    async def _publish_algorithm_preview(
        self,
        conn,
        *,
        row: dict[str, Any],
        preview: dict[str, Any],
    ) -> None:
        result_version = UUID(preview["result_version"])
        now = self._clock()
        async with conn.transaction():
            current = await (
                await conn.execute(
                    """
                    SELECT state, state_version, lease_owner, lease_epoch,
                           release_id, confirmed_profile_version,
                           active_execution_remaining_ms,
                           active_execution_started_at, cancel_requested_at
                    FROM run
                    WHERE run_id = %s
                    FOR UPDATE
                    """,
                    (row["run_id"],),
                )
            ).fetchone()
            if (
                current is None
                or current["state"] != "RUNNING"
                or current["lease_owner"] != self._worker_owner
                or current["lease_epoch"] != row["lease_epoch"]
                or current["confirmed_profile_version"]
                != row["confirmed_profile_version"]
            ):
                raise RunLifecycleError("RUN_STATE_CONFLICT")
            if current["cancel_requested_at"] is not None:
                raise RunLifecycleError("RUN_STATE_CONFLICT")
            release = await (
                await conn.execute(
                    """
                    SELECT status, revoked_at
                    FROM release
                    WHERE release_id = %s
                    FOR SHARE
                    """,
                    (current["release_id"],),
                )
            ).fetchone()
            if (
                release is None
                or release["status"] == "REVOKED"
                or release["revoked_at"] is not None
            ):
                raise RunLifecycleError("RELEASE_REVOKED")

            remaining = current["active_execution_remaining_ms"]
            if current["active_execution_started_at"] is not None:
                remaining = max(
                    0,
                    remaining
                    - int(
                        (
                            now - current["active_execution_started_at"]
                        ).total_seconds()
                        * 1000
                    ),
                )
            if remaining <= 0:
                raise RunLifecycleError(
                    "EXECUTION_TIMEOUT",
                    "主动执行额度在发布前耗尽",
                )
            next_version = current["state_version"] + 1
            await conn.execute(
                """
                INSERT INTO run_result (
                    result_version, run_id, release_id,
                    confirmed_profile_version, result_kind, payload,
                    verification_summary, unverified_conditions
                )
                VALUES (%s, %s, %s, %s, 'ALGORITHM_PREVIEW', %s, %s, %s)
                """,
                (
                    result_version,
                    row["run_id"],
                    current["release_id"],
                    current["confirmed_profile_version"],
                    Jsonb(preview),
                    Jsonb(preview["verification_summary"]),
                    Jsonb(preview["unverified_conditions"]),
                ),
            )
            updated = await (
                await conn.execute(
                    """
                    UPDATE run
                    SET state = 'SUCCEEDED',
                        stage = 'PREVIEW_READY',
                        terminal_reason = NULL,
                        state_version = %s,
                        result_version = %s,
                        result_kind = 'ALGORITHM_PREVIEW',
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        active_execution_remaining_ms = %s,
                        active_execution_started_at = NULL,
                        updated_at = NOW()
                    WHERE run_id = %s
                      AND state = 'RUNNING'
                      AND state_version = %s
                      AND lease_owner = %s
                      AND lease_epoch = %s
                    RETURNING run_id
                    """,
                    (
                        next_version,
                        result_version,
                        remaining,
                        row["run_id"],
                        current["state_version"],
                        self._worker_owner,
                        row["lease_epoch"],
                    ),
                )
            ).fetchone()
            if updated is None:
                raise RunLifecycleError("RUN_STATE_CONFLICT")
            await append_event(
                conn,
                run_id=row["run_id"],
                event_type="STATE_CHANGED",
                stage="PREVIEW_READY",
                state_version=next_version,
                payload={"from_state": "RUNNING", "to_state": "SUCCEEDED"},
            )
            await append_event(
                conn,
                run_id=row["run_id"],
                event_type="RESULT",
                stage="PREVIEW_READY",
                state_version=next_version,
                payload={
                    "result_version": str(result_version),
                    "result_kind": "ALGORITHM_PREVIEW",
                    "preview": preview,
                },
                payload_version=2,
            )

    async def _process_profile_run(self, run_id: UUID) -> None:
        await self._ensure_profile_runtime()
        conn = await self._connect(autocommit=True)
        try:
            row = await self._claim_run(conn, run_id)
            if row is None:
                return
            await self._after_claim_hook(run_id, row["lease_epoch"])
            if await self._stop_if_cancelled(conn, row):
                return
            row = await self._consume_active_budget(conn, row)
            if row is None:
                return
            if row["task_kind"] == "POLICY_QA":
                await self._process_policy_qa(conn, row)
                return
            if row["task_kind"] == "EXPLAIN_RESULT":
                try:
                    await self._process_explain_result(conn, row)
                except RunLifecycleError as exc:
                    if exc.reason == "RUN_STATE_CONFLICT":
                        if await self._stop_if_cancelled(conn, row):
                            return
                    await self._park_claim(
                        conn,
                        row,
                        target_state="FAILED",
                        stage="EXPLAINING",
                        terminal_reason=exc.reason,
                        error_message=str(exc),
                    )
                return
            if row["confirmed_profile_version"] is not None:
                try:
                    await self._process_algorithm_preview(conn, row)
                except RunLifecycleError as exc:
                    if exc.reason == "RUN_STATE_CONFLICT":
                        if await self._stop_if_cancelled(conn, row):
                            return
                    if exc.reason not in {
                        "VALIDATION_FAILED",
                        "DEPENDENCY_UNAVAILABLE",
                        "PROFILE_NOT_CONFIRMED",
                        "RELEASE_REVOKED",
                        "NO_RELIABLE_DATA",
                        "TARGET_ALLOCATION_UNSATISFIED",
                        "BUDGET_EXHAUSTED",
                        "EXECUTION_TIMEOUT",
                        "CONSENT_REQUIRED",
                        "CONSENT_REVOKED",
                        "SENSITIVE_DATA_NOT_ALLOWED",
                    } | ALGO_BUSINESS_ERROR_REASONS:
                        raise
                    await self._park_claim(
                        conn,
                        row,
                        target_state="FAILED",
                        stage=row["stage"],
                        terminal_reason=exc.reason,
                        error_message=str(exc),
                    )
                return

            if await self._apply_consumed_interrupt(conn, row):
                return

            if row["working_profile_version"] is None:
                input_data = dict(row["task_input"])
                input_data["subjects"] = tuple(input_data.get("subjects") or ())
                profile_input = StructuredProfileInput(**input_data)
                fields = build_profile_fields(profile_input)
                async with conn.transaction():
                    revision = await self._profile.CreateDraft(
                        conn,
                        session_id=row["session_id"],
                        run_id=row["run_id"],
                        fields=fields,
                    )
                    next_version = row["state_version"] + 1
                    revision_owner = await (
                        await conn.execute(
                        """
                        UPDATE run
                        SET working_profile_version = %s,
                            state_version = %s,
                            updated_at = NOW()
                        WHERE run_id = %s
                          AND state = 'RUNNING'
                          AND state_version = %s
                          AND lease_owner = %s
                          AND lease_epoch = %s
                        RETURNING run_id
                        """,
                        (
                            revision["profile_version"],
                            next_version,
                            run_id,
                            row["state_version"],
                            self._worker_owner,
                            row["lease_epoch"],
                        ),
                        )
                    ).fetchone()
                    if revision_owner is None:
                        raise RunLifecycleError("RUN_STATE_CONFLICT")
                    next_seq = await self._next_event_seq(conn, run_id)
                    await conn.execute(
                        """
                        INSERT INTO run_event (
                            run_id, event_seq, event_type, stage, state_version,
                            payload_version, payload
                        )
                        VALUES (%s, %s, 'PROGRESS', %s, %s, 1, %s)
                        """,
                        (
                            run_id,
                            next_seq,
                            "INTAKE",
                            next_version,
                            Jsonb(
                                {
                                    "profile_version": str(revision["profile_version"]),
                                    "readiness": "VALIDATING",
                                }
                            ),
                        ),
                    )
                row["state_version"] = next_version
                row["working_profile_version"] = revision["profile_version"]
                validation = await self._profile.Validate(
                    conn,
                    release_id=row["release_id"],
                    fields=fields,
                )
            else:
                revision_row = await (
                    await conn.execute(
                        """
                        SELECT profile_version, fields, lifecycle
                        FROM profile_revision
                        WHERE profile_version = %s
                        """,
                        (row["working_profile_version"],),
                    )
                ).fetchone()
                if revision_row is None:
                    raise RunLifecycleError("INTERNAL_ERROR", "working profile revision 不存在")
                validation = await self._profile.Validate(
                    conn,
                    release_id=row["release_id"],
                    fields=revision_row["fields"],
                )
                revision = revision_row

            macro_region = await self._unresolved_run_macro_region(
                conn,
                run_id=run_id,
                fields=validation.fields,
            )
            if macro_region is not None:
                expression, catalog = macro_region
                clarification = clarification_payload(
                    clarification_id=str(uuid4()),
                    expression=expression,
                    catalog=catalog,
                )
                kind = "INTAKE_ASK"
                prompt = {
                    "kind": kind,
                    "question_key": "profile.region.select_provinces",
                    "profile_version": str(revision["profile_version"]),
                    "fields": redact_sensitive_profile_fields(
                        validation.fields
                    ),
                    "findings": list(validation.findings),
                    "clarification_kind": "SELECT_PROVINCES",
                    "reason_code": MACRO_REGION_SCOPE_UNRESOLVED,
                    "region_clarification": clarification,
                }
            else:
                kind = (
                    "PROFILE_CONFIRM"
                    if validation.readiness == "READY_FOR_CONFIRMATION"
                    else "INTAKE_ASK"
                )
                prompt = {
                    "kind": kind,
                    "question_key": (
                        "profile.confirm"
                        if kind == "PROFILE_CONFIRM"
                        else "profile.input.required"
                    ),
                    "profile_version": str(revision["profile_version"]),
                    "fields": redact_sensitive_profile_fields(
                        validation.fields
                    ),
                    "findings": list(validation.findings),
                }
            config = {"configurable": {"thread_id": str(run_id)}}
            snapshot = await self._profile_graph.aget_state(config)
            pending_interrupt = self._pending_interrupt(snapshot)
            if pending_interrupt is None:
                await self._profile_graph.ainvoke(
                    {"run_id": str(run_id), "prompt": prompt},
                    config,
                )
                snapshot = await self._profile_graph.aget_state(config)
                pending_interrupt = self._pending_interrupt(snapshot)
            if pending_interrupt is None:
                raise RunLifecycleError("INTERNAL_ERROR", "官方 saver 未产生 interrupt")

            checkpoint = await self._saver.aget_tuple(config)
            if checkpoint is None or checkpoint.checkpoint.get("id") is None:
                raise RunLifecycleError("INTERNAL_ERROR", "interrupt 缺少 durable checkpoint")
            checkpoint_id = checkpoint.checkpoint["id"]
            self._checkpoint_persisted_hook(run_id)
            interrupt_id = uuid4()
            prompt_time = self._clock()
            expires_at = prompt_time + timedelta(minutes=30)
            async with conn.transaction():
                current = await (
                    await conn.execute(
                        """
                        SELECT state, state_version, lease_owner, lease_epoch,
                               active_execution_remaining_ms,
                               active_execution_started_at
                        FROM run
                        WHERE run_id = %s
                        FOR UPDATE
                        """,
                        (run_id,),
                    )
                ).fetchone()
                if (
                    current is None
                    or current["state"] != "RUNNING"
                    or current["lease_owner"] != self._worker_owner
                    or current["lease_epoch"] != row["lease_epoch"]
                ):
                    return
                await conn.execute(
                    """
                    INSERT INTO run_interrupt (
                        interrupt_id, run_id, kind, state, question_key,
                        payload_version, payload, answer_schema_version,
                        checkpoint_id, expires_at
                    )
                    VALUES (%s, %s, %s, 'OPEN', %s, 1, %s, 1, %s, %s)
                    """,
                    (
                        interrupt_id,
                        run_id,
                        kind,
                        prompt["question_key"],
                        Jsonb(prompt),
                        checkpoint_id,
                        expires_at,
                    ),
                )
                next_version = current["state_version"] + 1
                now = self._clock()
                remaining = current["active_execution_remaining_ms"]
                started_at = current["active_execution_started_at"]
                if started_at is not None:
                    remaining = max(
                        0,
                        remaining - int((now - started_at).total_seconds() * 1000),
                    )
                parked = await (
                    await conn.execute(
                        """
                        UPDATE run
                        SET state = 'WAITING_HITL',
                            stage = %s,
                            state_version = %s,
                            lease_owner = NULL,
                            lease_expires_at = NULL,
                            active_execution_remaining_ms = %s,
                            active_execution_started_at = NULL,
                            updated_at = NOW()
                        WHERE run_id = %s
                          AND state = 'RUNNING'
                          AND state_version = %s
                          AND lease_owner = %s
                          AND lease_epoch = %s
                        RETURNING run_id
                        """,
                        (
                            kind,
                            next_version,
                            remaining,
                            run_id,
                            current["state_version"],
                            self._worker_owner,
                            row["lease_epoch"],
                        ),
                    )
                ).fetchone()
                if parked is None:
                    raise RunLifecycleError("RUN_STATE_CONFLICT")
                next_seq = await self._next_event_seq(conn, run_id)
                await conn.execute(
                    """
                    INSERT INTO run_event (
                        run_id, event_seq, event_type, stage, state_version,
                        payload_version, payload
                    )
                    VALUES (%s, %s, 'STATE_CHANGED', %s, %s, 1, %s)
                    """,
                    (
                        run_id,
                        next_seq,
                        kind,
                        next_version,
                        Jsonb({"from_state": "RUNNING", "to_state": "WAITING_HITL"}),
                    ),
                )
                next_seq += 1
                await conn.execute(
                    """
                    INSERT INTO run_event (
                        run_id, event_seq, event_type, stage, state_version,
                        payload_version, payload
                    )
                    VALUES (%s, %s, 'INTERRUPT', %s, %s, 1, %s)
                    """,
                    (
                        run_id,
                        next_seq,
                        kind,
                        next_version,
                        Jsonb(
                            {
                                "interrupt_id": str(interrupt_id),
                                "kind": kind,
                                "state": "OPEN",
                                "question_key": prompt["question_key"],
                                "payload_version": 1,
                                "payload": prompt,
                                "created_at": prompt_time.isoformat(),
                                "expires_at": expires_at.isoformat(),
                            }
                        ),
                    ),
                )
                if kind == "PROFILE_CONFIRM":
                    await self._store_messages(
                        conn,
                        session_id=row["session_id"],
                        run_id=run_id,
                        messages=(
                            ChatMessageInput(
                                client_message_id=(
                                    f"server:{run_id}:{interrupt_id}:prompt"
                                ),
                                role="ASSISTANT",
                                kind="PROGRESS",
                                content=(
                                    "画像已整理完成。请确认下面的信息，"
                                    "确认后我会继续核验招生要求并生成正式报告。"
                                ),
                            ),
                        ),
                    )
                elif prompt.get("clarification_kind") == "SELECT_PROVINCES":
                    term = str(
                        (prompt.get("region_clarification") or {}).get(
                            "term"
                        )
                        or "这个地域范围"
                    )
                    await self._store_messages(
                        conn,
                        session_id=row["session_id"],
                        run_id=run_id,
                        messages=(
                            ChatMessageInput(
                                client_message_id=(
                                    f"server:{run_id}:{interrupt_id}:region"
                                ),
                                role="ASSISTANT",
                                kind="TEXT",
                                content=self._region_question(term),
                                metadata={
                                    "clarification_kind": "SELECT_PROVINCES",
                                    "reason_code": MACRO_REGION_SCOPE_UNRESOLVED,
                                    "region_clarification": prompt[
                                        "region_clarification"
                                    ],
                                },
                            ),
                        ),
                    )
                await conn.execute(
                    "UPDATE chat_session SET updated_at = NOW() WHERE id = %s",
                    (row["session_id"],),
                )
        finally:
            await conn.close()

    async def _process_explain_result(
        self,
        conn,
        row: dict[str, Any],
    ) -> None:
        parent_result_version = row.get("parent_result_version")
        if parent_result_version is None:
            raise RunLifecycleError("PARENT_RESULT_REQUIRED")
        parent = await (
            await conn.execute(
                """
                SELECT rr.result_version, rr.run_id, rr.result_kind,
                       rr.payload, rr.confirmed_profile_version,
                       r.session_id, r.principal_id,
                       state.status AS result_state
                FROM run_result rr
                JOIN run r ON r.run_id = rr.run_id
                LEFT JOIN LATERAL (
                    SELECT status
                    FROM run_result_state_event event
                    WHERE event.result_version = rr.result_version
                    ORDER BY event.event_id DESC
                    LIMIT 1
                ) state ON TRUE
                WHERE rr.result_version = %s
                """,
                (parent_result_version,),
            )
        ).fetchone()
        if parent is None:
            raise RunLifecycleError("PARENT_RESULT_NOT_FOUND")
        if (
            parent["session_id"] != row["session_id"]
            or parent["principal_id"] != row["principal_id"]
        ):
            raise RunLifecycleError("PARENT_RESULT_FORBIDDEN")
        if parent["result_kind"] != "V1_FORMAL":
            raise RunLifecycleError("PARENT_RESULT_INVALID")
        if parent["result_state"] not in (None, "ACTIVE"):
            raise RunLifecycleError("PARENT_RESULT_STALE")

        question_row = await (
            await conn.execute(
                """
                SELECT content
                FROM chat_message
                WHERE run_id = %s AND role = 'USER'
                ORDER BY created_at DESC, message_id DESC
                LIMIT 1
                """,
                (row["run_id"],),
            )
        ).fetchone()
        question = str(
            question_row["content"] if question_row is not None else ""
        ).strip()
        answer = self._deterministic_report_answer(
            question=question,
            payload=dict(parent["payload"] or {}),
        )
        result_version = uuid4()
        result_payload = {
            "result_version": str(result_version),
            "result_kind": "EXPLAIN_RESULT",
            "parent_result_version": str(parent_result_version),
            "answer": answer,
        }

        async with conn.transaction():
            current = await (
                await conn.execute(
                    """
                    SELECT state, state_version, lease_owner, lease_epoch,
                           cancel_requested_at
                    FROM run
                    WHERE run_id = %s
                    FOR UPDATE
                    """,
                    (row["run_id"],),
                )
            ).fetchone()
            if (
                current is None
                or current["state"] != "RUNNING"
                or current["lease_owner"] != self._worker_owner
                or current["lease_epoch"] != row["lease_epoch"]
                or current["cancel_requested_at"] is not None
            ):
                raise RunLifecycleError("RUN_STATE_CONFLICT")
            next_version = current["state_version"] + 1
            await conn.execute(
                """
                INSERT INTO run_result (
                    result_version, run_id, release_id,
                    confirmed_profile_version, result_kind, payload,
                    verification_summary, unverified_conditions
                )
                VALUES (%s, %s, %s, %s, 'EXPLAIN_RESULT', %s, %s, %s)
                """,
                (
                    result_version,
                    row["run_id"],
                    row["release_id"],
                    parent["confirmed_profile_version"],
                    Jsonb(result_payload),
                    Jsonb([]),
                    Jsonb([]),
                ),
            )
            updated = await (
                await conn.execute(
                    """
                    UPDATE run
                    SET state = 'SUCCEEDED',
                        stage = 'EXPLAIN_READY',
                        terminal_reason = NULL,
                        state_version = %s,
                        confirmed_profile_version = %s,
                        result_version = %s,
                        result_kind = 'EXPLAIN_RESULT',
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        active_execution_started_at = NULL,
                        updated_at = NOW()
                    WHERE run_id = %s
                      AND state = 'RUNNING'
                      AND state_version = %s
                      AND lease_owner = %s
                      AND lease_epoch = %s
                    RETURNING run_id
                    """,
                    (
                        next_version,
                        parent["confirmed_profile_version"],
                        result_version,
                        row["run_id"],
                        current["state_version"],
                        self._worker_owner,
                        row["lease_epoch"],
                    ),
                )
            ).fetchone()
            if updated is None:
                raise RunLifecycleError("RUN_STATE_CONFLICT")
            await append_event(
                conn,
                run_id=row["run_id"],
                event_type="STATE_CHANGED",
                stage="EXPLAIN_READY",
                state_version=next_version,
                payload={"from_state": "RUNNING", "to_state": "SUCCEEDED"},
            )
            await append_event(
                conn,
                run_id=row["run_id"],
                event_type="RESULT",
                stage="EXPLAIN_READY",
                state_version=next_version,
                payload={
                    "result_version": str(result_version),
                    "result_kind": "EXPLAIN_RESULT",
                    "explanation": result_payload,
                },
                payload_version=1,
            )
            await self._store_messages(
                conn,
                session_id=row["session_id"],
                run_id=row["run_id"],
                messages=(
                    ChatMessageInput(
                        client_message_id=f"server:{row['run_id']}:explain",
                        role="ASSISTANT",
                        kind="TEXT",
                        content=answer,
                    ),
                ),
            )
            await conn.execute(
                "UPDATE chat_session SET updated_at = NOW() WHERE id = %s",
                (row["session_id"],),
            )

    @staticmethod
    def _deterministic_report_answer(
        *,
        question: str,
        payload: dict[str, Any],
    ) -> str:
        report = payload.get("report") or {}
        sections = report.get("sections") or []
        candidates = [
            section for section in sections
            if str(section.get("section_key") or "").startswith("candidate:")
        ]
        matched = [
            section for section in candidates
            if any(
                token and token in str(section.get("title") or "")
                for token in re.findall(r"[\u4e00-\u9fff]{2,20}(?:大学|学院)", question)
            )
        ]
        chosen = matched[:2] if matched else candidates[:3]
        lines = [
            "这份方案先使用已确认的画像筛除不符合选科与资格规则的院校专业组，"
            "再由确定性算法估计录取概率、按志愿策略完成排序；模型不会改写候选身份、"
            "概率或顺序。"
        ]
        if chosen:
            lines.append("当前方案中可核对的关键说明：")
            for section in chosen:
                title = str(section.get("title") or "").strip()
                body = str(section.get("body") or "").strip()
                lines.append(f"{title}：{body}")
        risk_notice = str(payload.get("risk_notice") or "").strip()
        if risk_notice:
            lines.append(f"风险提示：{risk_notice}")
        return "\n".join(lines)

    @staticmethod
    def _pending_interrupt(snapshot):
        for task in snapshot.tasks:
            if task.interrupts:
                return task.interrupts[0]
        return None

    async def _apply_consumed_interrupt(self, conn, run) -> bool:
        interrupt = await (
            await conn.execute(
                """
                SELECT interrupt_id, kind, payload, answer_payload
                FROM run_interrupt
                WHERE run_id = %s
                  AND state = 'CONSUMED'
                  AND applied_at IS NULL
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (run["run_id"],),
            )
        ).fetchone()
        if interrupt is None:
            return False

        config = {"configurable": {"thread_id": str(run["run_id"])}}
        snapshot = await self._profile_graph.aget_state(config)
        pending = self._pending_interrupt(snapshot)
        if pending is not None:
            await self._profile_graph.ainvoke(
                Command(resume=interrupt["answer_payload"]),
                config,
            )
            snapshot = await self._profile_graph.aget_state(config)
            pending = self._pending_interrupt(snapshot)
            pending_value = getattr(pending, "value", None) if pending is not None else None
            if (
                pending is not None
                and not (
                    isinstance(pending_value, dict)
                    and pending_value.get("kind") == "INTERNAL_ALGORITHM_PREVIEW"
                )
            ):
                raise RunLifecycleError("INTERNAL_ERROR", "官方 saver 未消费 interrupt")

        answer = interrupt["answer_payload"]
        if answer.get("type") != "CONFIRM_PROFILE":
            await self._apply_profile_answer_and_create_revision(
                conn,
                run=run,
                interrupt=interrupt,
                answer=answer,
            )
            return False
        profile_version = UUID(answer["profile_version"])
        async with conn.transaction():
            run_row = await (
                await conn.execute(
                    """
                    SELECT run_id, session_id, state, stage, state_version,
                           release_id, working_profile_version, lease_owner,
                           lease_epoch
                    FROM run
                    WHERE run_id = %s
                    FOR UPDATE
                    """,
                    (run["run_id"],),
                )
            ).fetchone()
            if (
                run_row is None
                or run_row["state"] != "RUNNING"
                or run_row["lease_owner"] != self._worker_owner
                or run_row["lease_epoch"] != run["lease_epoch"]
            ):
                raise RunLifecycleError("RUN_STATE_CONFLICT")
            if run_row["working_profile_version"] != profile_version:
                raise RunLifecycleError("PROFILE_VERSION_CONFLICT")
            release = await (
                await conn.execute(
                    """
                    SELECT status, revoked_at
                    FROM release
                    WHERE release_id = %s
                    """,
                    (run_row["release_id"],),
                )
            ).fetchone()
            if (
                release is None
                or release["status"] == "REVOKED"
                or release["revoked_at"] is not None
            ):
                raise RunLifecycleError("RELEASE_REVOKED")
            revision = await (
                await conn.execute(
                    """
                    SELECT fields
                    FROM profile_revision
                    WHERE profile_version = %s AND lifecycle = 'DRAFT'
                    FOR UPDATE
                    """,
                    (profile_version,),
                )
            ).fetchone()
            if revision is None:
                raise RunLifecycleError("PROFILE_VERSION_CONFLICT")
            validation = await self._profile.Validate(
                conn,
                release_id=run_row["release_id"],
                fields=revision["fields"],
            )
            if validation.readiness != "READY_FOR_CONFIRMATION":
                raise RunLifecycleError("PROFILE_VERSION_CONFLICT")

            await self._profile.Confirm(
                conn,
                profile_version=profile_version,
                actor=run["principal_id"],
                session_id=run_row["session_id"],
            )
            next_version = run_row["state_version"] + 1
            await conn.execute(
                """
                UPDATE run
                SET state = 'QUEUED',
                    confirmed_profile_version = %s,
                    stage = 'PROFILE_CONFIRMED',
                    state_version = %s,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    active_execution_remaining_ms = %s,
                    active_execution_started_at = NULL,
                    updated_at = NOW()
                WHERE run_id = %s
                  AND state = 'RUNNING'
                  AND state_version = %s
                  AND lease_owner = %s
                  AND lease_epoch = %s
                """,
                (
                    profile_version,
                    next_version,
                    max(
                        0,
                        run["active_execution_remaining_ms"]
                        - int(
                            (
                                self._clock() - run["active_execution_started_at"]
                            ).total_seconds()
                            * 1000
                        ),
                    ),
                    run_row["run_id"],
                    run_row["state_version"],
                    self._worker_owner,
                    run["lease_epoch"],
                ),
            )
            next_seq = await self._next_event_seq(conn, run_row["run_id"])
            await conn.execute(
                """
                INSERT INTO run_event (
                    run_id, event_seq, event_type, stage, state_version,
                    payload_version, payload
                )
                VALUES (%s, %s, 'STATE_CHANGED', 'PROFILE_CONFIRMED', %s, 1, %s)
                """,
                (
                    run_row["run_id"],
                    next_seq,
                    next_version,
                    Jsonb(
                        {
                            "from_state": "RUNNING",
                            "to_state": "QUEUED",
                            "profile_version": str(profile_version),
                        }
                    ),
                ),
            )
            await conn.execute(
                """
                UPDATE run_interrupt
                SET applied_at = NOW()
                WHERE interrupt_id = %s
                """,
                (interrupt["interrupt_id"],),
            )
        return True

    @staticmethod
    def _resume_answer_payload(command: ResumeRunCommand) -> dict[str, Any]:
        answer = command.answer
        if isinstance(answer, ConfirmProfileAnswer):
            return {
                "type": "CONFIRM_PROFILE",
                "profile_version": (
                    str(command.profile_version) if command.profile_version else None
                ),
            }
        if isinstance(answer, AnswerFields):
            return {"type": "ANSWER_FIELDS", "fields": answer.fields}
        if isinstance(answer, RejectOrEditProfileAnswer):
            return {"type": "REJECT_OR_EDIT_PROFILE", "patch": answer.patch}
        if isinstance(answer, DeclineFieldsAnswer):
            return {"type": "DECLINE_FIELDS", "field_keys": list(answer.field_keys)}
        if isinstance(answer, RegionAnswer):
            return {
                "type": "SELECT_PROVINCES",
                "clarification_id": answer.clarification_id,
                "provinces": list(answer.provinces),
                "selected_preset_ids": list(answer.preset_ids),
                "region_catalog_version": answer.catalog_version,
                "cancel_region_restriction": (
                    answer.cancel_region_restriction
                ),
            }
        raise RunLifecycleError("RUN_STATE_CONFLICT")

    async def _apply_profile_answer_and_create_revision(
        self,
        conn,
        *,
        run,
        interrupt,
        answer: dict[str, Any],
    ) -> None:
        base_version = run["working_profile_version"]
        async with conn.transaction():
            current = await (
                await conn.execute(
                    """
                    SELECT run_id, session_id, working_profile_version, state_version,
                           state, lease_owner, lease_epoch
                    FROM run
                    WHERE run_id = %s
                    FOR UPDATE
                    """,
                    (run["run_id"],),
                )
            ).fetchone()
            if (
                current is None
                or current["working_profile_version"] != base_version
                or current["state"] != "RUNNING"
                or current["lease_owner"] != self._worker_owner
                or current["lease_epoch"] != run["lease_epoch"]
            ):
                raise RunLifecycleError("PROFILE_VERSION_CONFLICT")
            patch = {}
            if answer["type"] in {"ANSWER_FIELDS", "REJECT_OR_EDIT_PROFILE"}:
                patch = answer.get("fields") or answer.get("patch") or {}
            elif answer["type"] == "DECLINE_FIELDS":
                for key in answer["field_keys"]:
                    patch[key] = {
                        "state": "DECLINED",
                        "source": "USER_FORM",
                        "value": None,
                    }
            elif answer["type"] == "SELECT_PROVINCES":
                clarification = interrupt["payload"].get(
                    "region_clarification"
                ) or {}
                if (
                    answer.get("clarification_id")
                    != clarification.get("clarification_id")
                ):
                    raise RunLifecycleError("REGION_CLARIFICATION_CONFLICT")
                catalog = await self._region_catalog(run["release_id"])
                try:
                    provinces = validate_region_answer(
                        catalog=catalog,
                        catalog_version=str(
                            answer.get("region_catalog_version") or ""
                        ),
                        provinces=tuple(answer.get("provinces") or ()),
                        preset_ids=tuple(
                            answer.get("selected_preset_ids") or ()
                        ),
                        cancel_region_restriction=bool(
                            answer.get("cancel_region_restriction")
                        ),
                    )
                except MacroRegionError as exc:
                    raise RunLifecycleError(exc.reason) from exc
                if answer.get("cancel_region_restriction"):
                    patch = {}
                else:
                    current_revision = await (
                        await conn.execute(
                            """
                            SELECT fields
                            FROM profile_revision
                            WHERE profile_version = %s
                            """,
                            (base_version,),
                        )
                    ).fetchone()
                    if current_revision is None:
                        raise RunLifecycleError("PROFILE_VERSION_CONFLICT")
                    current_profile = self._profile_from_fields(
                        current_revision["fields"]
                    )
                    resolved = self._apply_resolved_region_to_profile(
                        current_profile,
                        operation=str(clarification.get("operation") or ""),
                        provinces=provinces,
                    )
                    patch = {
                        "preferences.required_provinces": {
                            "state": "PROVIDED",
                            "source": "USER_FORM",
                            "value": list(resolved.required_provinces),
                        },
                        "preferences.excluded_provinces": {
                            "state": "PROVIDED",
                            "source": "USER_FORM",
                            "value": list(resolved.excluded_provinces),
                        },
                        "preferences.preferred_cities": {
                            "state": "PROVIDED",
                            "source": "USER_FORM",
                            "value": list(resolved.preferred_cities),
                        },
                        "preferences.required_cities": {
                            "state": "PROVIDED",
                            "source": "USER_FORM",
                            "value": list(resolved.required_cities),
                        },
                    }
            else:
                raise RunLifecycleError("RUN_STATE_CONFLICT")
            patch = self._normalize_user_profile_patch(patch)

            new_revision = await self._profile.ApplyPatch(
                conn,
                session_id=current["session_id"],
                run_id=current["run_id"],
                base_profile_version=base_version,
                patch=patch,
                idempotency_key=answer.get("interrupt_id"),
            )
            next_version = current["state_version"] + 1
            updated = await (
                await conn.execute(
                """
                UPDATE run
                SET working_profile_version = %s,
                    stage = 'INTAKE',
                    state_version = %s,
                    updated_at = NOW()
                WHERE run_id = %s
                  AND state = 'RUNNING'
                  AND state_version = %s
                  AND lease_owner = %s
                  AND lease_epoch = %s
                RETURNING run_id
                """,
                (
                    new_revision["profile_version"],
                    next_version,
                    current["run_id"],
                    current["state_version"],
                    self._worker_owner,
                    run["lease_epoch"],
                ),
                )
            ).fetchone()
            if updated is None:
                raise RunLifecycleError("RUN_STATE_CONFLICT")
            next_seq = await self._next_event_seq(conn, current["run_id"])
            await conn.execute(
                """
                INSERT INTO run_event (
                    run_id, event_seq, event_type, stage, state_version,
                    payload_version, payload
                )
                VALUES (%s, %s, 'PROGRESS', 'INTAKE', %s, 1, %s)
                """,
                (
                    current["run_id"],
                    next_seq,
                    next_version,
                    Jsonb(
                        {
                            "profile_version": str(new_revision["profile_version"]),
                            "based_on": str(base_version),
                        }
                    ),
                ),
            )
            await conn.execute(
                "UPDATE run_interrupt SET applied_at = NOW() WHERE interrupt_id = %s",
                (interrupt["interrupt_id"],),
            )
        run["working_profile_version"] = new_revision["profile_version"]
        run["state_version"] = next_version
        run["stage"] = "INTAKE"

    @staticmethod
    def _normalize_user_profile_patch(
        patch: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        derived_fields = {"rank", "percentile", "batch_control_line"}
        normalized: dict[str, dict[str, Any]] = {}
        for key, field in patch.items():
            if key in derived_fields:
                raise RunLifecycleError("PROFILE_INVALID_VALUE")
            state = field.get("state")
            if state not in {"PROVIDED", "UNKNOWN", "DECLINED"}:
                raise RunLifecycleError("PROFILE_INVALID_VALUE")
            value = field.get("value")
            if state in {"UNKNOWN", "DECLINED"}:
                value = None
            elif value is None:
                raise RunLifecycleError("PROFILE_INVALID_VALUE")
            normalized[key] = {
                "state": state,
                "source": "USER_FORM",
                "value": value,
            }
        return normalized

    async def CancelRun(self, command: CancelRunCommand) -> RunReceipt:
        scope_key = f"run:{command.run_id}"
        request_hash = self._request_hash(
            {
                "run_id": str(command.run_id),
                "operation": "CANCEL_RUN",
            }
        )
        receipt_lock = f"{command.principal_id}:CANCEL_RUN:{scope_key}:{command.idempotency_key}"

        conn = await self._connect()
        try:
            async with conn.transaction():
                await self._raise_if_principal_unavailable(
                    conn,
                    command.principal_id,
                )
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (receipt_lock,),
                )
                replay = await self._find_receipt(
                    conn,
                    principal_id=command.principal_id,
                    operation="CANCEL_RUN",
                    scope_key=scope_key,
                    idempotency_key=command.idempotency_key,
                )
                if replay is not None:
                    if replay["request_hash"] != request_hash:
                        raise RunLifecycleError("IDEMPOTENCY_CONFLICT")
                    return self._receipt_from_response(replay["response"], replayed=True)

                row = await (
                    await conn.execute(
                        """
                        SELECT run_id, session_id, state, stage, state_version,
                               cancel_requested_at
                        FROM run
                        WHERE run_id = %s AND principal_id = %s
                        FOR UPDATE
                        """,
                        (command.run_id, command.principal_id),
                    )
                ).fetchone()
                if row is None:
                    raise RunLifecycleError("RUN_NOT_FOUND")

                if row["state"] in {"EXPIRED", "SUCCEEDED", "FAILED", "CANCELLED"}:
                    receipt = RunReceipt(
                        accepted=False,
                        reason="RUN_ALREADY_TERMINAL",
                        run_id=row["run_id"],
                        session_id=row["session_id"],
                        state=row["state"],
                        stage=row["stage"],
                        state_version=row["state_version"],
                        first_event_seq=0,
                    )
                    await self._save_receipt(
                        conn,
                        principal_id=command.principal_id,
                        operation="CANCEL_RUN",
                        scope_key=scope_key,
                        idempotency_key=command.idempotency_key,
                        request_hash=request_hash,
                        receipt=receipt,
                        target_run_id=command.run_id,
                    )
                    return receipt

                if row["state"] == "RUNNING":
                    next_version = row["state_version"] + 1
                    next_seq = await self._next_event_seq(conn, command.run_id)
                    requested_at = row["cancel_requested_at"] or self._clock()
                    updated = await (
                        await conn.execute(
                            """
                            UPDATE run
                            SET cancel_requested_at = COALESCE(cancel_requested_at, %s),
                                state_version = %s,
                                updated_at = NOW()
                            WHERE run_id = %s
                              AND principal_id = %s
                              AND state = 'RUNNING'
                              AND state_version = %s
                            RETURNING run_id, session_id, state, stage, state_version
                            """,
                            (
                                requested_at,
                                next_version,
                                command.run_id,
                                command.principal_id,
                                row["state_version"],
                            ),
                        )
                    ).fetchone()
                    if updated is None:
                        raise RunLifecycleError("RUN_STATE_CONFLICT")
                    await conn.execute(
                        """
                        INSERT INTO run_event (
                            run_id, event_seq, event_type, stage, state_version,
                            payload_version, payload
                        )
                        VALUES (%s, %s, 'PROGRESS', %s, %s, 1, %s)
                        """,
                        (
                            command.run_id,
                            next_seq,
                            updated["stage"],
                            updated["state_version"],
                            Jsonb(
                                {
                                    "cancel_requested_at": requested_at.isoformat(),
                                    "state": "RUNNING",
                                }
                            ),
                        ),
                    )
                    receipt = RunReceipt(
                        accepted=True,
                        reason=None,
                        run_id=updated["run_id"],
                        session_id=updated["session_id"],
                        state=updated["state"],
                        stage=updated["stage"],
                        state_version=updated["state_version"],
                        first_event_seq=next_seq,
                    )
                    await self._save_receipt(
                        conn,
                        principal_id=command.principal_id,
                        operation="CANCEL_RUN",
                        scope_key=scope_key,
                        idempotency_key=command.idempotency_key,
                        request_hash=request_hash,
                        receipt=receipt,
                        target_run_id=command.run_id,
                    )
                    return receipt

                if row["state"] == "WAITING_HITL":
                    next_version = row["state_version"] + 1
                    await conn.execute(
                        """
                        UPDATE run_interrupt
                        SET state = 'CANCELLED'
                        WHERE run_id = %s AND state = 'OPEN'
                        """,
                        (command.run_id,),
                    )
                    updated = await (
                        await conn.execute(
                            """
                            UPDATE run
                            SET state = 'CANCELLED',
                                terminal_reason = 'CANCELLED_BY_USER',
                                state_version = %s,
                                updated_at = NOW()
                            WHERE run_id = %s
                              AND principal_id = %s
                              AND state = 'WAITING_HITL'
                              AND state_version = %s
                            RETURNING run_id, session_id, state, stage, state_version
                            """,
                            (
                                next_version,
                                command.run_id,
                                command.principal_id,
                                row["state_version"],
                            ),
                        )
                    ).fetchone()
                    if updated is None:
                        raise RunLifecycleError("RUN_STATE_CONFLICT")
                    next_seq = await self._next_event_seq(conn, command.run_id)
                    await conn.execute(
                        """
                        INSERT INTO run_event (
                            run_id, event_seq, event_type, stage, state_version,
                            payload_version, payload
                        )
                        VALUES (%s, %s, 'STATE_CHANGED', %s, %s, 1, %s)
                        """,
                        (
                            command.run_id,
                            next_seq,
                            updated["stage"],
                            updated["state_version"],
                            Jsonb(
                                {
                                    "from_state": "WAITING_HITL",
                                    "to_state": "CANCELLED",
                                }
                            ),
                        ),
                    )
                    receipt = RunReceipt(
                        accepted=True,
                        reason=None,
                        run_id=updated["run_id"],
                        session_id=updated["session_id"],
                        state=updated["state"],
                        stage=updated["stage"],
                        state_version=updated["state_version"],
                        first_event_seq=next_seq,
                    )
                    await self._save_receipt(
                        conn,
                        principal_id=command.principal_id,
                        operation="CANCEL_RUN",
                        scope_key=scope_key,
                        idempotency_key=command.idempotency_key,
                        request_hash=request_hash,
                        receipt=receipt,
                        target_run_id=command.run_id,
                    )
                    return receipt

                if row["state"] != "QUEUED":
                    raise RunLifecycleError("RUN_STATE_CONFLICT")

                next_version = row["state_version"] + 1
                updated = await (
                    await conn.execute(
                        """
                        UPDATE run
                        SET state = 'CANCELLED',
                            terminal_reason = 'CANCELLED_BY_USER',
                            state_version = %s,
                            updated_at = NOW()
                        WHERE run_id = %s
                          AND principal_id = %s
                          AND state = 'QUEUED'
                          AND state_version = %s
                        RETURNING run_id, session_id, state, stage, state_version
                        """,
                        (
                            next_version,
                            command.run_id,
                            command.principal_id,
                            row["state_version"],
                        ),
                    )
                ).fetchone()
                if updated is None:
                    raise RunLifecycleError("RUN_STATE_CONFLICT")

                next_seq = await self._next_event_seq(conn, command.run_id)
                await conn.execute(
                    """
                    INSERT INTO run_event (
                        run_id, event_seq, event_type, stage, state_version,
                        payload_version, payload
                    )
                    VALUES (%s, %s, 'STATE_CHANGED', %s, %s, 1, %s)
                    """,
                    (
                        command.run_id,
                        next_seq,
                        updated["stage"],
                        updated["state_version"],
                        Jsonb({"from_state": "QUEUED", "to_state": "CANCELLED"}),
                    ),
                )
                receipt = RunReceipt(
                    accepted=True,
                    reason=None,
                    run_id=updated["run_id"],
                    session_id=updated["session_id"],
                    state=updated["state"],
                    stage=updated["stage"],
                    state_version=updated["state_version"],
                    first_event_seq=next_seq,
                )
                await self._save_receipt(
                    conn,
                    principal_id=command.principal_id,
                    operation="CANCEL_RUN",
                    scope_key=scope_key,
                    idempotency_key=command.idempotency_key,
                    request_hash=request_hash,
                    receipt=receipt,
                    target_run_id=command.run_id,
                )
                return receipt
        finally:
            await conn.close()

    async def WatchRun(
        self,
        *,
        principal_id: str,
        run_id: UUID,
        after_event_seq: int,
        poll_interval: float = 0.25,
    ) -> AsyncIterator[RunEvent]:
        conn = await self._connect(autocommit=True)
        try:
            await self._raise_if_principal_unavailable(conn, principal_id)
            owned = await (
                await conn.execute(
                    "SELECT 1 FROM run WHERE run_id = %s AND principal_id = %s",
                    (run_id, principal_id),
                )
            ).fetchone()
            if owned is None:
                raise RunLifecycleError("RUN_NOT_FOUND")

            expired = await (
                await conn.execute(
                    """
                    SELECT 1
                    FROM run_event
                    WHERE run_id = %s
                      AND event_seq > %s
                      AND occurred_at < NOW() - INTERVAL '7 days'
                    LIMIT 1
                    """,
                    (run_id, after_event_seq),
                )
            ).fetchone()
            if expired is not None:
                raise RunLifecycleError("EVENT_CURSOR_EXPIRED")

            cursor = after_event_seq
            while True:
                rows = await (
                    await conn.execute(
                        """
                        SELECT run_id, event_seq, event_type, stage, state_version,
                               occurred_at, payload_version, payload
                        FROM run_event
                        WHERE run_id = %s AND event_seq > %s
                        ORDER BY event_seq
                        """,
                        (run_id, cursor),
                    )
                ).fetchall()
                if rows:
                    for row in rows:
                        cursor = row["event_seq"]
                        yield RunEvent(
                            run_id=row["run_id"],
                            event_seq=row["event_seq"],
                            type=row["event_type"],
                            stage=row["stage"],
                            state_version=row["state_version"],
                            occurred_at=row["occurred_at"],
                            payload_version=row["payload_version"],
                            payload=row["payload"],
                        )
                    continue
                await asyncio.sleep(poll_interval)
        finally:
            await conn.close()

    async def _select_active_release(
        self,
        conn: psycopg.AsyncConnection,
        *,
        policy_input: PolicyQaInput | None = None,
    ) -> str:
        if policy_input is None:
            row = await (
                await conn.execute(
                    """
                    SELECT release_id
                    FROM release
                    WHERE status = 'PUBLISHED' AND revoked_at IS NULL
                    ORDER BY published_at DESC NULLS LAST,
                             created_at DESC, release_id DESC
                    LIMIT 1
                    FOR SHARE
                    """
                )
            ).fetchone()
        else:
            row = await (
                await conn.execute(
                    """
                    SELECT r.release_id
                    FROM release r
                    WHERE r.status = 'PUBLISHED'
                      AND r.revoked_at IS NULL
                      AND EXISTS (
                          SELECT 1
                          FROM release_scope s
                          WHERE s.release_id = r.release_id
                            AND s.province = %s
                            AND s.application_year = %s
                            AND (
                                %s = '不限'
                                OR s.category = %s
                            )
                            AND (
                                %s = '不限'
                                OR s.batch = %s
                            )
                      )
                    ORDER BY r.published_at DESC NULLS LAST,
                             r.created_at DESC, r.release_id DESC
                    LIMIT 1
                    FOR SHARE
                    """,
                    (
                        policy_input.province,
                        policy_input.application_year,
                        policy_input.category,
                        policy_input.category,
                        policy_input.batch,
                        policy_input.batch,
                    ),
                )
            ).fetchone()
        if row is None:
            raise RunLifecycleError("RELEASE_UNAVAILABLE")
        return row["release_id"]

    async def _resolve_session(self, conn: psycopg.AsyncConnection, command: SubmitRunCommand) -> UUID:
        if command.session_id is not None:
            row = await (
                await conn.execute(
                    """
                    SELECT id, principal_id
                    FROM chat_session
                    WHERE id = %s
                    FOR SHARE
                    """,
                    (command.session_id,),
                )
            ).fetchone()
            if row is None or row["principal_id"] != command.principal_id:
                raise RunLifecycleError("RUN_FORBIDDEN")
            return row["id"]

        session_id = uuid4()
        await conn.execute(
            """
            INSERT INTO chat_session (id, user_id, principal_id, status, current_node)
            VALUES (%s, %s, %s, 'active', 'INTAKE')
            """,
            (session_id, command.principal_id, command.principal_id),
        )
        return session_id

    async def _validate_parent_result(
        self,
        conn: psycopg.AsyncConnection,
        *,
        principal_id: str,
        session_id: UUID,
        parent_result_version: UUID,
    ) -> None:
        row = await (
            await conn.execute(
                """
                SELECT r.run_id, r.session_id, r.principal_id,
                       rr.result_kind,
                       state.status AS result_state
                FROM run r
                JOIN run_result rr ON rr.run_id = r.run_id
                LEFT JOIN LATERAL (
                    SELECT status
                    FROM run_result_state_event event
                    WHERE event.result_version = rr.result_version
                    ORDER BY event.event_id DESC
                    LIMIT 1
                ) state ON TRUE
                WHERE rr.result_version = %s
                """,
                (parent_result_version,),
            )
        ).fetchone()
        if row is None:
            raise RunLifecycleError("PARENT_RESULT_NOT_FOUND")
        if (
            row["principal_id"] != principal_id
            or row["session_id"] != session_id
        ):
            raise RunLifecycleError("PARENT_RESULT_FORBIDDEN")
        if row["result_kind"] != "V1_FORMAL":
            raise RunLifecycleError("PARENT_RESULT_INVALID")
        if row["result_state"] not in (None, "ACTIVE"):
            raise RunLifecycleError("PARENT_RESULT_STALE")

    async def _store_messages(
        self,
        conn: psycopg.AsyncConnection,
        *,
        session_id: UUID,
        run_id: UUID | None,
        messages: tuple[ChatMessageInput, ...],
    ) -> None:
        for message in messages:
            client_message_id = message.client_message_id.strip()
            role = message.role.strip().upper()
            kind = message.kind.strip().upper() or "TEXT"
            content = message.content.strip()
            if (
                not client_message_id
                or len(client_message_id) > 200
                or role not in {"USER", "ASSISTANT", "SYSTEM"}
                or kind not in {"TEXT", "PROGRESS", "RESULT", "ERROR"}
                or not content
                or len(content) > 8000
            ):
                raise RunLifecycleError("CHAT_MESSAGE_INVALID")
            await conn.execute(
                """
                INSERT INTO chat_message (
                    message_id, session_id, run_id, client_message_id,
                    role, kind, content, metadata
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (session_id, client_message_id) DO NOTHING
                """,
                (
                    uuid4(),
                    session_id,
                    run_id,
                    client_message_id,
                    role,
                    kind,
                    content,
                    Jsonb(message.metadata or {}),
                ),
            )

    async def _update_session_for_run(
        self,
        conn: psycopg.AsyncConnection,
        *,
        session_id: UUID,
        command: SubmitRunCommand,
        run_id: UUID,
    ) -> None:
        title = None
        if isinstance(command.input, StructuredProfileInput):
            category = command.input.category.strip()
            score = command.input.score
            if category and score is not None:
                title = f"{category} {score} 分志愿方案"
        await conn.execute(
            """
            UPDATE chat_session
            SET title = CASE
                    WHEN title IS NULL OR btrim(title) = ''
                    THEN COALESCE(%s, '新的志愿对话')
                    ELSE title
                END,
                current_node = 'INTAKE',
                updated_at = NOW()
            WHERE id = %s
            """,
            (title, session_id),
        )
        await conn.execute(
            "UPDATE chat_session SET updated_at = NOW() WHERE id = %s",
            (session_id,),
        )

    async def _find_receipt(
        self,
        conn: psycopg.AsyncConnection,
        *,
        principal_id: str,
        operation: str,
        scope_key: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        return await (
            await conn.execute(
                """
                SELECT request_hash, response
                FROM run_command_receipt
                WHERE principal_id = %s
                  AND operation = %s
                  AND scope_key = %s
                  AND idempotency_key = %s
                  AND expires_at > NOW()
                """,
                (principal_id, operation, scope_key, idempotency_key),
            )
        ).fetchone()

    async def _next_event_seq(self, conn: psycopg.AsyncConnection, run_id: UUID) -> int:
        row = await (
            await conn.execute(
                "SELECT COALESCE(MAX(event_seq), 0) + 1 AS next_seq FROM run_event WHERE run_id = %s",
                (run_id,),
            )
        ).fetchone()
        return row["next_seq"]

    async def _save_receipt(
        self,
        conn: psycopg.AsyncConnection,
        *,
        principal_id: str,
        operation: str,
        scope_key: str,
        idempotency_key: str,
        request_hash: str,
        receipt: RunReceipt,
        target_run_id: UUID,
    ) -> None:
        response = {
            "accepted": receipt.accepted,
            "reason": receipt.reason,
            "run_id": str(receipt.run_id) if receipt.run_id else None,
            "session_id": str(receipt.session_id),
            "state": receipt.state,
            "stage": receipt.stage,
            "state_version": receipt.state_version,
            "first_event_seq": receipt.first_event_seq,
            "replayed": False,
        }
        await conn.execute(
            """
            INSERT INTO run_command_receipt (
                receipt_id, principal_id, operation, scope_key, idempotency_key,
                request_hash, response, target_run_id, expires_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                uuid4(),
                principal_id,
                operation,
                scope_key,
                idempotency_key,
                request_hash,
                Jsonb(response),
                target_run_id,
                datetime.now(timezone.utc) + timedelta(days=7),
            ),
        )

    def _receipt_from_response(self, response: dict[str, Any], *, replayed: bool) -> RunReceipt:
        return RunReceipt(
            accepted=response["accepted"],
            reason=response["reason"],
            run_id=UUID(response["run_id"]) if response["run_id"] else None,
            session_id=UUID(response["session_id"]),
            state=response["state"],
            stage=response["stage"],
            state_version=response["state_version"],
            first_event_seq=response["first_event_seq"],
            replayed=replayed,
        )

    @staticmethod
    def _submit_request_payload(command: SubmitRunCommand) -> dict[str, Any]:
        return {
            "session_id": str(command.session_id) if command.session_id else None,
            "task_kind": command.task_kind,
            "input": asdict(command.input),
            "parent_result_version": (
                str(command.parent_result_version)
                if command.parent_result_version
                else None
            ),
            "messages": [asdict(message) for message in command.messages],
            "user_message": command.user_message,
        }

    @staticmethod
    def _request_hash(payload: dict[str, Any]) -> str:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
