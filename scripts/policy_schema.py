"""M4a policy 验收 schema 的共享迁移 helper。"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = (
    "migrations/00_schema.sql",
    "migrations/07_policy_chunk_trgm.sql",
    "migrations/08_release_and_run_foundation.sql",
    "migrations/09_profile_run_lifecycle.sql",
    "migrations/10_run_leases.sql",
    "migrations/11_algorithm_preview_results.sql",
    "migrations/12_run_view_fields.sql",
    "migrations/13_release_data_dimensions.sql",
    "migrations/14_default_release_scope.sql",
    "migrations/15_policy_retrieval_release_scope.sql",
    "migrations/16_policy_vector_release_identity.sql",
    "migrations/17_policy_rerank_release_identity.sql",
    "migrations/18_policy_retrieval_config_identity.sql",
    "migrations/19_policy_evidence_results.sql",
    "migrations/20_policy_relevance_policy.sql",
    "migrations/21_eligibility_rules.sql",
    "migrations/22_rule_lifecycle.sql",
    "migrations/23_rule_expression_contract.sql",
    "migrations/24_eligibility_profile_fields.sql",
    "migrations/25_rule_coverage_contract.sql",
    "migrations/26_release_bound_profile_fields.sql",
    "migrations/44_macro_region_catalog.sql",
)


def apply_sql_file(conn, relative_path: str) -> None:
    sql = (ROOT / relative_path).read_text(encoding="utf-8")
    conn.execute(sql)


def reset_schema(conn) -> None:
    conn.execute("DROP SCHEMA public CASCADE")
    conn.execute("CREATE SCHEMA public")
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    for migration in MIGRATIONS:
        apply_sql_file(conn, migration)
