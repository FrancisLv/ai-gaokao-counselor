"""从 RunLifecycle 与 LLMGateway 权威记录构造发布门禁样本。"""
from __future__ import annotations

from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from eval.release_gate import RunSloSample


async def load_run_slo_samples(
    dsn: str,
    *,
    run_ids: tuple[UUID, ...],
    reproducible_by_run: dict[str, bool],
) -> tuple[RunSloSample, ...]:
    if not run_ids:
        return ()
    conn = await psycopg.AsyncConnection.connect(
        dsn,
        row_factory=dict_row,
    )
    try:
        rows = await (
            await conn.execute(
                """
                SELECT run.run_id, run.session_id,
                       CASE
                           WHEN run.active_execution_remaining_ms <= 600000
                           THEN 600000 - run.active_execution_remaining_ms
                           ELSE 0
                       END AS active_execution_ms,
                       COALESCE(scope.provider_tokens_settled, 0)
                           AS provider_token_count,
                       COALESCE(scope.cost_settled_micro_usd, 0)
                           AS provider_cost_micro_usd,
                       CASE
                           WHEN scope.budget_status = 'BUDGET_OVERRUN'
                           THEN 1 ELSE 0
                       END AS budget_violation_count,
                       EXISTS (
                           SELECT 1
                           FROM llm_logical_call call
                           JOIN llm_provider_attempt attempt
                             ON attempt.logical_call_pk = call.logical_call_pk
                           WHERE call.run_id = run.run_id
                             AND attempt.status = 'PENDING_RECONCILIATION'
                       ) AS reconciliation_pending
                FROM run
                LEFT JOIN llm_budget_scope scope
                  ON scope.principal_id = run.principal_id
                 AND scope.session_id = run.session_id
                WHERE run.run_id = ANY(%s)
                ORDER BY run.run_id
                """,
                (list(run_ids),),
            )
        ).fetchall()
    finally:
        await conn.close()
    found = {row["run_id"] for row in rows}
    missing = sorted(set(run_ids) - found)
    if missing:
        raise ValueError(
            "run telemetry is missing run ids: "
            + ", ".join(str(run_id) for run_id in missing)
        )
    return tuple(
        RunSloSample(
            run_id=str(row["run_id"]),
            session_id=str(row["session_id"]),
            active_execution_ms=int(row["active_execution_ms"]),
            provider_token_count=int(row["provider_token_count"]),
            provider_cost_micro_usd=int(
                row["provider_cost_micro_usd"]
            ),
            budget_violation_count=int(
                row["budget_violation_count"]
            ),
            reconciliation_pending=bool(
                row["reconciliation_pending"]
            ),
            reproducible=bool(
                reproducible_by_run.get(str(row["run_id"]), False)
            ),
        )
        for row in rows
    )
