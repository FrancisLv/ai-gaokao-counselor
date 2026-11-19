"""为 M4a 前端验收写入可重复的 policy release 与文档。"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import psycopg
from psycopg import conninfo
from policy_schema import reset_schema

EMBEDDING_MODEL = "acceptance-keyword"
INDEX_VERSION = "acceptance-index-v1"
ACCEPTANCE_YEAR = datetime.now(ZoneInfo("Asia/Shanghai")).year
SEED_VERSION = "m4a-frontend-v2"
RELEVANCE_POLICY_VERSION = "m4a-acceptance-relevance-v1"
MIN_VECTOR_SIMILARITY = 0.0
MIN_KEYWORD_SIMILARITY = 0.1


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _vector() -> str:
    return "[" + ",".join("0" for _ in range(1024)) + "]"


def _insert_document(
    conn,
    *,
    release_id: str,
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
    effective_year: int | None = None,
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
            %s, %s, %s, %s, %s, %s, NULL,
            %s, %s, %s, %s, NOW(), %s, %s
        )
        """,
        (
            doc_id,
            title,
            title,
            effective_year or ACCEPTANCE_YEAR,
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
        (release_id, doc_id, chunk_set_version),
    )


def seed(dsn: str, *, reset: bool = False) -> None:
    release_id = "rel_m4a_ui_" + _sha256(
        f"{SEED_VERSION}:{ACCEPTANCE_YEAR}"
    )[:24]
    doc_suffix = release_id.removeprefix("rel_m4a_ui_")
    manifest = {
        "purpose": "M4a frontend acceptance",
        "seed_version": SEED_VERSION,
        "application_year": ACCEPTANCE_YEAR,
        "embedding_model": EMBEDDING_MODEL,
        "index_version": INDEX_VERSION,
        "relevance_policy": {
            "policy_version": RELEVANCE_POLICY_VERSION,
            "min_vector_similarity": MIN_VECTOR_SIMILARITY,
            "min_keyword_similarity": MIN_KEYWORD_SIMILARITY,
        },
    }
    manifest_text = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    with psycopg.connect(dsn) as conn:
        if reset:
            reset_schema(conn)
        with conn.transaction():
            existing = conn.execute(
                "SELECT status FROM release WHERE release_id = %s",
                (release_id,),
            ).fetchone()
            if existing is not None:
                if existing[0] != "PUBLISHED":
                    raise SystemExit(
                        f"验收 release {release_id} 已存在但状态为 {existing[0]}"
                    )
                print(f"release_id={release_id}")
                print(f"application_year={ACCEPTANCE_YEAR}")
                print("query_found=转专业政策是什么？")
                print("query_national=全国学生资助政策")
                print("query_no_evidence=火星基地招生政策")
                return
            conn.execute(
                """
                INSERT INTO release (
                    release_id, status, manifest, manifest_sha256,
                    published_at, rrf_k
                )
                VALUES (%s, 'PUBLISHED', %s::jsonb, %s, %s, 60)
                """,
                (
                    release_id,
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
                VALUES (%s, '湖南', %s, '物理类', '本科批')
                """,
                (release_id, ACCEPTANCE_YEAR),
            )
            conn.execute(
                """
                INSERT INTO release_policy_index (
                    release_id, embedding_model, embedding_dim, index_version
                )
                VALUES (%s, %s, 1024, %s)
                """,
                (release_id, EMBEDDING_MODEL, INDEX_VERSION),
            )
            conn.execute(
                """
                INSERT INTO release_policy_relevance (
                    release_id, policy_version,
                    min_vector_similarity, min_keyword_similarity
                )
                VALUES (%s, %s, %s, %s)
                """,
                (
                    release_id,
                    RELEVANCE_POLICY_VERSION,
                    MIN_VECTOR_SIMILARITY,
                    MIN_KEYWORD_SIMILARITY,
                ),
            )
            _insert_document(
                conn,
                release_id=release_id,
                doc_id=f"doc_m4a_charter_{doc_suffix}",
                title=f"湖南测试大学{ACCEPTANCE_YEAR}年招生章程",
                document_type="charter",
                issuer_type="university",
                school_id="1001",
                regions=["湖南"],
                categories=["物理类"],
                batches=["本科批"],
                valid_from=f"{ACCEPTANCE_YEAR}-01-01",
                valid_to=f"{ACCEPTANCE_YEAR}-12-31",
                text=(
                    "第二章 转专业政策是什么？学生可以在第一学年结束前"
                    "申请转专业。"
                ),
            )
            _insert_document(
                conn,
                release_id=release_id,
                doc_id=f"doc_m4a_national_policy_{doc_suffix}",
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
                release_id=release_id,
                doc_id=f"doc_m4a_wrong_province_{doc_suffix}",
                title=f"湖北省测试大学{ACCEPTANCE_YEAR}年招生章程",
                document_type="charter",
                issuer_type="university",
                school_id="2002",
                regions=["湖北"],
                categories=["物理类"],
                batches=["本科批"],
                valid_from=f"{ACCEPTANCE_YEAR}-01-01",
                valid_to=f"{ACCEPTANCE_YEAR}-12-31",
                text="学生可以申请转专业，但仅适用于湖北考生。",
            )
            _insert_document(
                conn,
                release_id=release_id,
                doc_id=f"doc_m4a_expired_{doc_suffix}",
                title=f"湖南测试大学{ACCEPTANCE_YEAR - 1}年招生章程",
                document_type="charter",
                issuer_type="university",
                school_id="1001",
                regions=["湖南"],
                categories=["物理类"],
                batches=["本科批"],
                valid_from=f"{ACCEPTANCE_YEAR - 1}-01-01",
                valid_to=f"{ACCEPTANCE_YEAR - 1}-12-31",
                text=f"{ACCEPTANCE_YEAR - 1}年转专业政策已经失效。",
                effective_year=ACCEPTANCE_YEAR - 1,
            )
    print(f"release_id={release_id}")
    print(f"application_year={ACCEPTANCE_YEAR}")
    print("query_found=转专业政策是什么？")
    print("query_national=全国学生资助政策")
    print("query_no_evidence=火星基地招生政策")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--reset-schema", action="store_true")
    args = parser.parse_args()
    database_name = str(
        conninfo.conninfo_to_dict(args.dsn).get("dbname") or ""
    ).lower()
    if "test" not in database_name and "acceptance" not in database_name:
        raise SystemExit(
            "拒绝写入数据库名未包含 test/acceptance 的 DSN"
        )
    seed(args.dsn, reset=args.reset_schema)


if __name__ == "__main__":
    main()
