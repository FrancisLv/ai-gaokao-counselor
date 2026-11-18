from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any, AsyncIterator
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.run_lifecycle.models import (
    CancelRunCommand,
    ResultReference,
    RunEvent,
    RunLifecycleError,
    RunReceipt,
    RunView,
    SubmitRunCommand,
)


class RunLifecycle:
    """调用方与测试共同使用的唯一持久任务 seam。"""

    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise ValueError("RunLifecycle 需要 PostgreSQL DSN")
        self._dsn = dsn

    async def _connect(self, *, autocommit: bool = False) -> psycopg.AsyncConnection:
        return await psycopg.AsyncConnection.connect(
            self._dsn,
            row_factory=dict_row,
            autocommit=autocommit,
        )

    async def SubmitRun(self, command: SubmitRunCommand) -> RunReceipt:
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
                    return self._receipt_from_response(replay["response"], replayed=True)

                release = await self._select_active_release(conn)
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
                return receipt
        finally:
            await conn.close()

    async def GetRun(self, *, principal_id: str, run_id: UUID) -> RunView:
        conn = await self._connect()
        try:
            row = await (
                await conn.execute(
                    """
                    SELECT run_id, session_id, task_kind, state, stage, terminal_reason,
                           state_version, release_id, confirmed_profile_version,
                           cancel_requested_at, result_version, result_kind,
                           created_at, updated_at
                    FROM run
                    WHERE run_id = %s AND principal_id = %s
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
                cancel_requested_at=row["cancel_requested_at"],
                current_interrupt=None,
                result=result,
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        finally:
            await conn.close()

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
                        SELECT run_id, session_id, state, stage, state_version
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

    async def _select_active_release(self, conn: psycopg.AsyncConnection) -> str:
        row = await (
            await conn.execute(
                """
                SELECT release_id
                FROM release
                WHERE status = 'PUBLISHED' AND revoked_at IS NULL
                ORDER BY published_at DESC NULLS LAST, created_at DESC, release_id DESC
                LIMIT 1
                FOR SHARE
                """
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
