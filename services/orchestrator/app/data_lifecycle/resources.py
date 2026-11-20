"""principal 私有资源清理的共享存储操作。"""
from __future__ import annotations

import psycopg


async def delete_published_result_chain(
    conn: psycopg.AsyncConnection,
    run_ids: list,
) -> None:
    await conn.execute(
        "DELETE FROM run_evidence_package WHERE run_id = ANY(%s::uuid[])",
        (run_ids,),
    )
    await conn.execute(
        "DELETE FROM run_verification_report WHERE run_id = ANY(%s::uuid[])",
        (run_ids,),
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
        (run_ids,),
    )
    await conn.execute(
        "DELETE FROM run_formal_gate_artifact WHERE run_id = ANY(%s::uuid[])",
        (run_ids,),
    )
    await conn.execute(
        "DELETE FROM run_result WHERE run_id = ANY(%s::uuid[])",
        (run_ids,),
    )


async def delete_official_checkpoints(
    conn: psycopg.AsyncConnection,
    thread_ids: list[str],
) -> int:
    if not thread_ids:
        return 0
    affected = 0
    for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
        exists = await (
            await conn.execute(
                "SELECT to_regclass(%s) IS NOT NULL AS present",
                (f"public.{table}",),
            )
        ).fetchone()
        if not exists or not exists["present"]:
            continue
        result = await conn.execute(
            f"DELETE FROM {table} WHERE thread_id = ANY(%s::text[])",
            (thread_ids,),
        )
        affected += result.rowcount
    return affected
