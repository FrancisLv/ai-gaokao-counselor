"""版本化终态数据保留与幂等清理。"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from app.data_lifecycle.resources import (
    delete_official_checkpoints,
    delete_published_result_chain,
)


class DataRetentionError(Exception):
    def __init__(self, reason: str, message: str | None = None) -> None:
        self.reason = reason
        super().__init__(message or reason)


@dataclass(frozen=True, slots=True)
class SweepRetentionCommand:
    policy_version: str


@dataclass(frozen=True, slots=True)
class RetentionSweepReceipt:
    sweep_id: UUID
    policy_version: str
    status: str
    reason: str | None
    affected_rows: dict[str, int]
    failed_resources: tuple[str, ...]


class DataRetention:
    REQUIRED_RESOURCES = (
        "FINAL_RESULTS",
        "SUCCESSFUL_CHECKPOINTS",
        "PROFILE",
        "RUN_METADATA",
        "RUN_EVENTS",
        "FAILED_CHECKPOINTS",
        "COMMAND_RECEIPTS",
        "PROVIDER_LEDGER",
        "SECURITY_AUDIT",
    )

    def __init__(
        self,
        dsn: str,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not dsn:
            raise ValueError("DataRetention 需要 PostgreSQL DSN")
        self._dsn = dsn
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def SweepExpiredData(
        self,
        command: SweepRetentionCommand,
    ) -> RetentionSweepReceipt:
        conn = await self._connect()
        try:
            policies = await self._load_policies(
                conn,
                policy_version=command.policy_version,
            )
        finally:
            await conn.close()

        sweep_id = uuid4()
        started_at = self._clock()
        conn = await self._connect()
        try:
            await conn.execute(
                """
                INSERT INTO data_retention_sweep (
                    sweep_id, policy_version, status, started_at
                )
                VALUES (%s, %s, 'RUNNING', %s)
                """,
                (sweep_id, command.policy_version, started_at),
            )
        finally:
            await conn.close()

        affected_rows: dict[str, int] = {}
        failed_resources: list[str] = []
        for resource_kind in self.REQUIRED_RESOURCES:
            policy = policies[resource_kind]
            try:
                affected = await self._apply_resource(
                    sweep_id=sweep_id,
                    policy=policy,
                    now=started_at,
                )
            except Exception as exc:
                failed_resources.append(resource_kind)
                await self._record_resource(
                    sweep_id=sweep_id,
                    policy=policy,
                    affected_rows=0,
                    error_code=type(exc).__name__,
                    now=self._clock(),
                )
                continue
            affected_rows[resource_kind] = affected
            await self._record_resource(
                sweep_id=sweep_id,
                policy=policy,
                affected_rows=affected,
                error_code=None,
                now=self._clock(),
            )

        status = (
            "RETENTION_FAILED_RETRYABLE"
            if failed_resources
            else "COMPLETED"
        )
        reason = status if failed_resources else None
        completed_at = self._clock()
        conn = await self._connect()
        try:
            await conn.execute(
                """
                UPDATE data_retention_sweep
                SET status = %s, completed_at = %s, error_code = %s
                WHERE sweep_id = %s
                """,
                (
                    status,
                    completed_at,
                    "RETENTION_RESOURCE_FAILED" if failed_resources else None,
                    sweep_id,
                ),
            )
        finally:
            await conn.close()
        return RetentionSweepReceipt(
            sweep_id=sweep_id,
            policy_version=command.policy_version,
            status=status,
            reason=reason,
            affected_rows=affected_rows,
            failed_resources=tuple(failed_resources),
        )

    async def _load_policies(
        self,
        conn: psycopg.AsyncConnection,
        *,
        policy_version: str,
    ) -> dict[str, dict[str, Any]]:
        rows = await (
            await conn.execute(
                """
                SELECT resource_kind, retention_days, action
                FROM data_retention_policy
                WHERE policy_version = %s
                """,
                (policy_version,),
            )
        ).fetchall()
        policies = {row["resource_kind"]: row for row in rows}
        if any(kind not in policies for kind in self.REQUIRED_RESOURCES):
            raise DataRetentionError("RETENTION_POLICY_INVALID")
        return policies

    async def _apply_resource(
        self,
        *,
        sweep_id: UUID,
        policy: dict[str, Any],
        now: datetime,
    ) -> int:
        if policy["resource_kind"] == "FINAL_RESULTS":
            return await self._delete_final_results(
                cutoff=now - timedelta(days=policy["retention_days"])
            )
        if policy["resource_kind"] == "PROFILE":
            return await self._delete_profiles(
                cutoff=now - timedelta(days=policy["retention_days"])
            )
        if policy["resource_kind"] == "RUN_METADATA":
            return await self._deidentify_run_metadata(
                cutoff=now - timedelta(days=policy["retention_days"])
            )
        if policy["resource_kind"] == "RUN_EVENTS":
            return await self._delete_run_events(
                cutoff=now - timedelta(days=policy["retention_days"])
            )
        if policy["resource_kind"] == "COMMAND_RECEIPTS":
            return await self._delete_command_receipts(
                cutoff=now - timedelta(days=policy["retention_days"])
            )
        if policy["resource_kind"] == "SUCCESSFUL_CHECKPOINTS":
            return await self._delete_checkpoints(
                terminal_states=("SUCCEEDED",),
                cutoff=now - timedelta(days=policy["retention_days"]),
            )
        if policy["resource_kind"] == "FAILED_CHECKPOINTS":
            return await self._delete_checkpoints(
                terminal_states=("FAILED", "EXPIRED"),
                cutoff=now - timedelta(days=policy["retention_days"]),
            )
        if policy["resource_kind"] == "PROVIDER_LEDGER":
            return await self._deidentify_provider_ledger(
                cutoff=now - timedelta(days=policy["retention_days"]),
            )
        if policy["resource_kind"] == "SECURITY_AUDIT":
            return await self._deidentify_security_audit(
                cutoff=now - timedelta(days=policy["retention_days"]),
            )
        return 0

    async def _delete_final_results(self, *, cutoff: datetime) -> int:
        conn = await self._connect()
        try:
            async with conn.transaction():
                await conn.execute("SET LOCAL app.retention_cleanup = 'on'")
                rows = await (
                    await conn.execute(
                        """
                        SELECT r.run_id, rr.result_version
                        FROM run r
                        JOIN run_result rr ON rr.run_id = r.run_id
                        WHERE r.state = 'SUCCEEDED'
                          AND r.updated_at < %s
                        """,
                        (cutoff,),
                    )
                ).fetchall()
                run_ids = [row["run_id"] for row in rows]
                if not run_ids:
                    return 0
                await delete_published_result_chain(conn, run_ids)
                await conn.execute(
                    """
                    UPDATE run
                    SET result_version = NULL, result_kind = NULL
                    WHERE run_id = ANY(%s::uuid[])
                    """,
                    (run_ids,),
                )
                return len(rows)
        finally:
            await conn.close()

    async def _delete_profiles(self, *, cutoff: datetime) -> int:
        conn = await self._connect()
        try:
            async with conn.transaction():
                rows = await (
                    await conn.execute(
                        """
                        SELECT pr.profile_version, pr.run_id
                        FROM profile_revision pr
                        JOIN run r ON r.run_id = pr.run_id
                        WHERE r.state = 'SUCCEEDED'
                          AND r.updated_at < %s
                        """,
                        (cutoff,),
                    )
                ).fetchall()
                profile_versions = [row["profile_version"] for row in rows]
                if not profile_versions:
                    return 0
                run_ids = [row["run_id"] for row in rows]
                await conn.execute(
                    """
                    UPDATE run
                    SET working_profile_version = NULL,
                        confirmed_profile_version = NULL
                    WHERE run_id = ANY(%s::uuid[])
                      AND (
                          working_profile_version = ANY(%s::uuid[])
                          OR confirmed_profile_version = ANY(%s::uuid[])
                      )
                    """,
                    (run_ids, profile_versions, profile_versions),
                )
                await conn.execute(
                    """
                    DELETE FROM profile_confirmation
                    WHERE profile_version = ANY(%s::uuid[])
                    """,
                    (profile_versions,),
                )
                await conn.execute(
                    "DELETE FROM profile_revision WHERE profile_version = ANY(%s::uuid[])",
                    (profile_versions,),
                )
                return len(rows)
        finally:
            await conn.close()

    async def _deidentify_run_metadata(self, *, cutoff: datetime) -> int:
        conn = await self._connect()
        try:
            async with conn.transaction():
                rows = await (
                    await conn.execute(
                        """
                        SELECT run_id, session_id
                        FROM run
                        WHERE state IN (
                            'SUCCEEDED', 'FAILED', 'EXPIRED', 'CANCELLED'
                        )
                          AND updated_at < %s
                        """,
                        (cutoff,),
                    )
                ).fetchall()
                if not rows:
                    return 0
                run_ids = [row["run_id"] for row in rows]
                candidate_session_ids = sorted(
                    {row["session_id"] for row in rows},
                    key=str,
                )
                stale_sessions = await (
                    await conn.execute(
                        """
                        SELECT session_id
                        FROM run
                        WHERE session_id = ANY(%s::uuid[])
                        GROUP BY session_id
                        HAVING bool_and(
                            state IN (
                                'SUCCEEDED', 'FAILED', 'EXPIRED', 'CANCELLED'
                            )
                            AND updated_at < %s
                        )
                        """,
                        (candidate_session_ids, cutoff),
                    )
                ).fetchall()
                session_ids = [row["session_id"] for row in stale_sessions]
                interrupts = await conn.execute(
                    "DELETE FROM run_interrupt WHERE run_id = ANY(%s::uuid[])",
                    (run_ids,),
                )
                runs = await conn.execute(
                    """
                    UPDATE run
                    SET principal_id = 'retention:deidentified',
                        task_input = '{}'::jsonb,
                        parent_result_version = NULL,
                        working_profile_version = NULL,
                        confirmed_profile_version = NULL,
                        result_version = NULL,
                        result_kind = NULL,
                        updated_at = NOW()
                    WHERE run_id = ANY(%s::uuid[])
                    """,
                    (run_ids,),
                )
                sessions = 0
                if session_ids:
                    result = await conn.execute(
                        """
                        UPDATE chat_session
                        SET principal_id = 'retention:deidentified',
                            user_id = NULL,
                            profile = NULL,
                            status = 'retention_expired',
                            current_node = NULL,
                            updated_at = NOW()
                        WHERE id = ANY(%s::uuid[])
                        """,
                        (session_ids,),
                    )
                    sessions = result.rowcount
                return interrupts.rowcount + runs.rowcount + sessions
        finally:
            await conn.close()

    async def _delete_run_events(self, *, cutoff: datetime) -> int:
        conn = await self._connect()
        try:
            result = await conn.execute(
                "DELETE FROM run_event WHERE occurred_at < %s",
                (cutoff,),
            )
            return result.rowcount
        finally:
            await conn.close()

    async def _delete_command_receipts(self, *, cutoff: datetime) -> int:
        conn = await self._connect()
        try:
            result = await conn.execute(
                "DELETE FROM run_command_receipt WHERE expires_at < %s",
                (cutoff,),
            )
            return result.rowcount
        finally:
            await conn.close()

    async def _delete_checkpoints(
        self,
        *,
        terminal_states: tuple[str, ...],
        cutoff: datetime,
    ) -> int:
        conn = await self._connect()
        try:
            rows = await (
                await conn.execute(
                    """
                    SELECT run_id, session_id
                    FROM run
                    WHERE state = ANY(%s::text[])
                      AND updated_at < %s
                    """,
                    (list(terminal_states), cutoff),
                )
            ).fetchall()
            if not rows:
                return 0
            run_ids = [str(row["run_id"]) for row in rows]
            session_ids = [row["session_id"] for row in rows]
            affected = await delete_official_checkpoints(conn, run_ids)
            plan_result = await conn.execute(
                """
                DELETE FROM plan_checkpoint
                WHERE session_id = ANY(%s::uuid[])
                  AND created_at < %s
                """,
                (session_ids, cutoff),
            )
            affected += plan_result.rowcount
            return affected
        finally:
            await conn.close()

    async def _deidentify_provider_ledger(self, *, cutoff: datetime) -> int:
        conn = await self._connect()
        try:
            rows = await (
                await conn.execute(
                    """
                    SELECT logical_call_pk, budget_scope_id
                    FROM llm_logical_call
                    WHERE created_at < %s
                      AND principal_id <> 'retention:deidentified'
                    """,
                    (cutoff,),
                )
            ).fetchall()
            if not rows:
                await conn.execute(
                    """
                    UPDATE llm_call_log
                    SET trace_id = NULL, session_id = NULL, node = NULL,
                        error_message = NULL
                    WHERE created_at < %s
                      AND (
                          trace_id IS NOT NULL
                          OR session_id IS NOT NULL
                          OR node IS NOT NULL
                          OR error_message IS NOT NULL
                      )
                    """,
                    (cutoff,),
                )
                return 0
            logical_call_pks = [row["logical_call_pk"] for row in rows]
            budget_scope_ids = [row["budget_scope_id"] for row in rows]
            affected = 0
            logical = await conn.execute(
                """
                UPDATE llm_logical_call
                SET principal_id = 'retention:deidentified',
                    response_payload = NULL,
                    error_reason = NULL,
                    updated_at = NOW()
                WHERE logical_call_pk = ANY(%s::uuid[])
                """,
                (logical_call_pks,),
            )
            affected += logical.rowcount
            budget = await conn.execute(
                """
                UPDATE llm_budget_scope
                SET principal_id = 'retention:deidentified',
                    updated_at = NOW()
                WHERE budget_scope_id = ANY(%s::uuid[])
                """,
                (budget_scope_ids,),
            )
            affected += budget.rowcount
            attempts = await conn.execute(
                """
                UPDATE llm_provider_attempt
                SET provider_request_id = NULL, error_code = NULL
                WHERE logical_call_pk = ANY(%s::uuid[])
                  AND (
                      provider_request_id IS NOT NULL
                      OR error_code IS NOT NULL
                  )
                """,
                (logical_call_pks,),
            )
            affected += attempts.rowcount
            call_log = await conn.execute(
                """
                UPDATE llm_call_log
                SET trace_id = NULL, session_id = NULL, node = NULL,
                    error_message = NULL
                WHERE created_at < %s
                  AND (
                      trace_id IS NOT NULL
                      OR session_id IS NOT NULL
                      OR node IS NOT NULL
                      OR error_message IS NOT NULL
                  )
                """,
                (cutoff,),
            )
            affected += call_log.rowcount
            return affected
        finally:
            await conn.close()

    async def _deidentify_security_audit(self, *, cutoff: datetime) -> int:
        conn = await self._connect()
        try:
            async with conn.transaction():
                state = await conn.execute(
                    """
                    UPDATE principal_data_state
                    SET deletion_request_id = 'retention-expired',
                        updated_at = NOW()
                    WHERE updated_at < %s
                      AND deletion_request_id IS NOT NULL
                    """,
                    (cutoff,),
                )
                ledger = await conn.execute(
                    """
                    UPDATE principal_deletion_ledger
                    SET request_id = 'retention-expired', error_code = NULL
                    WHERE occurred_at < %s
                      AND (
                          request_id <> 'retention-expired'
                          OR error_code IS NOT NULL
                      )
                    """,
                    (cutoff,),
                )
                tombstone = await conn.execute(
                    """
                    UPDATE principal_deletion_tombstone
                    SET request_id = 'retention-expired',
                        reason_code = 'RETENTION_EXPIRED'
                    WHERE created_at < %s
                      AND (
                          request_id <> 'retention-expired'
                          OR reason_code <> 'RETENTION_EXPIRED'
                      )
                    """,
                    (cutoff,),
                )
                request = await conn.execute(
                    """
                    DELETE FROM principal_deletion_request
                    WHERE updated_at < %s
                    """,
                    (cutoff,),
                )
                return (
                    state.rowcount
                    + ledger.rowcount
                    + tombstone.rowcount
                    + request.rowcount
                )
        finally:
            await conn.close()

    async def _record_resource(
        self,
        *,
        sweep_id: UUID,
        policy: dict[str, Any],
        affected_rows: int,
        error_code: str | None,
        now: datetime,
    ) -> None:
        conn = await self._connect()
        try:
            await conn.execute(
                """
                INSERT INTO data_retention_ledger (
                    sweep_id, resource_kind, action, result,
                    affected_rows, error_code, occurred_at
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    sweep_id,
                    policy["resource_kind"],
                    policy["action"],
                    "FAILED" if error_code else "SUCCEEDED",
                    affected_rows,
                    error_code,
                    now,
                ),
            )
        finally:
            await conn.close()

    async def _connect(self) -> psycopg.AsyncConnection:
        return await psycopg.AsyncConnection.connect(
            self._dsn,
            row_factory=dict_row,
            autocommit=True,
        )
