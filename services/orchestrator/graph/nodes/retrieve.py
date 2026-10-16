"""RETRIEVING 节点：结构化(SQL) ‖ 非结构化(RAG) → RRF → Rerank。

本地执行，不经 svc-llm-gateway（Embedding / Rerank 用本地小模型）。
"""
from __future__ import annotations

from graph.state import GraphState


async def run(state: GraphState) -> GraphState:
    # TODO(M1):
    #   分支 A（结构化）：SQL 精确查询历年分数线 / 招生计划 / 选科要求
    #   分支 B（非结构化）：pgvector ‖ tsvector → 元数据硬过滤 → RRF → cross-encoder rerank
    state["candidates"] = []
    return state
