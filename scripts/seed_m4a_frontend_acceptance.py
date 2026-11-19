"""为 M4a 前端验收写入可重复的 policy release 与文档。"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from datetime import datetime, timezone

import psycopg

RELEASE_ID = "rel_m4a_frontend_acceptance"
EMBEDDING_MODEL = "acceptance-keyword"
INDEX_VERSION = "acceptance-index-v1"
ROOT = Path(__file__).resolve().parents[1]
SCHEMA_MIGRATIONS = (
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
)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _vector() -> str:
    return "[" + ",".join("0" for _ in range(1024)) + "]"


def reset_schema(conn) -> None:
    conn.execute("DROP SCHEMA public CASCADE")
    conn.execute("CREATE SCHEMA public")
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    for relative_path in SCHEMA_MIGRATIONS:
        sql = (ROOT / relative_path).read_text(encoding="utf-8")
        for statement in sql.split(";"):
            statement = statement.strip()
            if statement:
                conn.execute(statement)


def _insert_document(
    conn,
    *,
    doc_id: str,
    title: str,
    document_type: str,
    issuer_type: str,
    school_id: str | None,
    regions: list[str],
    categories: list[str],
    batches: list[str],
    valid_from: str,
    valid_to: str | None,
    text: str,
) -> None:
    content_sha256 = _sha256(f"{doc_id}:{text}")
    text_sha256 = _sha256(text)
    chunk_set_version = f"{doc_id}-chunks-v1"
    chunk_id = f"{doc_id}-chunk-1"
    conn.execute(
        """
        INSERT INTO policy_doc (
            id, title, issuer, effective_year, region, source_uri, raw_path,
            document_type, issuer_type, issuer_id, school_id, fetched_at,
            content_sha256, raw_storage_uri
        )
        VALUES (
            %s, %s, %s, 2025, %s, %s, NULL,
            %s, %s, %s, %s, NOW(), %s, %s
        )
        """,
        (
            doc_id,
            title,
            title,
            regions[0],
            f"https://example.invalid/{doc_id}",
            document_type,
            issuer_type,
            f"issuer-{doc_id}",
            school_id,
            content_sha256,
            f"s3://acceptance/{doc_id}.txt",
        ),
    )
    conn.execute(
        """
        INSERT INTO policy_chunk_set (
            chunk_set_version, doc_id, content_sha256, embedding_model,
            embedding_dim, index_version
        )
        VALUES (%s, %s, %s, %s, 1024, %s)
        """,
        (
            chunk_set_version,
            doc_id,
            content_sha256,
            EMBEDDING_MODEL,
            INDEX_VERSION,
        ),
    )
    conn.execute(
        """
        INSERT INTO policy_chunk (
            id, doc_id, chunk_set_version, section_path, text, text_sha256,
            char_start, char_end, embedding, embedding_model, embedding_dim,
            index_version
        )
        VALUES (
            %s, %s, %s, '第二章 > 政策说明', %s, %s,
            0, %s, %s::vector, %s, 1024, %s
        )
        """,
        (
            chunk_id,
            doc_id,
            chunk_set_version,
            text,
            text_sha256,
            len(text),
            _vector(),
            EMBEDDING_MODEL,
            INDEX_VERSION,
        ),
    )
    conn.execute(
        """
        INSERT INTO policy_applicability (
            doc_id, regions, categories, batches, valid_from, valid_to
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            doc_id,
            regions,
            categories,
            batches,
            valid_from,
            valid_to,
        ),
    )
    conn.execute(
        """
        INSERT INTO release_policy_doc (
            release_id, doc_id, chunk_set_version
        )
        VALUES (%s, %s, %s)
        """,
        (RELEASE_ID, doc_id, chunk_set_version),
    )


def seed(dsn: str, *, reset: bool = False) -> None:
    document_ids = (
        "doc_m4a_charter",
        "doc_m4a_national_policy",
        "doc_m4a_wrong_province",
        "doc_m4a_expired",
    )
    with psycopg.connect(dsn) as conn:
        if reset:
            reset_schema(conn)
        with conn.transaction():
            conn.execute(
                "DELETE FROM release_policy_doc WHERE release_id = %s",
                (RELEASE_ID,),
            )
            conn.execute(
                "DELETE FROM release WHERE release_id = %s",
                (RELEASE_ID,),
            )
            conn.execute(
                "DELETE FROM policy_applicability WHERE doc_id = ANY(%s)",
                (list(document_ids),),
            )
            conn.execute(
                "DELETE FROM policy_chunk WHERE doc_id = ANY(%s)",
                (list(document_ids),),
            )
            conn.execute(
                "DELETE FROM policy_chunk_set WHERE doc_id = ANY(%s)",
                (list(document_ids),),
            )
            conn.execute(
                "DELETE FROM policy_doc WHERE id = ANY(%s)",
                (list(document_ids),),
            )

            manifest = {
                "purpose": "M4a frontend acceptance",
                "embedding_model": EMBEDDING_MODEL,
                "index_version": INDEX_VERSION,
            }
            manifest_text = json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            conn.execute(
                """
                INSERT INTO release (
                    release_id, status, manifest, manifest_sha256,
                    published_at, rrf_k
                )
                VALUES (%s, 'PUBLISHED', %s::jsonb, %s, %s, 60)
                """,
                (
                    RELEASE_ID,
                    manifest_text,
                    _sha256(manifest_text),
                    datetime.now(timezone.utc),
                ),
            )
            conn.execute(
                """
                INSERT INTO release_scope (
                    release_id, province, application_year, category, batch
                )
                VALUES (%s, '湖南', 2025, '物理类', '本科批')
                """,
                (RELEASE_ID,),
            )
            conn.execute(
                """
                INSERT INTO release_policy_index (
                    release_id, embedding_model, embedding_dim, index_version
                )
                VALUES (%s, %s, 1024, %s)
                """,
                (RELEASE_ID, EMBEDDING_MODEL, INDEX_VERSION),
            )
            _insert_document(
                conn,
                doc_id="doc_m4a_charter",
                title="湖南测试大学2025年招生章程",
                document_type="charter",
                issuer_type="university",
                school_id="1001",
                regions=["湖南"],
                categories=["物理类"],
                batches=["本科批"],
                valid_from="2025-01-01",
                valid_to=None,
                text=(
                    "第二章 转专业政策是什么？学生可以在第一学年结束前"
                    "申请转专业。"
                ),
            )
            _insert_document(
                conn,
                doc_id="doc_m4a_national_policy",
                title="全国学生资助政策",
                document_type="policy",
                issuer_type="national",
                school_id=None,
                regions=["全国"],
                categories=["不限"],
                batches=["不限"],
                valid_from="2020-01-01",
                valid_to=None,
                text=(
                    "全国学生资助政策：家庭经济困难学生可以按规定"
                    "申请国家资助。"
                ),
            )
            _insert_document(
                conn,
                doc_id="doc_m4a_wrong_province",
                title="湖北省测试大学招生章程",
                document_type="charter",
                issuer_type="university",
                school_id="2002",
                regions=["湖北"],
                categories=["物理类"],
                batches=["本科批"],
                valid_from="2025-01-01",
                valid_to="2025-12-31",
                text="学生可以申请转专业，但仅适用于湖北考生。",
            )
            _insert_document(
                conn,
                doc_id="doc_m4a_expired",
                title="湖南测试大学2024年招生章程",
                document_type="charter",
                issuer_type="university",
                school_id="1001",
                regions=["湖南"],
                categories=["物理类"],
                batches=["本科批"],
                valid_from="2024-01-01",
                valid_to="2024-12-31",
                text="2024年转专业政策已经失效。",
            )
    print(f"release_id={RELEASE_ID}")
    print("query_found=转专业政策是什么？")
    print("query_national=全国学生资助政策")
    print("query_no_evidence=火星基地招生政策")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--reset-schema", action="store_true")
    args = parser.parse_args()
    if "test" not in args.dsn.lower() and "acceptance" not in args.dsn.lower():
        raise SystemExit(
            "拒绝写入未声明 test/acceptance 的数据库 DSN"
        )
    seed(args.dsn, reset=args.reset_schema)


if __name__ == "__main__":
    main()
