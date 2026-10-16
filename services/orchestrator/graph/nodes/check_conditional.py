"""CHECK_CONDITIONAL 节点：是否需要补问「条件必需」项。

只在「候选确实有体检 / 单科 / 语种要求、且用户尚未提供」时才产生补问，
避免一上来就把用户问死。
"""
from __future__ import annotations

from graph.state import CONDITIONAL_REQUIRED, GraphState


async def run(state: GraphState) -> GraphState:
    profile = state.get("profile") or {}
    # TODO(M3): 由「候选的硬性要求」驱动，而不是把所有条件字段都问一遍。
    #   例如：候选里没有带体检限制的专业 → 不用问体检。
    missing = [f for f in CONDITIONAL_REQUIRED if not profile.get(f)]
    state["missing"] = missing
    return state
