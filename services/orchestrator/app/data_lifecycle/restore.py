"""备份恢复期间的删除安全闸门与 tombstone/ledger 重放。"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from typing import Protocol

import psycopg
from psycopg.rows import dict_row

from app.data_lifecycle.deletion import (
    DeletionResourceContext,
    PostgresPrincipalDataCleaner,
    PrincipalDataCleaner,
)


class RestoreProjection(Protocol):
    async def reset_after_restore(self) -> None: ...


@dataclass(frozen=True, slots=True)
class RestoreBackupCommand:
    restore_id: str


@dataclass(frozen=True, slots=True)
class RestoreBackupReceipt:
    accepted: bool
    status: str
    reason: str | None
    replayed_principal_count: int
    replayed: bool = False


class PrincipalDataRestore:
    def __init__(
        self,
        dsn: str,
        *,
        projection: RestoreProjection | None = None,
        cleaner: PrincipalDataCleaner | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not dsn:
            raise ValueError("PrincipalDataRestore 需要 PostgreSQL DSN")
        self._dsn = dsn
        self._projection = projection
        self._cleaner = cleaner or PostgresPrincipalDataCleaner()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def RestoreFromBackup(
        self,
        command: RestoreBackupCommand,
    ) -> RestoreBackupReceipt:
        restore_id = command.restore_id.strip()
        if not restore_id:
            raise ValueError("restore_id is required")
        conn = await self._connect()
        try:
            await self._begin_restore(conn, restore_id)
            try:
                completed = await self._completed_replay_count(
                    conn,
                    restore_id,
                )
                if completed > 0:
                    await self._open_service(conn, restore_id)
                    return RestoreBackupReceipt(
                        accepted=True,
                        status="OPEN",
                        reason=None,
                        replayed_principal_count=completed,
                        replayed=True,
                    )
                if self._projection is not None:
                    await self._projection.reset_after_restore()
                replayed = await self._replay_tombstones(conn, restore_id)
                await self._open_service(conn, restore_id)
            except Exception as exc:
                await self._keep_closed(conn, restore_id, type(exc).__name__)
                return RestoreBackupReceipt(
                    accepted=False,
                    status="RESTORE_FAILED_RETRYABLE",
                    reason="RESTORE_FAILED_RETRYABLE",
                    replayed_principal_count=0,
                )
            return RestoreBackupReceipt(
                accepted=True,
                status="OPEN",
                reason=None,
                replayed_principal_count=replayed,
            )
        finally:
            await conn.close()

    @staticmethod
    async def _completed_replay_count(
        conn: psycopg.AsyncConnection,
        restore_id: str,
    ) -> int:
        row = await (
            await conn.execute(
                """
                SELECT
                    (SELECT count(*)
                     FROM principal_deletion_tombstone) AS tombstones,
                    (SELECT count(*)
                     FROM data_lifecycle_restore_replay
                     WHERE restore_id = %s
                       AND result = 'SUCCEEDED') AS replayed
                """,
                (restore_id,),
            )
        ).fetchone()
        if row["tombstones"] > 0 and row["replayed"] == row["tombstones"]:
            return int(row["replayed"])
        return 0

    async def _connect(self) -> psycopg.AsyncConnection:
        return await psycopg.AsyncConnection.connect(
            self._dsn,
            row_factory=dict_row,
            autocommit=True,
        )

    async def _begin_restore(
        self,
        conn: psycopg.AsyncConnection,
        restore_id: str,
    ) -> None:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('m5b-restore', 0))"
            )
            await conn.execute(
                """
                INSERT INTO data_lifecycle_restore_state (
                    restore_key, status, restore_id, error_code, updated_at
                )
                VALUES ('SERVICE', 'RESTORING', %s, NULL, %s)
                ON CONFLICT (restore_key) DO UPDATE
                SET status = 'RESTORING',
                    restore_id = EXCLUDED.restore_id,
                    error_code = NULL,
                    updated_at = EXCLUDED.updated_at
                """,
                (restore_id, self._clock()),
            )

    async def _replay_tombstones(
        self,
        conn: psycopg.AsyncConnection,
        restore_id: str,
    ) -> int:
        tombstones = await (
            await conn.execute(
                """
                SELECT principal_ref, request_id, completed_at
                FROM principal_deletion_tombstone
                ORDER BY created_at, principal_ref
                """
            )
        ).fetchall()
        principal_ids = await self._principal_ids_by_ref(conn)
        replayed = 0
        for tombstone in tombstones:
            principal_ref = tombstone["principal_ref"]
            request_id = tombstone["request_id"]
            ledger = await (
                await conn.execute(
                    """
                    SELECT
                        (SELECT count(*) FROM principal_deletion_request
                         WHERE principal_ref = %s AND request_id = %s) AS requests,
                        (SELECT count(*) FROM principal_deletion_ledger
                         WHERE principal_ref = %s AND request_id = %s
                           AND result = 'SUCCEEDED') AS resources
                    """,
                    (
                        principal_ref,
                        request_id,
                        principal_ref,
                        request_id,
                    ),
                )
            ).fetchone()
            if ledger["requests"] != 1 or ledger["resources"] < 1:
                raise RuntimeError("DELETION_LEDGER_INCOMPLETE")

            replayed += await self._replay_principal(
                conn,
                restore_id=restore_id,
                principal_ref=principal_ref,
                request_id=request_id,
                principal_id=principal_ids.get(principal_ref),
                completed=tombstone["completed_at"] is not None,
            )
        return replayed

    async def _replay_principal(
        self,
        conn: psycopg.AsyncConnection,
        *,
        restore_id: str,
        principal_ref: str,
        request_id: str,
        principal_id: str | None,
        completed: bool,
    ) -> int:
        if principal_id is None:
            await conn.execute(
                """
                INSERT INTO data_lifecycle_restore_replay (
                    restore_id, principal_ref, request_id, result, replayed_at
                )
                VALUES (%s, %s, %s, 'SUCCEEDED', %s)
                ON CONFLICT (restore_id, principal_ref) DO NOTHING
                """,
                (restore_id, principal_ref, request_id, self._clock()),
            )
            return 1
        session_rows = await (
            await conn.execute(
                "SELECT id FROM chat_session WHERE principal_id = %s",
                (principal_id,),
            )
        ).fetchall()
        run_rows = await (
            await conn.execute(
                "SELECT run_id FROM run WHERE principal_id = %s",
                (principal_id,),
            )
        ).fetchall()
        context = DeletionResourceContext(
            principal_id=principal_id,
            principal_ref=principal_ref,
            request_id=request_id,
            session_ids=tuple(row["id"] for row in session_rows),
            run_ids=tuple(row["run_id"] for row in run_rows),
        )
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
            (principal_id, request_id, self._clock()),
        )
        async with conn.transaction():
            await conn.execute("SET LOCAL app.principal_deletion = 'on'")
            await self._cleaner.cleanup(conn, context)
        now = self._clock()
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE principal_data_state
                SET status = 'DELETED', updated_at = %s
                WHERE principal_id = %s
                """,
                (now, principal_id),
            )
            if completed:
                await conn.execute(
                    """
                    UPDATE principal_deletion_tombstone
                    SET completed_at = COALESCE(completed_at, %s)
                    WHERE principal_ref = %s
                    """,
                    (now, principal_ref),
                )
            await conn.execute(
                """
                INSERT INTO data_lifecycle_restore_replay (
                    restore_id, principal_ref, request_id, result, replayed_at
                )
                VALUES (%s, %s, %s, 'SUCCEEDED', %s)
                ON CONFLICT (restore_id, principal_ref) DO NOTHING
                """,
                (restore_id, principal_ref, request_id, now),
            )
        return 1

    async def _principal_ids_by_ref(
        self,
        conn: psycopg.AsyncConnection,
    ) -> dict[str, str]:
        rows = await (
            await conn.execute(
                """
                SELECT principal_id FROM chat_session
                WHERE principal_id IS NOT NULL
                  AND principal_id NOT LIKE 'deleted:%%'
                UNION
                SELECT principal_id FROM run
                WHERE principal_id NOT LIKE 'deleted:%%'
                """
            )
        ).fetchall()
        return {
            hashlib.sha256(row["principal_id"].encode("utf-8")).hexdigest():
                row["principal_id"]
            for row in rows
        }

    async def _open_service(
        self,
        conn: psycopg.AsyncConnection,
        restore_id: str,
    ) -> None:
        await conn.execute(
            """
            UPDATE data_lifecycle_restore_state
            SET status = 'OPEN',
                restore_id = %s,
                error_code = NULL,
                updated_at = %s
            WHERE restore_key = 'SERVICE'
            """,
            (restore_id, self._clock()),
        )

    async def _keep_closed(
        self,
        conn: psycopg.AsyncConnection,
        restore_id: str,
        error_code: str,
    ) -> None:
        await conn.execute(
            """
            UPDATE data_lifecycle_restore_state
            SET status = 'RESTORING',
                restore_id = %s,
                error_code = %s,
                updated_at = %s
            WHERE restore_key = 'SERVICE'
            """,
            (restore_id, error_code, self._clock()),
        )
