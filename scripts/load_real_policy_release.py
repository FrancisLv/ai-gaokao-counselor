"""把主库真实 2025 招生章程升级为测试库中的 release-bound 索引。

主库文档和 embedding 由 ``scripts/ingest_charters.py`` 写入。
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg import conninfo
from psycopg.types.json import Jsonb

from policy_schema import reset_schema

EMBEDDING_MODEL = "BAAI/bge-large-zh-v1.5"
EMBEDDING_DIM = 1024
INDEX_VERSION = "idx_bge_large_zh_v1_5_2025_v1"
POLICY_YEAR = 2025
MAX_CHUNK_CHARS = 600
RELEVANCE_POLICY_VERSION = "m4a-relevance-v1"
MIN_VECTOR_SIMILARITY = 0.5
MIN_KEYWORD_SIMILARITY = 0.1


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _copy_query(source, target, query: str, target_spec: str) -> None:
    with source.cursor().copy(
        f"COPY ({query}) TO STDOUT WITH (FORMAT binary)"
    ) as source_copy:
        with target.cursor().copy(
            f"COPY {target_spec} FROM STDIN WITH (FORMAT binary)"
        ) as target_copy:
            for block in source_copy:
                target_copy.write(block)


def _executemany(conn, sql: str, rows) -> None:
    with conn.cursor() as cursor:
        cursor.executemany(sql, rows)


def _safe_target_database(dsn: str) -> str:
    database = str(conninfo.conninfo_to_dict(dsn).get("dbname") or "").lower()
    if "test" not in database and "acceptance" not in database:
        raise SystemExit(
            "拒绝写入数据库名未包含 test/acceptance 的 DSN"
        )
    return database


def load(
    *,
    source_dsn: str,
    target_dsn: str,
) -> None:
    _safe_target_database(target_dsn)
    source = psycopg.connect(source_dsn)
    target = psycopg.connect(target_dsn)
    try:
        target.autocommit = False
        reset_schema(target)
        _copy_query(
            source,
            target,
            """
            SELECT id, title, issuer, effective_year, region,
                   source_uri, raw_path
            FROM policy_doc
            ORDER BY id
            """,
            (
                "policy_doc (id, title, issuer, effective_year, region, "
                "source_uri, raw_path)"
            ),
        )
        _copy_query(
            source,
            target,
            """
            SELECT id, doc_id, section_path, text, meta, embedding, tsv
            FROM policy_chunk
            ORDER BY doc_id, id
            """,
            (
                "policy_chunk "
                "(id, doc_id, section_path, text, meta, embedding, tsv)"
            ),
        )
        invalid_embeddings = target.execute(
            """
            SELECT count(*)
            FROM policy_chunk
            WHERE embedding IS NULL
               OR vector_dims(embedding) <> %s
            """,
            (EMBEDDING_DIM,),
        ).fetchone()[0]
        if invalid_embeddings:
            raise RuntimeError(
                "源库存在缺失或维度不匹配的 embedding："
                f"expected_dim={EMBEDDING_DIM}, rows={invalid_embeddings}"
            )
        target.execute(
            """
            DELETE FROM policy_chunk
            WHERE char_length(text) NOT BETWEEN 1 AND %s
            """,
            (MAX_CHUNK_CHARS,),
        )

        docs = target.execute(
            """
            SELECT id, title, issuer, raw_path
            FROM policy_doc
            ORDER BY id
            """
        ).fetchall()
        release_entries = []
        total_chunks = 0
        skipped_docs = 0
        for doc_id, title, issuer, raw_path_value in docs:
            raw_path = Path(raw_path_value)
            raw_bytes = raw_path.read_bytes()
            content_sha256 = _sha256_bytes(raw_bytes)
            meta_path = raw_path.with_suffix(".json")
            source_meta = (
                json.loads(meta_path.read_text(encoding="utf-8"))
                if meta_path.exists()
                else {}
            )
            school_id = str(
                source_meta.get("school_id")
                or f"legacy-{_sha256_text(issuer or title)[:12]}"
            )
            fetched_at = datetime.fromtimestamp(
                raw_path.stat().st_mtime,
                tz=timezone.utc,
            )
            target.execute(
                """
                UPDATE policy_doc
                SET region = '全国',
                    document_type = 'charter',
                    issuer_type = 'university',
                    issuer_id = %s,
                    school_id = %s,
                    fetched_at = %s,
                    content_sha256 = %s,
                    raw_storage_uri = %s
                WHERE id = %s
                """,
                (
                    school_id,
                    school_id,
                    fetched_at,
                    content_sha256,
                    raw_path.resolve().as_uri(),
                    doc_id,
                ),
            )
            chunks = target.execute(
                """
                SELECT id, text
                FROM policy_chunk
                WHERE doc_id = %s
                ORDER BY id
                """,
                (doc_id,),
            ).fetchall()
            if not chunks:
                skipped_docs += 1
                continue
            chunk_set_version = "cs_" + _sha256_text(
                "|".join(
                    (
                        doc_id,
                        content_sha256,
                        EMBEDDING_MODEL,
                        INDEX_VERSION,
                    )
                )
            )[:24]
            target.execute(
                """
                INSERT INTO policy_chunk_set (
                    chunk_set_version, doc_id, content_sha256,
                    embedding_model, embedding_dim, index_version
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    chunk_set_version,
                    doc_id,
                    content_sha256,
                    EMBEDDING_MODEL,
                    EMBEDDING_DIM,
                    INDEX_VERSION,
                ),
            )
            _executemany(
                target,
                """
                UPDATE policy_chunk
                SET text_sha256 = %s,
                    embedding_model = %s,
                    embedding_dim = %s,
                    index_version = %s,
                    chunk_set_version = %s
                WHERE id = %s
                """,
                [
                    (
                        _sha256_text(text),
                        EMBEDDING_MODEL,
                        EMBEDDING_DIM,
                        INDEX_VERSION,
                        chunk_set_version,
                        chunk_id,
                    )
                    for chunk_id, text in chunks
                ],
            )
            target.execute(
                """
                INSERT INTO policy_applicability (
                    doc_id, regions, categories, batches,
                    valid_from, valid_to
                )
                VALUES (
                    %s, ARRAY['全国'], ARRAY['不限'], ARRAY['不限'],
                    DATE '2025-01-01', DATE '2025-12-31'
                )
                """,
                (doc_id,),
            )
            release_entries.append(
                {
                    "doc_id": doc_id,
                    "content_sha256": content_sha256,
                    "chunk_set_version": chunk_set_version,
                    "chunk_count": len(chunks),
                }
            )
            total_chunks += len(chunks)

        manifest = {
            "purpose": "M4a real 2025 charter validation",
            "source_dataset": "docs/charters/2025",
            "embedding_model": EMBEDDING_MODEL,
            "embedding_dim": EMBEDDING_DIM,
            "index_version": INDEX_VERSION,
            "max_chunk_chars": MAX_CHUNK_CHARS,
            "relevance_policy": {
                "policy_version": RELEVANCE_POLICY_VERSION,
                "min_vector_similarity": MIN_VECTOR_SIMILARITY,
                "min_keyword_similarity": MIN_KEYWORD_SIMILARITY,
            },
            "document_count": len(release_entries),
            "chunk_count": total_chunks,
            "documents": release_entries,
        }
        manifest_text = json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        release_id = "rel_real_2025_" + _sha256_text(manifest_text)[:24]
        target.execute(
            """
            INSERT INTO release (
                release_id, status, manifest, manifest_sha256,
                published_at, rrf_k
            )
            VALUES (%s, 'PUBLISHED', %s, %s, %s, 60)
            """,
            (
                release_id,
                Jsonb(manifest),
                _sha256_text(manifest_text),
                datetime.now(timezone.utc),
            ),
        )
        target.execute(
            "SELECT ensure_region_preset_catalog(%s)",
            (release_id,),
        )
        _executemany(
            target,
            """
            INSERT INTO release_scope (
                release_id, province, application_year, category, batch
            )
            VALUES (%s, '湖南', %s, %s, '本科批')
            """,
            (
                (release_id, POLICY_YEAR, "物理类"),
                (release_id, POLICY_YEAR, "历史类"),
            ),
        )
        target.execute(
            """
            INSERT INTO release_policy_index (
                release_id, embedding_model, embedding_dim, index_version
            )
            VALUES (%s, %s, %s, %s)
            """,
            (
                release_id,
                EMBEDDING_MODEL,
                EMBEDDING_DIM,
                INDEX_VERSION,
            ),
        )
        target.execute(
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
        _executemany(
            target,
            """
            INSERT INTO release_policy_doc (
                release_id, doc_id, chunk_set_version
            )
            VALUES (%s, %s, %s)
            """,
            [
                (
                    release_id,
                    entry["doc_id"],
                    entry["chunk_set_version"],
                )
                for entry in release_entries
            ],
        )
        target.execute(
            """
            INSERT INTO release_component (
                release_id, component_type, dataset_key,
                component_version, content_sha256
            )
            VALUES
                (%s, 'policy_documents', 'policy-2025', %s, %s),
                (%s, 'policy_embedding_index', 'bge-large-zh-v1.5', %s, %s),
                (
                    %s,
                    'policy_relevance',
                    'm4a-relevance',
                    %s,
                    %s
                )
            """,
            (
                release_id,
                INDEX_VERSION,
                _sha256_text(manifest_text),
                release_id,
                INDEX_VERSION,
                _sha256_text(
                    f"{EMBEDDING_MODEL}:{EMBEDDING_DIM}:{INDEX_VERSION}"
                ),
                release_id,
                RELEVANCE_POLICY_VERSION,
                _sha256_text(
                    f"{RELEVANCE_POLICY_VERSION}:"
                    f"{MIN_VECTOR_SIMILARITY}:{MIN_KEYWORD_SIMILARITY}"
                ),
            ),
        )
        target.commit()
    except Exception:
        target.rollback()
        raise
    finally:
        source.close()
        target.close()

    print(f"release_id={release_id}")
    print(f"policy_year={POLICY_YEAR}")
    print(f"documents={len(release_entries)}")
    print(f"chunks={total_chunks}")
    print(f"skipped_documents_without_valid_chunks={skipped_docs}")
    print("retrieval_as_of=2025-11-19")
    print(f"relevance_policy_version={RELEVANCE_POLICY_VERSION}")
    print(f"min_vector_similarity={MIN_VECTOR_SIMILARITY}")
    print(f"min_keyword_similarity={MIN_KEYWORD_SIMILARITY}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-dsn",
        default="postgresql://francis@localhost:5432/ai_gaokao_counselor",
    )
    parser.add_argument(
        "--target-dsn",
        default="postgresql://francis@localhost:5432/ai_gaokao_ticket2_test",
    )
    args = parser.parse_args()
    load(
        source_dsn=args.source_dsn,
        target_dsn=args.target_dsn,
    )


if __name__ == "__main__":
    main()
