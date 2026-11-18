from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb


async def append_event(
    conn,
    *,
    run_id: UUID,
    event_type: str,
    stage: str,
    state_version: int,
    payload: dict[str, Any],
    payload_version: int = 1,
) -> int:
    """追加 run 内唯一下一个持久业务事件，调用方负责同一事务提交。"""
    row = await (
        await conn.execute(
            """
            SELECT COALESCE(MAX(event_seq), 0) + 1 AS next_seq
            FROM run_event
            WHERE run_id = %s
            """,
            (run_id,),
        )
    ).fetchone()
    next_seq = row["next_seq"]
    await conn.execute(
        """
        INSERT INTO run_event (
            run_id, event_seq, event_type, stage, state_version,
            payload_version, payload
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            run_id,
            next_seq,
            event_type,
            stage,
            state_version,
            payload_version,
            Jsonb(payload),
        ),
    )
    return next_seq
