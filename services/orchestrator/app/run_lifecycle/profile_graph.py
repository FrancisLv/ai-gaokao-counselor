from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph
from langgraph.types import interrupt


class ProfileGraphState(TypedDict, total=False):
    run_id: str
    prompt: dict[str, Any]
    resume_answer: dict[str, Any]
    preview: dict[str, Any]
    stage: str


async def prepare_profile(state: ProfileGraphState) -> ProfileGraphState:
    return {"stage": "PROFILE_READY"}


async def pause_for_user(state: ProfileGraphState) -> ProfileGraphState:
    answer = interrupt(state["prompt"])
    return {"resume_answer": answer, "stage": "PROFILE_ANSWERED"}


def route_after_profile(state: ProfileGraphState) -> str:
    answer = state.get("resume_answer") or {}
    if answer.get("type") == "CONFIRM_PROFILE":
        return "wait_for_execution"
    return END


async def wait_for_execution(state: ProfileGraphState) -> ProfileGraphState:
    """确认写回业务库后，由 worker 恢复内部执行闸门。"""
    interrupt(
        {
            "kind": "INTERNAL_ALGORITHM_PREVIEW",
            "run_id": state["run_id"],
        }
    )
    return {"stage": "ALGORITHM_PREVIEW_READY"}


def build_profile_graph(
    checkpointer,
    algorithm_preview: Callable[[str], Awaitable[dict[str, Any]]],
):
    async def preview_node(state: ProfileGraphState) -> ProfileGraphState:
        preview = await algorithm_preview(state["run_id"])
        return {"preview": preview, "stage": "PREVIEW_READY"}

    graph = StateGraph(ProfileGraphState)
    graph.add_node("prepare_profile", prepare_profile)
    graph.add_node("pause_for_user", pause_for_user)
    graph.add_node("wait_for_execution", wait_for_execution)
    graph.add_node("algorithm_preview", preview_node)
    graph.set_entry_point("prepare_profile")
    graph.add_edge("prepare_profile", "pause_for_user")
    graph.add_conditional_edges(
        "pause_for_user",
        route_after_profile,
        {
            "wait_for_execution": "wait_for_execution",
            END: END,
        },
    )
    graph.add_edge("wait_for_execution", "algorithm_preview")
    graph.add_edge("algorithm_preview", END)
    return graph.compile(checkpointer=checkpointer)
