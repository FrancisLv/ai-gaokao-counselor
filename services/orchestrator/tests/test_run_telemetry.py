"""M5b-04：从 RunLifecycle 与 LLMGateway 权威记录采集 SLO 样本。"""
from __future__ import annotations

from uuid import uuid4

from run_lifecycle_support import RunLifecyclePostgresTest


class RunTelemetryTests(RunLifecyclePostgresTest):
    async def test_collects_active_execution_and_authoritative_budget_ledger(self):
        from eval.run_telemetry import load_run_slo_samples

        release_id = await self.seed_release()
        run_id = uuid4()
        session_id = uuid4()
        principal_id = "principal-telemetry"
        conn = await self._connect()
        try:
            await conn.execute(
                "INSERT INTO chat_session (id) VALUES (%s)",
                (session_id,),
            )
            await conn.execute(
                """
                INSERT INTO run (
                    run_id, session_id, principal_id, task_kind, task_input,
                    state, stage, release_id,
                    active_execution_remaining_ms
                )
                VALUES (
                    %s, %s, %s, 'GENERATE_PLAN', '{}'::jsonb,
                    'SUCCEEDED', 'FORMAL_READY', %s, 590000
                )
                """,
                (run_id, session_id, principal_id, release_id),
            )
            await conn.execute(
                """
                INSERT INTO llm_budget_scope (
                    principal_id, session_id,
                    provider_tokens_settled, cost_settled_micro_usd
                )
                VALUES (%s, %s, 123, 456)
                """,
                (principal_id, session_id),
            )
            await conn.execute(
                """
                INSERT INTO llm_price_table (
                    price_table_version, currency_unit, effective_at, published_at
                )
                VALUES ('telemetry-price-v1', 'USD_MICRO', NOW(), NOW())
                """
            )
            logical_pk = uuid4()
            budget_scope_id = (
                await (
                    await conn.execute(
                        """
                        SELECT budget_scope_id
                        FROM llm_budget_scope
                        WHERE principal_id = %s AND session_id = %s
                        """,
                        (principal_id, session_id),
                    )
                ).fetchone()
            )["budget_scope_id"]
            await conn.execute(
                """
                INSERT INTO llm_logical_call (
                    logical_call_pk, logical_call_id, principal_id,
                    session_id, run_id, role, release_id, route_version,
                    price_table_version, model_id, request_hash,
                    budget_scope_id, reserved_input_tokens,
                    reserved_output_tokens, reserved_cost_micro_usd
                )
                VALUES (
                    %s, 'telemetry-call', %s, %s, %s, 'report', %s,
                    'route-v1', 'telemetry-price-v1', 'model-v1',
                    %s, %s, 100, 10, 456
                )
                """,
                (
                    logical_pk,
                    principal_id,
                    session_id,
                    run_id,
                    release_id,
                    "a" * 64,
                    budget_scope_id,
                ),
            )
            await conn.execute(
                """
                INSERT INTO llm_provider_attempt (
                    logical_call_pk, logical_call_id, model_id, provider,
                    status, reserved_tokens, reserved_cost_micro_usd
                )
                VALUES (
                    %s, 'telemetry-call', 'model-v1', 'fake',
                    'PENDING_RECONCILIATION', 110, 456
                )
                """,
                (logical_pk,),
            )
        finally:
            await conn.close()

        samples = await load_run_slo_samples(
            self.dsn,
            run_ids=(run_id,),
            reproducible_by_run={str(run_id): True},
        )

        self.assertEqual(len(samples), 1)
        sample = samples[0]
        self.assertEqual(sample.active_execution_ms, 10000)
        self.assertEqual(sample.provider_token_count, 123)
        self.assertEqual(sample.provider_cost_micro_usd, 456)
        self.assertEqual(sample.budget_violation_count, 0)
        self.assertTrue(sample.reconciliation_pending)
        self.assertTrue(sample.reproducible)
