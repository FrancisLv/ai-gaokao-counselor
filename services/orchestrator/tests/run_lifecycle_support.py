"""RunLifecycle PostgreSQL 集成测试夹具。

仅在显式提供可写测试库时运行；库名必须包含 ``test``，避免误清业务数据库。
测试断言通过 RunLifecycle seam，数据库操作只负责建 schema 和准备不可变 release。
"""
from __future__ import annotations

import os
from pathlib import Path
import unittest
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row


TEST_DSN = os.getenv("ORCHESTRATOR_TEST_DSN")
ROOT = Path(__file__).resolve().parents[3]
BASE_SESSION_DDL = """
CREATE TABLE chat_session (
    id UUID PRIMARY KEY,
    user_id VARCHAR(100),
    profile JSONB,
    status VARCHAR(20) DEFAULT 'active',
    current_node VARCHAR(50),
    token_budget INTEGER DEFAULT 50000,
    token_used INTEGER DEFAULT 0,
    call_budget INTEGER DEFAULT 30,
    call_used INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    expired_at TIMESTAMPTZ
)
"""


@unittest.skipUnless(TEST_DSN, "ORCHESTRATOR_TEST_DSN 未设置")
class RunLifecyclePostgresTest(unittest.IsolatedAsyncioTestCase):
    dsn = TEST_DSN or ""

    async def asyncSetUp(self) -> None:
        await self._reset_schema()

    async def _connect(self):
        return await psycopg.AsyncConnection.connect(
            self.dsn,
            row_factory=dict_row,
            autocommit=True,
        )

    async def _reset_schema(self) -> None:
        conn = await self._connect()
        try:
            database = (await (await conn.execute("SELECT current_database() AS name")).fetchone())["name"]
            if "test" not in database.lower():
                self.fail(f"拒绝清理非测试数据库: {database}")
            await conn.execute(
                """
                DROP TABLE IF EXISTS
                    rule_coverage,
                    rule_version,
                    run_result,
                    run_interrupt,
                    profile_confirmation,
                    profile_revision,
                    run_event,
                    run_command_receipt,
                    run,
                    release_scope,
                    release,
                    chat_session,
                    checkpoint_writes,
                    checkpoint_blobs,
                    checkpoints,
                    checkpoint_migrations
                CASCADE
                """
            )
            await conn.execute(BASE_SESSION_DDL)
            migration = ROOT / "migrations" / "08_release_and_run_foundation.sql"
            for statement in migration.read_text(encoding="utf-8").split(";"):
                statement = statement.strip()
                if statement:
                    await conn.execute(statement)
            migration = ROOT / "migrations" / "09_profile_run_lifecycle.sql"
            for statement in migration.read_text(encoding="utf-8").split(";"):
                statement = statement.strip()
                if statement:
                    await conn.execute(statement)
            migration = ROOT / "migrations" / "10_run_leases.sql"
            for statement in migration.read_text(encoding="utf-8").split(";"):
                statement = statement.strip()
                if statement:
                    await conn.execute(statement)
            migration = ROOT / "migrations" / "11_algorithm_preview_results.sql"
            for statement in migration.read_text(encoding="utf-8").split(";"):
                statement = statement.strip()
                if statement:
                    await conn.execute(statement)
            migration = ROOT / "migrations" / "12_run_view_fields.sql"
            for statement in migration.read_text(encoding="utf-8").split(";"):
                statement = statement.strip()
                if statement:
                    await conn.execute(statement)
            has_release_dimension = await (
                await conn.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'university'
                          AND column_name = 'release_id'
                    ) AS present
                    """
                )
            ).fetchone()
            if has_release_dimension and has_release_dimension["present"]:
                pass
            else:
                migration = ROOT / "migrations" / "13_release_data_dimensions.sql"
                for statement in migration.read_text(encoding="utf-8").split(";"):
                    statement = statement.strip()
                    if statement:
                        await conn.execute(statement)
            migration = ROOT / "migrations" / "14_default_release_scope.sql"
            for statement in migration.read_text(encoding="utf-8").split(";"):
                statement = statement.strip()
                if statement:
                    await conn.execute(statement)
            migration = ROOT / "migrations" / "19_policy_evidence_results.sql"
            for statement in migration.read_text(encoding="utf-8").split(";"):
                statement = statement.strip()
                if statement:
                    await conn.execute(statement)
            migration = ROOT / "migrations" / "20_policy_relevance_policy.sql"
            for statement in migration.read_text(encoding="utf-8").split(";"):
                statement = statement.strip()
                if statement:
                    await conn.execute(statement)
            migration = ROOT / "migrations" / "21_eligibility_rules.sql"
            for statement in migration.read_text(encoding="utf-8").split(";"):
                statement = statement.strip()
                if statement:
                    await conn.execute(statement)
            migration = ROOT / "migrations" / "23_rule_expression_contract.sql"
            for statement in migration.read_text(encoding="utf-8").split(";"):
                statement = statement.strip()
                if statement:
                    await conn.execute(statement)
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS release_component (
                    release_id TEXT NOT NULL REFERENCES release(release_id),
                    component_type TEXT NOT NULL,
                    dataset_key TEXT NOT NULL,
                    component_version TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    PRIMARY KEY (release_id, component_type, dataset_key)
                )
                """
            )
        finally:
            await conn.close()

    async def _apply_sql_file(self, conn, relative_path: str) -> None:
        sql = (ROOT / relative_path).read_text(encoding="utf-8")
        for statement in sql.split(";"):
            statement = statement.strip()
            if statement:
                await conn.execute(statement)

    async def reset_to_fresh_schema(self) -> None:
        conn = await self._connect()
        try:
            database = (await (await conn.execute("SELECT current_database() AS name")).fetchone())["name"]
            if "test" not in database.lower():
                self.fail(f"拒绝清理非测试数据库: {database}")
            await conn.execute("DROP SCHEMA public CASCADE")
            await conn.execute("CREATE SCHEMA public")
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            await self._apply_sql_file(conn, "migrations/00_schema.sql")
            await self._apply_sql_file(conn, "migrations/07_policy_chunk_trgm.sql")
            await self._apply_sql_file(conn, "migrations/08_release_and_run_foundation.sql")
            await self._apply_sql_file(conn, "migrations/09_profile_run_lifecycle.sql")
            await self._apply_sql_file(conn, "migrations/10_run_leases.sql")
            await self._apply_sql_file(conn, "migrations/11_algorithm_preview_results.sql")
            await self._apply_sql_file(conn, "migrations/12_run_view_fields.sql")
            await self._apply_sql_file(conn, "migrations/13_release_data_dimensions.sql")
            await self._apply_sql_file(conn, "migrations/14_default_release_scope.sql")
            await self._apply_sql_file(conn, "migrations/15_policy_retrieval_release_scope.sql")
            await self._apply_sql_file(conn, "migrations/16_policy_vector_release_identity.sql")
            await self._apply_sql_file(conn, "migrations/17_policy_rerank_release_identity.sql")
            await self._apply_sql_file(conn, "migrations/18_policy_retrieval_config_identity.sql")
            await self._apply_sql_file(conn, "migrations/19_policy_evidence_results.sql")
            await self._apply_sql_file(conn, "migrations/20_policy_relevance_policy.sql")
            await self._apply_sql_file(conn, "migrations/21_eligibility_rules.sql")
            await self._apply_sql_file(conn, "migrations/23_rule_expression_contract.sql")
        finally:
            await conn.close()

    async def seed_release(
        self,
        *,
        status: str = "PUBLISHED",
        release_id: str | None = None,
    ) -> str:
        value = release_id or (
            "rel_legacy_v1" if status == "PUBLISHED" else f"rel-{uuid4().hex}"
        )
        conn = await self._connect()
        try:
            await conn.execute(
                """
                INSERT INTO release (release_id, status, manifest, manifest_sha256, published_at)
                VALUES (%s, %s, %s::jsonb, %s, CASE WHEN %s = 'PUBLISHED' THEN NOW() ELSE NULL END)
                ON CONFLICT (release_id) DO UPDATE
                SET status = EXCLUDED.status,
                    published_at = COALESCE(EXCLUDED.published_at, release.published_at)
                """,
                (value, status, '{"components": {}}', "test-sha256", status),
            )
            if status == "PUBLISHED":
                await conn.execute(
                    """
                    INSERT INTO release_scope (
                        release_id, province, application_year, category, batch
                    )
                    VALUES (%s, '湖南', 2025, '物理类', '本科批')
                    ON CONFLICT DO NOTHING
                    """,
                    (value,),
                )
        finally:
            await conn.close()
        return value

    async def age_events_outside_retention(self, run_id) -> None:
        conn = await self._connect()
        try:
            await conn.execute(
                """
                UPDATE run_event
                SET occurred_at = NOW() - INTERVAL '8 days'
                WHERE run_id = %s
                """,
                (run_id,),
            )
        finally:
            await conn.close()

    async def revoke_release(self, release_id: str) -> None:
        conn = await self._connect()
        try:
            await conn.execute(
                """
                UPDATE release
                SET status = 'REVOKED', revoked_at = NOW()
                WHERE release_id = %s
                """,
                (release_id,),
            )
        finally:
            await conn.close()

