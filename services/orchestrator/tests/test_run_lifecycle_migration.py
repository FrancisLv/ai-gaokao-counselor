"""ticket #2 的 fresh schema 迁移验收。"""
from __future__ import annotations

from run_lifecycle_support import RunLifecyclePostgresTest


class FreshSchemaTests(RunLifecyclePostgresTest):
    async def test_run_lifecycle_works_on_fresh_schema_from_00_to_08(self):
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
