"""ticket #2 的 fresh schema 迁移验收。"""
from __future__ import annotations

import asyncio

from run_lifecycle_support import RunLifecyclePostgresTest


class FreshSchemaTests(RunLifecyclePostgresTest):
    async def test_conversation_history_schema_is_installed(self):
        conn = await self._connect()
        try:
            columns = await (
                await conn.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'chat_message'
                    ORDER BY column_name
                    """
                )
            ).fetchall()
            constraint = await (
                await conn.execute(
                    """
                    SELECT pg_get_constraintdef(oid) AS definition
                    FROM pg_constraint
                    WHERE conname = 'run_result_result_kind_check'
                      AND conrelid = 'run_result'::regclass
                    """
                )
            ).fetchone()
            lineage = await (
                await conn.execute(
                    """
                    SELECT to_regclass('public.admission_group_lineage') AS name
                    """
                )
            ).fetchone()
        finally:
            await conn.close()

        names = {row["column_name"] for row in columns}
        self.assertTrue(
            {
                "message_id",
                "message_seq",
                "session_id",
                "run_id",
                "role",
                "kind",
                "content",
                "metadata",
                "created_at",
            }.issubset(names)
        )
        self.assertIn("EXPLAIN_RESULT", constraint["definition"])
        self.assertEqual(
            lineage["name"],
            "admission_group_lineage",
        )

    async def test_shared_fixture_keeps_release_rrf_contract(self):
        release_id = await self.seed_release()
        conn = await self._connect()
        try:
            row = await (
                await conn.execute(
                    """
                    SELECT rrf_k
                    FROM release
                    WHERE release_id = %s
                    """,
                    (release_id,),
                )
            ).fetchone()
        finally:
            await conn.close()

        self.assertIsNotNone(row)
        self.assertEqual(row["rrf_k"], 60)

    async def test_run_lifecycle_works_on_fresh_schema_through_m5b_deletion(self):
        from app.run_lifecycle import RunLifecycle, StructuredProfileInput, SubmitRunCommand

        await self.reset_to_fresh_schema()
        release_id = await self.seed_release()
        lifecycle = RunLifecycle(self.dsn)

        receipt = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="fresh-schema-principal",
                session_id=None,
                task_kind="GENERATE_PLAN",
                input=StructuredProfileInput(
                    province="湖南",
                    application_year=2025,
                    category="物理类",
                    batch="本科批",
                    score=600,
                    subjects=("物理", "化学", "生物"),
                ),
                idempotency_key="fresh-schema-submit",
            )
        )

        view = await lifecycle.GetRun(
            principal_id="fresh-schema-principal",
            run_id=receipt.run_id,
        )
        self.assertEqual(view.release_id, release_id)
        self.assertEqual(view.state, "QUEUED")

    async def test_formal_result_migration_upgrades_26_and_preserves_old_results(self):
        from uuid import uuid4

        from psycopg.types.json import Jsonb

        from app.run_lifecycle import RunLifecycle, StructuredProfileInput, SubmitRunCommand

        await self.reset_to_fresh_schema(apply_formal_result_migration=False)
        release_id = await self.seed_release()
        lifecycle = RunLifecycle(self.dsn)
        receipt = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="upgrade-principal",
                session_id=None,
                task_kind="GENERATE_PLAN",
                input=StructuredProfileInput(
                    province="湖南",
                    application_year=2025,
                    category="物理类",
                    batch="本科批",
                    score=600,
                    subjects=("物理", "化学", "生物"),
                ),
                idempotency_key="upgrade-existing-run",
            )
        )
        result_version = uuid4()
        conn = await self._connect()
        try:
            await conn.execute(
                """
                INSERT INTO run_result (
                    result_version, run_id, release_id,
                    confirmed_profile_version, result_kind, payload,
                    verification_summary, unverified_conditions
                )
                VALUES (
                    %s, %s, %s, NULL, 'POLICY_EVIDENCE', %s, '[]'::jsonb,
                    '[]'::jsonb
                )
                """,
                (
                    result_version,
                    receipt.run_id,
                    release_id,
                    Jsonb(
                        {
                            "result_version": str(result_version),
                            "result_kind": "POLICY_EVIDENCE",
                            "retrieval_status": "NO_EVIDENCE",
                            "evidence": [],
                        }
                    ),
                ),
            )
            await conn.execute(
                """
                UPDATE run
                SET state = 'SUCCEEDED',
                    stage = 'EVIDENCE_READY',
                    result_version = %s,
                    result_kind = 'POLICY_EVIDENCE',
                    state_version = state_version + 1
                WHERE run_id = %s
                """,
                (result_version, receipt.run_id),
            )
            await self._apply_sql_file(
                conn,
                "migrations/27_formal_evidence_results.sql",
            )
            await self._apply_sql_file(
                conn,
                "migrations/28_authoritative_formal_gate.sql",
            )
            tables = await (
                await conn.execute(
                    """
                    SELECT tablename
                    FROM pg_tables
                    WHERE schemaname = 'public'
                      AND tablename IN (
                          'run_formal_gate_artifact',
                          'run_evidence_package',
                          'run_verification_report',
                          'run_result_state_event'
                      )
                    ORDER BY tablename
                    """
                )
            ).fetchall()
        finally:
            await conn.close()

        view = await lifecycle.GetRun(
            principal_id="upgrade-principal",
            run_id=receipt.run_id,
        )
        self.assertEqual(
            [row["tablename"] for row in tables],
            [
                "run_evidence_package",
                "run_formal_gate_artifact",
                "run_result_state_event",
                "run_verification_report",
            ],
        )
        self.assertEqual(view.state, "SUCCEEDED")
        self.assertEqual(view.result.result_version, result_version)
        self.assertEqual(view.result.result_kind, "POLICY_EVIDENCE")
        self.assertEqual(
            view.result_payload["retrieval_status"],
            "NO_EVIDENCE",
        )
        self.assertIsNone(view.formal_result)

    async def test_official_saver_tables_are_installed_without_reusing_plan_checkpoint(self):
        from app.run_lifecycle import RunLifecycle, StructuredProfileInput, SubmitRunCommand

        await self.reset_to_fresh_schema()
        await self.seed_release()
        lifecycle = RunLifecycle(self.dsn, auto_profile=True)
        self.addAsyncCleanup(lifecycle.close)
        await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="saver-schema-principal",
                session_id=None,
                task_kind="GENERATE_PLAN",
                input=StructuredProfileInput(
                    province="湖南",
                    application_year=2025,
                    category="物理类",
                    batch="本科批",
                    score=600,
                    subjects=("物理", "化学", "生物"),
                ),
                idempotency_key="saver-schema-submit",
            )
        )
        conn = await self._connect()
        try:
            rows = await (
                await conn.execute(
                    """
                    SELECT tablename
                    FROM pg_tables
                    WHERE schemaname = 'public'
                      AND tablename IN (
                        'checkpoint_migrations',
                        'checkpoints',
                        'checkpoint_blobs',
                        'checkpoint_writes'
                      )
                    ORDER BY tablename
                    """
                )
            ).fetchall()
            plan_checkpoint = await (
                await conn.execute(
                    """
                    SELECT to_regclass('public.plan_checkpoint') AS name,
                           (SELECT COUNT(*) FROM plan_checkpoint) AS row_count
                    """
                )
            ).fetchone()
        finally:
            await conn.close()

        self.assertEqual(
            [row["tablename"] for row in rows],
            [
                "checkpoint_blobs",
                "checkpoint_migrations",
                "checkpoint_writes",
                "checkpoints",
            ],
        )
        self.assertIsNotNone(plan_checkpoint["name"])
        self.assertEqual(plan_checkpoint["row_count"], 0)

    async def test_fresh_migrations_accept_supported_scope_without_test_seeding(self):
        from app.run_lifecycle import RunLifecycle, StructuredProfileInput, SubmitRunCommand
        from app.run_lifecycle.worker import RunLifecycleWorker

        await self.reset_to_fresh_schema()
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="fresh-scope-worker",
        )
        self.addAsyncCleanup(lifecycle.close)
        worker = RunLifecycleWorker(lifecycle, poll_interval=0.01)
        await worker.start()
        self.addAsyncCleanup(worker.stop)

        receipt = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="fresh-scope-principal",
                session_id=None,
                task_kind="GENERATE_PLAN",
                input=StructuredProfileInput(
                    province="湖南",
                    application_year=2025,
                    category="物理类",
                    batch="本科批",
                    score=600,
                    subjects=("物理", "化学", "生物"),
                ),
                idempotency_key="fresh-scope-submit",
            )
        )

        for _ in range(200):
            view = await lifecycle.GetRun(
                principal_id="fresh-scope-principal",
                run_id=receipt.run_id,
            )
            if view.state == "WAITING_HITL":
                break
            if view.state in {"EXPIRED", "SUCCEEDED", "FAILED", "CANCELLED"}:
                self.fail(
                    f"fresh schema 未进入画像确认: {view.state}/{view.terminal_reason}"
                )
            await asyncio.sleep(0.01)
        else:
            self.fail("fresh schema 未在超时内进入画像确认")

        self.assertEqual(view.release_id, "rel_legacy_v1")
        self.assertEqual(view.current_interrupt.kind, "PROFILE_CONFIRM")

    async def test_default_release_scope_migration_is_repeatable_for_history_category(self):
        from app.run_lifecycle import RunLifecycle, StructuredProfileInput, SubmitRunCommand
        from app.run_lifecycle.worker import RunLifecycleWorker

        await self.reset_to_fresh_schema()
        conn = await self._connect()
        try:
            await self._apply_sql_file(
                conn,
                "migrations/14_default_release_scope.sql",
            )
            await self._apply_sql_file(
                conn,
                "migrations/14_default_release_scope.sql",
            )
        finally:
            await conn.close()

        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="history-scope-worker",
        )
        self.addAsyncCleanup(lifecycle.close)
        worker = RunLifecycleWorker(lifecycle, poll_interval=0.01)
        await worker.start()
        self.addAsyncCleanup(worker.stop)

        receipt = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="history-scope-principal",
                session_id=None,
                task_kind="GENERATE_PLAN",
                input=StructuredProfileInput(
                    province="湖南",
                    application_year=2025,
                    category="历史类",
                    batch="本科批",
                    score=600,
                    subjects=("历史", "政治", "地理"),
                ),
                idempotency_key="history-scope-submit",
            )
        )

        for _ in range(200):
            view = await lifecycle.GetRun(
                principal_id="history-scope-principal",
                run_id=receipt.run_id,
            )
            if view.state == "WAITING_HITL":
                break
            if view.state in {"EXPIRED", "SUCCEEDED", "FAILED", "CANCELLED"}:
                self.fail(
                    f"历史类 fresh schema 未进入画像确认: "
                    f"{view.state}/{view.terminal_reason}"
                )
            await asyncio.sleep(0.01)
        else:
            self.fail("历史类 fresh schema 未在超时内进入画像确认")

        self.assertEqual(view.current_interrupt.kind, "PROFILE_CONFIRM")
