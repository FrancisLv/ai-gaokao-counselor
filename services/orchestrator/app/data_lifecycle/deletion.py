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


@dataclass(frozen=True, slots=True)
class DeletionResourceContext:
    principal_id: str
    principal_ref: str
    request_id: str
    session_ids: tuple[UUID, ...]
    run_ids: tuple[UUID, ...]


class PrincipalDataCleaner(Protocol):
    async def cleanup(
        self,
        conn: psycopg.AsyncConnection,
        context: DeletionResourceContext,
    ) -> None: ...


DELETION_RESOURCES = (
    "CHECKPOINTS",
    "RESULT",
    "PROFILE",
    "RUN_DATA",
    "SESSION",
    "LLM_LEDGER",
)


class PostgresPrincipalDataCleaner:
    """在调用方事务连接上删除或去标识化 principal 私有资源。"""

    async def cleanup(
        self,
        conn: psycopg.AsyncConnection,
        context: DeletionResourceContext,
    ) -> None:
        deleted_principal_ref = f"deleted:{context.principal_ref}"
        session_ids = tuple(str(value) for value in context.session_ids)
        run_ids = tuple(str(value) for value in context.run_ids)

        await self._delete_official_checkpoints(conn, run_ids)
        await conn.execute(
            "DELETE FROM run_evidence_package WHERE run_id = ANY(%s::uuid[])",
            (list(run_ids),),
        )
        await conn.execute(
            "DELETE FROM run_verification_report WHERE run_id = ANY(%s::uuid[])",
            (list(run_ids),),
        )
        await conn.execute(
            """
            DELETE FROM run_result_state_event
            WHERE result_version IN (
                SELECT result_version
                FROM run_result
                WHERE run_id = ANY(%s::uuid[])
            )
            """,
            (list(run_ids),),
        )
        await conn.execute(
            "DELETE FROM run_formal_gate_artifact WHERE run_id = ANY(%s::uuid[])",
            (list(run_ids),),
        )
        await conn.execute(
            "DELETE FROM run_result WHERE run_id = ANY(%s::uuid[])",
            (list(run_ids),),
        )
        await conn.execute(
            """
            DELETE FROM profile_confirmation
            WHERE profile_version IN (
                SELECT profile_version
                FROM profile_revision
                WHERE session_id = ANY(%s::uuid[])
            )
            """,
            (list(session_ids),),
        )
        await conn.execute(
            "DELETE FROM profile_revision WHERE session_id = ANY(%s::uuid[])",
            (list(session_ids),),
        )
        await conn.execute(
            "DELETE FROM run_interrupt WHERE run_id = ANY(%s::uuid[])",
            (list(run_ids),),
        )
        await conn.execute(
            "DELETE FROM run_event WHERE run_id = ANY(%s::uuid[])",
            (list(run_ids),),
        )
        await conn.execute(
            "DELETE FROM run_command_receipt WHERE principal_id = %s",
            (context.principal_id,),
        )
        await conn.execute(
            "DELETE FROM plan_checkpoint WHERE session_id = ANY(%s::uuid[])",
            (list(session_ids),),
        )

        await self._deidentify_llm_ledger(
            conn,
            context=context,
            deleted_principal_ref=deleted_principal_ref,
        )
        await conn.execute(
            "UPDATE run SET task_input = '{}'::jsonb, "
            "parent_result_version = NULL, "
            "working_profile_version = NULL, "
            "confirmed_profile_version = NULL, "
            "result_version = NULL, result_kind = NULL, "
            "principal_id = %s WHERE run_id = ANY(%s::uuid[])",
            (deleted_principal_ref, list(run_ids)),
        )
        await conn.execute(
            "UPDATE chat_session SET principal_id = %s, user_id = NULL, "
            "profile = NULL, status = 'deleted', current_node = NULL "
            "WHERE id = ANY(%s::uuid[])",
            (deleted_principal_ref, list(session_ids)),
        )

    async def _delete_official_checkpoints(
        self,
        conn: psycopg.AsyncConnection,
        run_ids: tuple[str, ...],
    ) -> None:
        thread_ids = list(run_ids)
        for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            exists = await (
                await conn.execute(
                    "SELECT to_regclass(%s) IS NOT NULL AS present",
                    (f"public.{table}",),
                )
            ).fetchone()
            if not exists or not exists["present"]:
                continue
            await conn.execute(
                f"DELETE FROM {table} WHERE thread_id = ANY(%s::text[])",
                (thread_ids,),
            )

    @staticmethod
    async def _deidentify_llm_ledger(
        conn: psycopg.AsyncConnection,
        *,
        context: DeletionResourceContext,
        deleted_principal_ref: str,
    ) -> None:
        session_ids = list(context.session_ids)
        run_ids = list(context.run_ids)
        logical_exists = await (
            await conn.execute(
                "SELECT to_regclass('public.llm_logical_call') IS NOT NULL AS present"
            )
        ).fetchone()
        referenced_cache_keys: tuple[str, ...] = ()
        if logical_exists and logical_exists["present"]:
            cache_rows = await (
                await conn.execute(
                    """
                    SELECT DISTINCT cache_key_hash
                    FROM llm_logical_call
                    WHERE cache_key_hash IS NOT NULL
                      AND (
                          principal_id = %s
                          OR session_id = ANY(%s::uuid[])
                          OR run_id = ANY(%s::uuid[])
                      )
                    """,
                    (
                        context.principal_id,
                        session_ids,
                        run_ids,
                    ),
                )
            ).fetchall()
            referenced_cache_keys = tuple(
                row["cache_key_hash"] for row in cache_rows
            )
            await conn.execute(
                "UPDATE llm_logical_call SET principal_id = %s, "
                "response_payload = NULL, error_reason = NULL "
                "WHERE principal_id = %s OR session_id = ANY(%s::uuid[]) "
                "OR run_id = ANY(%s::uuid[])",
                (
                    deleted_principal_ref,
                    context.principal_id,
                    session_ids,
                    run_ids,
                ),
            )
        cache_exists = await (
            await conn.execute(
                "SELECT to_regclass('public.llm_result_cache') IS NOT NULL AS present"
            )
        ).fetchone()
        if (
            cache_exists
            and cache_exists["present"]
            and referenced_cache_keys
        ):
            await conn.execute(
                """
                UPDATE llm_result_cache
                SET status = 'INVALIDATED',
                    result_payload = NULL,
                    invalidation_reason = 'PRINCIPAL_DELETED',
                    invalidated_at = NOW(),
                    completed_at = NULL,
                    updated_at = NOW()
                WHERE cache_key_hash = ANY(%s::char(64)[])
                """,
                (list(referenced_cache_keys),),
            )
        budget_exists = await (
            await conn.execute(
                "SELECT to_regclass('public.llm_budget_scope') IS NOT NULL AS present"
            )
        ).fetchone()
        if budget_exists and budget_exists["present"]:
            await conn.execute(
                "UPDATE llm_budget_scope SET principal_id = %s "
                "WHERE principal_id = %s OR session_id = ANY(%s::uuid[])",
                (
                    deleted_principal_ref,
                    context.principal_id,
                    session_ids,
                ),
            )
        call_log_exists = await (
            await conn.execute(
                "SELECT to_regclass('public.llm_call_log') IS NOT NULL AS present"
            )
        ).fetchone()
        if call_log_exists and call_log_exists["present"]:
            await conn.execute(
                "UPDATE llm_call_log SET trace_id = NULL, session_id = NULL, "
                "node = NULL, error_message = NULL "
                "WHERE session_id = ANY(%s::uuid[])",
                (session_ids,),
            )


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
