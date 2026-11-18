"""ticket #2 的 fresh schema 迁移验收。"""
from __future__ import annotations

from run_lifecycle_support import RunLifecyclePostgresTest


class FreshSchemaTests(RunLifecyclePostgresTest):
    async def test_run_lifecycle_works_on_fresh_schema_from_00_to_09(self):
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
