#!/usr/bin/env python3
"""招生章程入库脚本（幂等，支持增量）。

功能：
1. 遍历 docs/charters/2025/ 下所有 JSON+TXT 对
2. 解析 → 切分 → embedding → 写入 policy_doc + policy_chunk 表
3. 断点续传：已存在的 doc_id 跳过
4. 进度条显示

用法：
    # 默认连接本地 PG
    python scripts/ingest_charters.py

    # 指定 DSN
    python scripts/ingest_charters.py --dsn "postgres://user:pass@host:5432/gaokao"

    # 只处理前 N 个文件（调试用）
    python scripts/ingest_charters.py --limit 10

    # 指定章程目录
    python scripts/ingest_charters.py --dir docs/charters/2025
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
import uuid
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from tqdm import tqdm

# 将 services/orchestrator 加入 path，以便 import rag 模块
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "services" / "orchestrator"))

from rag.etl import DocMeta, Chunk, iter_charter_files, process_single_file
from rag.embedder import Embedder

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ingest")

# ---------- 默认配置 ----------

DEFAULT_DSN = "postgres://app:app@localhost:5433/gaokao?sslmode=disable"  # compose PG 宿主机端口 5433（避开本机 PG 的 5432）
DEFAULT_CHARTER_DIR = str(PROJECT_ROOT / "docs" / "charters" / "2025")
BATCH_EMBED_SIZE = 32


def doc_id_from_meta(meta: DocMeta) -> str:
    """根据元数据生成稳定的 doc_id。"""
    raw = f"{meta.school_name}_{meta.year}_{meta.title}"
    h = hashlib.sha256(raw.encode()).hexdigest()[:12]
    return f"doc_{meta.year}_{h}"


def chunk_id(doc_id: str, idx: int) -> str:
    return f"{doc_id}_c{idx:04d}"


def ensure_meta_index(conn: psycopg.Connection):
    """确保 policy_chunk.meta 上有 GIN 索引（用于检索时硬过滤）。"""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_chunk_meta
            ON policy_chunk USING gin (meta);
        """)
    conn.commit()


def doc_exists(conn: psycopg.Connection, doc_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM policy_doc WHERE id = %s", (doc_id,))
        return cur.fetchone() is not None


def insert_doc(conn: psycopg.Connection, doc_id: str, meta: DocMeta, raw_path: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO policy_doc (id, title, issuer, effective_year, source_uri, raw_path)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (doc_id, meta.title, meta.school_name, meta.year, meta.url, raw_path),
        )
    conn.commit()


def insert_chunks(
    conn: psycopg.Connection,
    doc_id: str,
    chunks: list[Chunk],
    embeddings,
):
    """批量写入 policy_chunk。"""
    rows = []
    for i, (chunk, emb) in enumerate(zip(chunks, embeddings)):
        cid = chunk_id(doc_id, i)
        rows.append((
            cid,
            doc_id,
            chunk.section_path,
            chunk.text,
            chunk.meta,
            emb.tolist(),
        ))

    with conn.cursor() as cur:
        # 先删除该 doc 的旧 chunks（幂等）
        cur.execute("DELETE FROM policy_chunk WHERE doc_id = %s", (doc_id,))
        cur.executemany(
            """
            INSERT INTO policy_chunk (id, doc_id, section_path, text, meta, embedding, tsv)
            VALUES (%s, %s, %s, %s, %s, %s, to_tsvector('simple', %s))
            """,
            [(r[0], r[1], r[2], r[3], psycopg.types.json.Json(r[4]), r[5], r[3]) for r in rows],
        )
    conn.commit()


def run(args):
    charter_dir = Path(args.dir)
    if not charter_dir.exists():
        logger.error("章程目录不存在: %s", charter_dir)
        sys.exit(1)

    # 加载 embedding 模型
    logger.info("加载 embedding 模型...")
    embedder = Embedder(device=args.device)
    logger.info("模型加载完成，维度=%d", embedder.dim)

    # 连接数据库
    logger.info("连接数据库: %s", args.dsn.split("@")[-1].split("?")[0])
    conn = psycopg.connect(args.dsn, autocommit=False)
    ensure_meta_index(conn)

    # 遍历文件
    files = list(iter_charter_files(charter_dir))
    if args.limit:
        files = files[: args.limit]
    logger.info("共发现 %d 个章程文件对", len(files))

    stats = {"total_docs": 0, "total_chunks": 0, "skipped": 0, "errors": 0}

    for json_path, txt_path in tqdm(files, desc="入库进度"):
        try:
            meta, chunks = process_single_file(json_path, txt_path)
        except Exception as e:
            logger.error("解析失败 %s: %s", json_path.name, e)
            stats["errors"] += 1
            continue

        if not chunks:
            logger.warning("切分结果为空: %s", json_path.name)
            stats["skipped"] += 1
            continue

        did = doc_id_from_meta(meta)

        # 断点续传：跳过已存在的文档
        if not args.force and doc_exists(conn, did):
            stats["skipped"] += 1
            continue

        # 写入 policy_doc
        insert_doc(conn, did, meta, str(txt_path))

        # 批量 embedding
        texts = [c.text for c in chunks]
        embeddings = embedder.encode(texts, batch_size=BATCH_EMBED_SIZE)

        # 写入 policy_chunk
        insert_chunks(conn, did, chunks, embeddings)

        stats["total_docs"] += 1
        stats["total_chunks"] += len(chunks)

        tqdm.write(f"  ✓ {meta.school_name} ({meta.year}): {len(chunks)} chunks")

    conn.close()

    # 输出统计
    logger.info("=" * 50)
    logger.info("入库完成:")
    logger.info("  新增文档: %d", stats["total_docs"])
    logger.info("  新增 chunks: %d", stats["total_chunks"])
    logger.info("  跳过（已存在）: %d", stats["skipped"])
    logger.info("  错误: %d", stats["errors"])


def main():
    parser = argparse.ArgumentParser(description="招生章程 ETL 入库脚本")
    parser.add_argument(
        "--dsn",
        default=DEFAULT_DSN,
        help="PostgreSQL 连接字符串",
    )
    parser.add_argument(
        "--dir",
        default=DEFAULT_CHARTER_DIR,
        help="章程文件目录",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="只处理前 N 个文件（调试用，0=全部）",
    )
    parser.add_argument(
        "--device",
        default=None,
        help="embedding 模型设备 (cpu/cuda/mps)，默认自动检测",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="强制重新入库（忽略已存在的文档）",
    )
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
