"""INTAKE 节点：交互采集 + 确定性闸门。

流程：解析用户输入 → 结构化画像（经 svc-llm-gateway 调 LLM，JSON Schema 约束）
     → check_completeness 查缺 → 缺则生成追问（interrupt）→ 用户答完 resume。
"""
from __future__ import annotations

from graph.state import GraphState, check_completeness


async def run(state: GraphState) -> GraphState:
    # TODO(M2): 调 svc-llm-gateway（role="profile"）把 user_text 解析进 profile，
    #           并与上一轮 profile 合并（增量补全）。
    profile = state.get("profile") or {}

    state["profile"] = profile
    state["missing"] = check_completeness(profile)
    state["ask_round"] = state.get("ask_round", 0) + 1

    # TODO(M2): 若 missing 非空 → 用 LLM 生成自然语言追问并 interrupt()；
    #           话术由 LLM 写，但"问什么 / 够没够"由 Schema + 校验函数决定。
    return state
