"""ticket #2 的 fresh schema 迁移验收。"""
from __future__ import annotations

import asyncio

from run_lifecycle_support import RunLifecyclePostgresTest


class FreshSchemaTests(RunLifecyclePostgresTest):
    async def test_run_lifecycle_works_on_fresh_schema_from_00_to_21(self):
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
