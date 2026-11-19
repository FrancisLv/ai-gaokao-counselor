from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from collections.abc import Callable
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
from app.run_lifecycle.algo import AlgoClient
from app.run_lifecycle.events import append_event
from app.run_lifecycle.models import (
    AlgorithmPreviewPayload,
    AnswerFields,
    ConfirmProfileAnswer,
    CancelRunCommand,
    DeclineFieldsAnswer,
    InterruptView,
    PolicyQaInput,
    RejectOrEditProfileAnswer,
    ResumeRunCommand,
    ResultReference,
    RunEvent,
    RunLifecycleError,
    RunReceipt,
    RunView,
    StructuredProfileInput,
    SubmitRunCommand,
)
from app.run_lifecycle.profile import ProfileRuntime, build_profile_fields
from app.run_lifecycle.profile_graph import build_profile_graph
from rag.policy_retrieval import (
    ApplicabilityScope,
    PolicyRetrievalError,
    PolicyRetrievalQuery,
    PolicyRetrievalResult,
)


class PolicyRetriever(Protocol):
    async def retrieve(
        self,
        query: PolicyRetrievalQuery,
    ) -> PolicyRetrievalResult: ...


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
        if command.task_kind == "GENERATE_PLAN" and not isinstance(
            command.input,
            StructuredProfileInput,
        ):
            raise RunLifecycleError(
                "PROFILE_UNSUPPORTED_INPUT",
                "GENERATE_PLAN requires StructuredProfileInput",
            )
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

        conn = await self._connect()
        try:
            async with conn.transaction():
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
                run_id = uuid4()
                await conn.execute(
                    """
                    INSERT INTO run (
                        run_id, session_id, principal_id, task_kind, task_input,
                        state, stage, state_version, release_id
                    )
                    VALUES (%s, %s, %s, %s, %s, 'QUEUED', 'INTAKE', 1, %s)
                    """,
                    (
                        run_id,
                        session_id,
                        command.principal_id,
                        command.task_kind,
                        Jsonb(asdict(command.input)),
                        release,
                    ),
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
            row = await (
                await conn.execute(
                    """
                    SELECT r.run_id, r.session_id, r.task_kind, r.state, r.stage,
                           r.terminal_reason, r.state_version, r.release_id,
                           r.confirmed_profile_version,
                           r.active_execution_remaining_ms, r.cancel_requested_at,
                           r.result_version, r.result_kind,
                           rr.payload AS result_payload,
                           r.created_at, r.updated_at, r.expires_at
                    FROM run r
                    LEFT JOIN run_result rr ON rr.result_version = r.result_version
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
            interrupt_row = await (
                await conn.execute(
                    """
                    SELECT interrupt_id, kind, state, question_key,
                           payload_version, payload, created_at, expires_at
                    FROM run_interrupt
                    WHERE run_id = %s AND state = 'OPEN'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (run_id,),
                )
            ).fetchone()
            current_interrupt = None
            if interrupt_row is not None:
                current_interrupt = InterruptView(
                    interrupt_id=interrupt_row["interrupt_id"],
                    kind=interrupt_row["kind"],
                    state=interrupt_row["state"],
                    question_key=interrupt_row["question_key"],
                    payload_version=interrupt_row["payload_version"],
                    payload=interrupt_row["payload"],
                    created_at=interrupt_row["created_at"],
                    expires_at=interrupt_row["expires_at"],
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
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                expires_at=row["expires_at"],
            )
        finally:
            await conn.close()

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
                           task_input, state, stage,
                           state_version, release_id, working_profile_version,
                           confirmed_profile_version, lease_owner, lease_epoch,
                           lease_expires_at, active_execution_remaining_ms,
                           active_execution_started_at
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
                              task_input, state, stage,
                              state_version, release_id, working_profile_version,
                              confirmed_profile_version, lease_owner, lease_epoch,
                              lease_expires_at, active_execution_remaining_ms,
                              active_execution_started_at
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
        await self._publish_algorithm_preview(
            conn,
            row=row,
            preview=preview,
        )

    async def _algorithm_preview_for_graph(
        self,
        run_id: str,
    ) -> AlgorithmPreviewPayload:
        conn = await self._connect(autocommit=True)
        try:
            row = await (
                await conn.execute(
                    """
                    SELECT run_id, session_id, principal_id, task_input, state, stage,
                           state_version, release_id, working_profile_version,
                           confirmed_profile_version, lease_owner, lease_epoch,
                           lease_expires_at, active_execution_remaining_ms,
                           active_execution_started_at
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
            checked = await self._algo_client.CheckConstraints(
                algo_pb2.CheckConstraintsRequest(
                    trace=trace,
                    profile=user_profile,
                    candidates=source_candidates,
                    subjects=algo_input["subjects"],
                    category=algo_input["category"],
                    release_id=release_id,
                )
            )
            if not checked.kept:
                raise RunLifecycleError(
                    "NO_RELIABLE_DATA",
                    "全部候选均被确定性约束过滤",
                )
            kept_by_identity: dict[tuple[str, str], float] = {}
            for item in checked.kept:
                identity = self._candidate_identity(item)
                if identity in kept_by_identity:
                    raise RunLifecycleError(
                        "VALIDATION_FAILED",
                        "CheckConstraints 返回重复候选",
                    )
                if (
                    identity not in source_by_identity
                    or source_by_identity[identity] != item.prob.probability
                ):
                    raise RunLifecycleError(
                        "VALIDATION_FAILED",
                        "约束过滤引入新候选或改写概率",
                    )
                kept_by_identity[identity] = item.prob.probability
            rejected_identities: set[tuple[str, str]] = set()
            for item in checked.rejected:
                identity = self._candidate_identity(item.candidate)
                if (
                    identity in rejected_identities
                    or identity in kept_by_identity
                    or identity not in source_by_identity
                    or source_by_identity[identity]
                    != item.candidate.prob.probability
                ):
                    raise RunLifecycleError(
                        "VALIDATION_FAILED",
                        "约束拒绝结果引入新候选、重复候选或改写概率",
                    )
                rejected_identities.add(identity)
            optimized = await self._algo_client.OptimizeWishlist(
                algo_pb2.OptimizeWishlistRequest(
                    trace=trace,
                    candidates=list(checked.kept),
                    profile=user_profile,
                    release_id=release_id,
                )
            )
        except grpc.RpcError as exc:
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
                or identity not in kept_by_identity
                or kept_by_identity[identity] != item.cwp.prob.probability
            ):
                raise RunLifecycleError(
                    "VALIDATION_FAILED",
                    "优化引入新候选、重复候选或改写概率",
                )
            optimized_identities.add(identity)

        result_version = uuid4()
        preview = self._algorithm_preview_payload(
            result_version=result_version,
            row=row,
            rank=rank,
            estimate=estimate,
            checked=checked,
            optimized=optimized,
        )
        return preview

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
    def _algorithm_preview_payload(
        *,
        result_version: UUID,
        row: dict[str, Any],
        rank,
        estimate,
        checked,
        optimized,
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
                "key": "subject_constraints",
                "status": "CHECKED",
                "detail": "OptimizeWishlist 仅接收 CheckConstraints kept 候选",
            },
        ]
        unverified_conditions = [
            {
                "code": "CURRENT_YEAR_PLAN_IDENTITY",
                "message": "尚未完整核验当年招生计划身份与组成员变化",
            },
            {
                "code": "FULL_ELIGIBILITY_RULES",
                "message": "尚未实现完整当年资格规则链",
            },
            {
                "code": "CONDITIONAL_FACTS",
                "message": "体检、单科、语种、性别与专项计划条件未完整核验",
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
            "kept_candidates": [
                MessageToDict(item, **message_options) for item in checked.kept
            ],
            "rejected_candidates": [
                MessageToDict(item, **message_options) for item in checked.rejected
            ],
            "wishlist": MessageToDict(optimized.wishlist, **message_options),
            "algorithm_method": estimate.basis.method,
            "algorithm_config_version": row["release_id"],
            "verification_summary": verification_summary,
            "unverified_conditions": unverified_conditions,
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
                        "EXECUTION_TIMEOUT",
                    }:
                        raise
                    await self._park_claim(
                        conn,
                        row,
                        target_state="FAILED",
                        stage=row["stage"],
                        terminal_reason=exc.reason,
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
                "fields": validation.fields,
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
        finally:
            await conn.close()

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
