"""按 principal 删除或去标识化私有资源。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

import psycopg

from app.data_lifecycle.resources import (
    delete_official_checkpoints,
    delete_published_result_chain,
)


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
    """在调用方事务连接上清理 principal 私有资源。"""

    async def cleanup(
        self,
        conn: psycopg.AsyncConnection,
        context: DeletionResourceContext,
    ) -> None:
        deleted_principal_ref = f"deleted:{context.principal_ref}"
        session_ids = tuple(str(value) for value in context.session_ids)
        run_ids = tuple(str(value) for value in context.run_ids)

        await delete_official_checkpoints(conn, list(run_ids))
        await delete_published_result_chain(conn, list(run_ids))
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
