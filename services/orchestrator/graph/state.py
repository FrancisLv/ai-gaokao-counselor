"""LangGraph 图的状态定义，以及**画像 Schema**（闸门据此判断"够没够"）。

注意：字段分类是「确定性闸门」的依据——不许让 LLM 来判断信息是否足够。
"""
from __future__ import annotations

from typing import Any, TypedDict

# ---- 画像 Schema ----

# 全局必需：缺任一都不能往下走
GLOBAL_REQUIRED = ["province", "year", "track", "score_or_rank", "subjects"]

# 条件必需：只对「候选确实有该要求」时才需要（由 CHECK_CONDITIONAL 按需补问）
CONDITIONAL_REQUIRED = [
    "physical_exam",         # 体检（色觉 / 视力）
    "single_subject_scores", # 单科成绩（如英语 / 数学）
    "foreign_language",      # 外语语种
    "gender",                # 性别（部分专业限制）
]

# 可推导（不必问）：由算法算出
DERIVABLE = ["rank", "batch_control_line"]

# 可选偏好：缺失不阻断，只影响排序与解释
OPTIONAL_PREFS = ["region", "budget", "major_interest", "risk_appetite"]


class Profile(TypedDict, total=False):
    # 全局必需
    province: str
    year: str
    track: str
    score: int
    rank: int
    subjects: list[str]
    # 条件必需
    physical_exam: dict[str, Any]
    single_subject_scores: dict[str, int]
    foreign_language: str
    gender: str
    # 可选偏好
    region: str
    budget: float
    major_interest: list[str]
    risk_appetite: str


class GraphState(TypedDict, total=False):
    session_id: str
    trace_id: str
    user_text: str

    profile: Profile
    missing: list[str]      # 当前缺失的必需字段
    ask_round: int          # 追问轮次（护栏）

    candidates: list[dict]  # 冲/稳/保候选
    kept: list[dict]
    rejected: list[dict]
    wishlist: list[dict]
    report: str

    loop: int               # Critic 循环轮次
    retry: int              # Verify 循环轮次
    degraded: bool          # 是否进入了降级路径


def new_state(session_id: str, trace_id: str, user_text: str) -> GraphState:
    return GraphState(
        session_id=session_id,
        trace_id=trace_id,
        user_text=user_text,
        profile={},
        missing=list(GLOBAL_REQUIRED),
        ask_round=0,
        loop=0,
        retry=0,
        degraded=False,
    )


def check_completeness(profile: Profile) -> list[str]:
    """确定性闸门：返回仍然缺失的「全局必需」字段。

    绝不用 LLM 判断——保证稳定、可测（见架构文档第 6 章）。
    """
    missing: list[str] = []
    for field in GLOBAL_REQUIRED:
        if field == "score_or_rank":
            if not profile.get("score") and not profile.get("rank"):
                missing.append(field)
        elif not profile.get(field):
            missing.append(field)
    return missing
