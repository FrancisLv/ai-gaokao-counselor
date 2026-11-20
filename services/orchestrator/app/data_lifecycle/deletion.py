"""幂等 principal 数据删除；用户可见访问裁决仍由 RunLifecycle 读取面执行。"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Protocol
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.data_lifecycle.cleaner import (
    DELETION_RESOURCES,
    DeletionResourceContext,
    PostgresPrincipalDataCleaner,
    PrincipalDataCleaner,
)


class PrincipalDataDeletionError(Exception):
    def __init__(self, reason: str, message: str | None = None) -> None:
        self.reason = reason
        super().__init__(message or reason)


class DeletionConfirmationAuthority(Protocol):
    async def consume(
        self,
        *,
        principal_id: str,
        request_id: str,
        confirmation_token: str,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class DeletePrincipalDataCommand:
    principal_id: str
    request_id: str
    confirmation_token: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class DeletePrincipalDataReceipt:
    accepted: bool
    status: str
    reason: str | None
    request_id: str
    replayed: bool = False


class PrincipalDataDeletion:
    def __init__(
        self,
        dsn: str,
        *,
        confirmation_authority: DeletionConfirmationAuthority,
        cleaner: PrincipalDataCleaner | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not dsn:
            raise ValueError("PrincipalDataDeletion 需要 PostgreSQL DSN")
        self._dsn = dsn
        self._confirmation_authority = confirmation_authority
        self._cleaner = cleaner or PostgresPrincipalDataCleaner()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def DeletePrincipalData(
        self,
        command: DeletePrincipalDataCommand,
    ) -> DeletePrincipalDataReceipt:
        if not command.principal_id.strip() or not command.request_id.strip():
            raise ValueError("principal_id and request_id are required")
        principal_ref = hashlib.sha256(
            command.principal_id.encode("utf-8")
        ).hexdigest()
        request_hash = hashlib.sha256(
            json.dumps(
                {
                    "principal_ref": principal_ref,
                    "request_id": command.request_id,
                    "reason": command.reason,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        conn = await self._connect()
        try:
            existing = await self._find_receipt(
                conn,
                principal_ref=principal_ref,
                request_id=command.request_id,
            )
            if existing is not None:
                if existing["status"] == "DELETION_COMPLETED":
                    return self._replayed_receipt(existing)
                context = await self._retry_context(
                    conn,
                    command=command,
                    principal_ref=principal_ref,
                )
            else:
                confirmed = await self._confirmation_authority.consume(
                    principal_id=command.principal_id,
                    request_id=command.request_id,
                    confirmation_token=command.confirmation_token,
                )
                if not confirmed:
                    raise PrincipalDataDeletionError(
                        "DELETION_CONFIRMATION_INVALID"
                    )

                context = await self._accept_request(
                    conn,
                    command=command,
                    principal_ref=principal_ref,
                    request_hash=request_hash,
                )
                if context is None:
                    current = await self._find_receipt(
                        conn,
                        principal_ref=principal_ref,
                        request_id=command.request_id,
                    )
                    if current is not None and current["status"] == "DELETION_COMPLETED":
                        return self._replayed_receipt(current)
                    context = await self._retry_context(
                        conn,
                        command=command,
                        principal_ref=principal_ref,
                    )
            try:
                async with conn.transaction():
                    await conn.execute(
                        "SET LOCAL app.principal_deletion = 'on'"
                    )
                    await self._cleaner.cleanup(conn, context)
            except Exception as exc:
                failed = DeletePrincipalDataReceipt(
                    accepted=True,
                    status="DELETION_FAILED_RETRYABLE",
                    reason="DELETION_FAILED_RETRYABLE",
                    request_id=command.request_id,
                )
                await self._record_failure(
                    conn,
                    principal_ref=principal_ref,
                    request_id=command.request_id,
                    error_code=type(exc).__name__,
                )
                await self._finish_request(
                    conn,
                    principal_ref=principal_ref,
                    command=command,
                    receipt=failed,
                    completed=False,
                )
                return failed

            completed = DeletePrincipalDataReceipt(
                accepted=True,
                status="DELETION_COMPLETED",
                reason=None,
                request_id=command.request_id,
            )
            await self._finish_request(
                conn,
                principal_ref=principal_ref,
                command=command,
                receipt=completed,
                completed=True,
            )
            return completed
        finally:
            await conn.close()

    async def _connect(self) -> psycopg.AsyncConnection:
        return await psycopg.AsyncConnection.connect(
            self._dsn,
            row_factory=dict_row,
            autocommit=True,
        )

    async def _accept_request(
        self,
        conn: psycopg.AsyncConnection,
        *,
        command: DeletePrincipalDataCommand,
        principal_ref: str,
        request_hash: str,
    ) -> DeletionResourceContext | None:
        pending = DeletePrincipalDataReceipt(
            accepted=True,
            status="DELETION_PENDING",
            reason=None,
            request_id=command.request_id,
        )
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"principal-delete:{principal_ref}",),
            )
            existing = await self._find_receipt(
                conn,
                principal_ref=principal_ref,
                request_id=command.request_id,
            )
            if existing is not None:
                return None
            await conn.execute(
                """
                INSERT INTO principal_data_state (
                    principal_id, status, deletion_request_id, updated_at
                )
                VALUES (%s, 'DELETION_PENDING', %s, %s)
                ON CONFLICT (principal_id) DO UPDATE
                SET status = 'DELETION_PENDING',
                    deletion_request_id = EXCLUDED.deletion_request_id,
                    updated_at = EXCLUDED.updated_at
                """,
                (command.principal_id, command.request_id, self._clock()),
            )
            await conn.execute(
                """
                INSERT INTO principal_deletion_request (
                    principal_ref, request_id, request_hash, status, receipt
                )
                VALUES (%s, %s, %s, 'DELETION_PENDING', %s)
                """,
                (
                    principal_ref,
                    command.request_id,
                    request_hash,
                    Jsonb(asdict(pending)),
                ),
            )
            await conn.execute(
                """
                INSERT INTO principal_deletion_tombstone (
                    principal_ref, request_id, reason_code
                )
                VALUES (%s, %s, %s)
                ON CONFLICT (principal_ref) DO UPDATE
                SET request_id = EXCLUDED.request_id,
                    reason_code = EXCLUDED.reason_code,
                    completed_at = NULL
                """,
                (
                    principal_ref,
                    command.request_id,
                    "USER_REQUEST",
                ),
            )
            await conn.execute(
                """
                UPDATE run
                SET state = 'CANCELLED',
                    terminal_reason = 'PRINCIPAL_DELETED',
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    active_execution_started_at = NULL,
                    updated_at = NOW()
                WHERE principal_id = %s
                  AND state IN ('QUEUED', 'RUNNING', 'WAITING_HITL')
                """,
                (command.principal_id,),
            )
            session_rows = await (
                await conn.execute(
                    "SELECT id FROM chat_session WHERE principal_id = %s",
                    (command.principal_id,),
                )
            ).fetchall()
            run_rows = await (
                await conn.execute(
                    "SELECT run_id FROM run WHERE principal_id = %s",
                    (command.principal_id,),
                )
            ).fetchall()
            return DeletionResourceContext(
                principal_id=command.principal_id,
                principal_ref=principal_ref,
                request_id=command.request_id,
                session_ids=tuple(row["id"] for row in session_rows),
                run_ids=tuple(row["run_id"] for row in run_rows),
            )

    async def _retry_context(
        self,
        conn: psycopg.AsyncConnection,
        *,
        command: DeletePrincipalDataCommand,
        principal_ref: str,
    ) -> DeletionResourceContext:
        session_rows = await (
            await conn.execute(
                "SELECT id FROM chat_session WHERE principal_id = %s",
                (command.principal_id,),
            )
        ).fetchall()
        run_rows = await (
            await conn.execute(
                "SELECT run_id FROM run WHERE principal_id = %s",
                (command.principal_id,),
            )
        ).fetchall()
        return DeletionResourceContext(
            principal_id=command.principal_id,
            principal_ref=principal_ref,
            request_id=command.request_id,
            session_ids=tuple(row["id"] for row in session_rows),
            run_ids=tuple(row["run_id"] for row in run_rows),
        )

    @staticmethod
    async def _record_failure(
        conn: psycopg.AsyncConnection,
        *,
        principal_ref: str,
        request_id: str,
        error_code: str,
    ) -> None:
        await conn.execute(
            """
            INSERT INTO principal_deletion_ledger (
                principal_ref, request_id, resource_kind, result, error_code
            )
            VALUES (%s, %s, 'CLEANUP', 'FAILED', %s)
            """,
            (principal_ref, request_id, error_code),
        )

    @staticmethod
    async def _record_success_resources(
        conn: psycopg.AsyncConnection,
        *,
        principal_ref: str,
        request_id: str,
    ) -> None:
        for resource_kind in DELETION_RESOURCES:
            await conn.execute(
                """
                INSERT INTO principal_deletion_ledger (
                    principal_ref, request_id, resource_kind, result
                )
                VALUES (%s, %s, %s, 'SUCCEEDED')
                """,
                (principal_ref, request_id, resource_kind),
            )

    async def _finish_request(
        self,
        conn: psycopg.AsyncConnection,
        *,
        principal_ref: str,
        command: DeletePrincipalDataCommand,
        receipt: DeletePrincipalDataReceipt,
        completed: bool,
    ) -> None:
        now = self._clock()
        async with conn.transaction():
            if completed:
                await self._record_success_resources(
                    conn,
                    principal_ref=principal_ref,
                    request_id=command.request_id,
                )
            await conn.execute(
                """
                UPDATE principal_deletion_request
                SET status = %s,
                    receipt = %s,
                    updated_at = %s,
                    completed_at = CASE WHEN %s THEN %s ELSE NULL END
                WHERE principal_ref = %s AND request_id = %s
                """,
                (
                    receipt.status,
                    Jsonb(asdict(receipt)),
                    now,
                    completed,
                    now,
                    principal_ref,
                    command.request_id,
                ),
            )
            await conn.execute(
                """
                UPDATE principal_data_state
                SET status = %s, updated_at = %s
                WHERE principal_id = %s
                """,
                (
                    "DELETED" if completed else "DELETION_PENDING",
                    now,
                    command.principal_id,
                ),
            )
            if completed:
                await conn.execute(
                    """
                    UPDATE principal_deletion_tombstone
                    SET completed_at = %s
                    WHERE principal_ref = %s
                    """,
                    (now, principal_ref),
                )

    @staticmethod
    async def _find_receipt(
        conn: psycopg.AsyncConnection,
        *,
        principal_ref: str,
        request_id: str,
    ) -> dict[str, Any] | None:
        return await (
            await conn.execute(
                """
                SELECT status, receipt
                FROM principal_deletion_request
                WHERE principal_ref = %s AND request_id = %s
                """,
                (principal_ref, request_id),
            )
        ).fetchone()

    @staticmethod
    def _replayed_receipt(row: dict[str, Any]) -> DeletePrincipalDataReceipt:
        receipt = row["receipt"]
        return DeletePrincipalDataReceipt(
            accepted=bool(receipt["accepted"]),
            status=str(receipt["status"]),
            reason=receipt.get("reason"),
            request_id=str(receipt["request_id"]),
            replayed=True,
        )
