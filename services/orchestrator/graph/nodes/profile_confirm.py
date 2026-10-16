"""PROFILE_CONFIRM 节点：HITL 中断，等用户确认画像卡片。"""
from __future__ import annotations

from graph.state import GraphState


async def run(state: GraphState) -> GraphState:
    # TODO(M3): interrupt() 推画像卡片（经 gateway 的 SSE）；用户确认 / 修正后 resume。
    return state
