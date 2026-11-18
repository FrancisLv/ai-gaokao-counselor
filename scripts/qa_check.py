#!/usr/bin/env python3
"""入库质检脚本：统计 chunk 数量、embedding 非空率、抽样检查 section_path。

用法：
    python scripts/qa_check.py
    python scripts/qa_check.py --dsn "postgres://user:pass@host:5432/gaokao"
    python scripts/qa_check.py --samples 20
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("qa_check")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DSN = "postgres://app:app@localhost:5433/gaokao?sslmode=disable"  # compose PG 宿主机端口 5433（避开本机 PG 的 5432）


def run_qa(dsn: str, n_samples: int = 10):
    conn = psycopg.connect(dsn, row_factory=dict_row)

    # ---------- 1. 基础统计 ----------
    print("\n" + "=" * 60)
    print("📊 招生章程入库质检报告")
    print("=" * 60)

    with conn.cursor() as cur:
        # 文档总数
        cur.execute("SELECT COUNT(*) AS cnt FROM policy_doc")
        doc_count = cur.fetchone()["cnt"]
        print(f"\n📄 文档总数: {doc_count}")

        # chunk 总数
        cur.execute("SELECT COUNT(*) AS cnt FROM policy_chunk")
        chunk_count = cur.fetchone()["cnt"]
        print(f"📦 Chunk 总数: {chunk_count}")

        if chunk_count == 0:
            print("\n⚠️  没有 chunk 数据，请先运行 ingest_charters.py")
            conn.close()
            return

        # 平均每文档 chunk 数
        avg = chunk_count / max(doc_count, 1)
        print(f"📏 平均每文档 chunk 数: {avg:.1f}")

    # ---------- 2. Embedding 非空率 ----------
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                COUNT(*) AS total,
                COUNT(embedding) AS non_null,
                COUNT(*) - COUNT(embedding) AS null_cnt
            FROM policy_chunk
        """)
        row = cur.fetchone()
        total = row["total"]
        non_null = row["non_null"]
        null_cnt = row["null_cnt"]
        rate = non_null / max(total, 1) * 100

        print(f"\n🔢 Embedding 非空率: {non_null}/{total} ({rate:.1f}%)")
        if null_cnt > 0:
            print(f"   ⚠️  有 {null_cnt} 个 chunk 缺少 embedding")

    # ---------- 3. 向量维度检查 ----------
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) AS cnt
            FROM policy_chunk
            WHERE embedding IS NOT NULL AND vector_dims(embedding) != 1024
        """)
        wrong_dim = cur.fetchone()["cnt"]
        if wrong_dim > 0:
            print(f"   ❌ 有 {wrong_dim} 个 chunk 维度不是 1024")
        else:
            print(f"   ✅ 所有 embedding 维度均为 1024")

    # ---------- 4. section_path 抽样检查 ----------
    print(f"\n🔍 section_path 抽样检查（随机 {n_samples} 条）:")
    print("-" * 60)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT c.id, c.section_path, c.doc_id, d.issuer, d.effective_year,
                   LENGTH(c.text) AS text_len, c.meta
            FROM policy_chunk c
            JOIN policy_doc d ON c.doc_id = d.id
            ORDER BY RANDOM()
            LIMIT %s
        """, (n_samples,))
        samples = cur.fetchall()

        for i, row in enumerate(samples, 1):
            print(f"\n  [{i}] doc_id={row['doc_id']}")
            print(f"      学校: {row['issuer']} ({row['effective_year']})")
            print(f"      section_path: {row['section_path']}")
            print(f"      text_len: {row['text_len']} 字符")
            meta = row.get("meta", {})
            if meta:
                print(f"      meta.school_name: {meta.get('school_name', 'N/A')}")
                print(f"      meta.year: {meta.get('year', 'N/A')}")

    # ---------- 5. section_path 格式检查 ----------
    print(f"\n📋 section_path 格式检查:")
    with conn.cursor() as cur:
        cur.execute("""
            SELECT section_path, COUNT(*) AS cnt
            FROM policy_chunk
            WHERE section_path NOT LIKE '%>%'
            GROUP BY section_path
            ORDER BY cnt DESC
            LIMIT 10
        """)
        bad_paths = cur.fetchall()
        if bad_paths:
            print(f"   ⚠️  有 {sum(r['cnt'] for r in bad_paths)} 个 chunk 的 section_path 不含 '>' 分隔符:")
            for row in bad_paths[:5]:
                print(f"      '{row['section_path']}' ({row['cnt']} 次)")
        else:
            print(f"   ✅ 所有 section_path 均包含层级路径")

    # ---------- 6. 文本长度分布 ----------
    print(f"\n📏 Chunk 文本长度分布:")
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                MIN(LENGTH(text)) AS min_len,
                MAX(LENGTH(text)) AS max_len,
                AVG(LENGTH(text))::int AS avg_len,
                PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY LENGTH(text)) AS median_len,
                PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY LENGTH(text)) AS p95_len
            FROM policy_chunk
        """)
        row = cur.fetchone()
        print(f"   最小: {row['min_len']} 字符")
        print(f"   最大: {row['max_len']} 字符")
        print(f"   平均: {row['avg_len']} 字符")
        print(f"   中位数: {row['median_len']} 字符")
        print(f"   P95: {row['p95_len']} 字符")

    # ---------- 7. 学校覆盖检查 ----------
    print(f"\n🏫 学校覆盖:")
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(DISTINCT issuer) AS cnt FROM policy_doc")
        school_count = cur.fetchone()["cnt"]
        print(f"   入库学校数: {school_count}")

    print("\n" + "=" * 60)
    print("✅ 质检完成")
    print("=" * 60 + "\n")

    conn.close()


def main():
    parser = argparse.ArgumentParser(description="招生章程入库质检")
    parser.add_argument("--dsn", default=DEFAULT_DSN, help="PostgreSQL 连接字符串")
    parser.add_argument("--samples", type=int, default=10, help="抽样检查条数")
    args = parser.parse_args()
    run_qa(args.dsn, args.samples)


if __name__ == "__main__":
    main()
