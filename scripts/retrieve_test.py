#!/usr/bin/env python3
"""检索验证脚本：向量 ‖ 关键词（pg_trgm）‖ hybrid（RRF 融合）三种模式。

用法：
    python scripts/retrieve_test.py                                  # 交互模式（hybrid）
    python scripts/retrieve_test.py --query "清华大学转专业政策" --top 5
    python scripts/retrieve_test.py --query "东莞理工学院录取规则" --school "东莞理工学院"
    python scripts/retrieve_test.py --query "色弱限报" --mode keyword   # 不加载 embedding 模型
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 把项目根目录加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services" / "orchestrator"))

import psycopg

from rag.retrieve import HybridRetriever, RetrievedChunk, extract_terms

DEFAULT_DSN = "postgresql://francis@localhost:5432/ai_gaokao_counselor"


def print_results(query: str, results: list[RetrievedChunk], mode: str):
    """格式化打印检索结果。"""
    print()
    print("=" * 70)
    print(f"🔍 查询: {query}  (mode={mode})")
    if mode in ("hybrid", "keyword"):
        print(f"   关键词: {extract_terms(query)}")
    print("=" * 70)

    if not results:
        print("  ❌ 无结果")
        return

    for i, r in enumerate(results, 1):
        print(f"\n  [{i}] RRF: {r.rrf_score:.5f}")
        if r.vector_rank is not None:
            # 归一化向量的余弦相似度 = 1 - L2距离²/2
            similarity = 1 - (r.vector_distance ** 2) / 2
            print(f"      向量路: rank={r.vector_rank} 相似度={similarity:.4f}")
        if r.keyword_rank is not None:
            print(f"      关键词路: rank={r.keyword_rank} sim={r.keyword_sim:.4f}")
        print(f"      学校: {r.school_name} ({r.year})")
        print(f"      路径: {r.section_path}")
        print(f"      内容:")
        for line in (r.text or "").splitlines():
            print(f"        {line}")

    print()
    print("=" * 70)


def run_interactive(retriever: HybridRetriever, filters: dict | None, mode: str, top: int):
    """交互式检索模式。"""
    print(f"\n🔍 招生章程混合检索验证（mode={mode}，输入 quit 退出）")
    print("-" * 50)

    while True:
        try:
            query = input("\n请输入查询: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break

        if not query or query.lower() in ("quit", "exit", "q"):
            print("再见！")
            break

        results = retriever.search(query, top_k=top, filters=filters, mode=mode)
        print_results(query, results, mode)


def main():
    parser = argparse.ArgumentParser(description="招生章程混合检索验证（pgvector ‖ pg_trgm → RRF）")
    parser.add_argument("--dsn", default=DEFAULT_DSN, help="数据库 DSN")
    parser.add_argument("--query", "-q", help="查询文本（不指定则进入交互模式）")
    parser.add_argument("--top", "-k", type=int, default=5, help="返回条数")
    parser.add_argument("--pool", type=int, default=20, help="每一路的召回池大小（融合前）")
    parser.add_argument("--school", "-s", help="按学校名称硬过滤")
    parser.add_argument("--year", "-y", type=int, default=2025, help="按年份硬过滤")
    parser.add_argument("--mode", "-m", choices=["hybrid", "vector", "keyword"],
                        default="hybrid", help="检索模式（默认 hybrid 双路 RRF）")
    parser.add_argument("--device", default=None, help="计算设备")
    args = parser.parse_args()

    embedder = None
    if args.mode in ("hybrid", "vector"):
        # 延迟导入：keyword 模式不需要加载 embedding 模型
        from rag.embedder import Embedder

        print("加载 embedding 模型...")
        embedder = Embedder(device=args.device)
        print(f"模型加载完成，维度={embedder.dim}")

    print(f"连接数据库: {args.dsn.split('@')[-1]}")
    conn = psycopg.connect(args.dsn, autocommit=True)
    retriever = HybridRetriever(conn, embedder=embedder)

    filters: dict = {}
    if args.school:
        filters["school_name"] = args.school
    if args.year:
        filters["year"] = args.year

    if args.query:
        results = retriever.search(
            args.query, top_k=args.top, pool_k=args.pool,
            filters=filters or None, mode=args.mode,
        )
        print_results(args.query, results, args.mode)
    else:
        run_interactive(retriever, filters or None, args.mode, args.top)

    conn.close()


if __name__ == "__main__":
    main()
