from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, StateGraph
from langgraph.types import interrupt


class ProfileGraphState(TypedDict, total=False):
    prompt: dict[str, Any]
    resume_answer: dict[str, Any]


async def pause_for_user(state: ProfileGraphState) -> ProfileGraphState:
    answer = interrupt(state["prompt"])
    return {"resume_answer": answer}


def build_profile_graph(checkpointer):
    graph = StateGraph(ProfileGraphState)
    graph.add_node("pause_for_user", pause_for_user)
    graph.set_entry_point("pause_for_user")
    graph.add_edge("pause_for_user", END)
    return graph.compile(checkpointer=checkpointer)
