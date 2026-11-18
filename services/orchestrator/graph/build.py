"""组装 LangGraph 图：节点 + 条件边 + 循环闸门。

图（见架构文档）：
  INIT → INTAKE(闸门循环) → PROFILE_CONFIRM → RETRIEVING
       → CHECK_CONDITIONAL →(有)→ BACKFILL_ASK → PROFILE_CONFIRM
       →(无)→ STRATEGIZING → CONSTRAINING → RISK_ASSESSING
       → CRITIQUING(循环) → OPTIMIZING → REPORTING → VERIFYING(循环) → DONE
"""
from __future__ import annotations

from langgraph.graph import END, StateGraph

from graph.nodes import check_conditional, intake, profile_confirm, retrieve
from graph.state import GraphState

# 循环护栏（超限即裁决，防止死循环与 token 爆炸）
MAX_ASK_ROUNDS = 6
MAX_CRITIC_LOOPS = 2
MAX_VERIFY_RETRIES = 2


def route_after_intake(state: GraphState) -> str:
    """INTAKE 的确定性条件边：齐了就确认，没齐就继续追问（受轮次上限约束）。"""
    if not state.get("missing"):
        return "confirm"
    if state.get("ask_round", 0) >= MAX_ASK_ROUNDS:
        return "confirm"  # 到上限：带缺失项继续，并标记低置信
    return "ask"


def build_graph():
    g = StateGraph(GraphState)

    g.add_node("intake", intake.run)
    g.add_node("profile_confirm", profile_confirm.run)
    g.add_node("retrieve", retrieve.run)
    g.add_node("check_conditional", check_conditional.run)
    # TODO(M3+): strategy / constraint / risk / critic / optimize / report / verify / backfill

    g.set_entry_point("intake")
    g.add_conditional_edges(
        "intake", route_after_intake, {"ask": "intake", "confirm": "profile_confirm"}
    )
    g.add_edge("profile_confirm", "retrieve")
    g.add_edge("retrieve", "check_conditional")
    g.add_edge("check_conditional", END)  # TODO: 接 STRATEGIZING…

    return g.compile()
