"""在线政策检索节点：只通过唯一 ``RetrievePolicy`` seam 读取证据。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from graph.state import GraphState
from rag.policy_retrieval import (
    ApplicabilityScope,
    PolicyRetrievalError,
    PolicyRetrievalQuery,
    PolicyRetrievalResult,
)


class RetrievePolicyPort(Protocol):
    def retrieve(self, query: PolicyRetrievalQuery) -> PolicyRetrievalResult:
        """在指定 release 和适用域内检索政策证据。"""


class RetrievalNodeError(Exception):
    def __init__(self, reason: str, message: str | None = None) -> None:
        self.reason = reason
        super().__init__(message or reason)


@dataclass(frozen=True, slots=True)
class ConfirmedProfileScope:
    profile_version: str
    province: str
    application_year: int
    category: str
    batch: str

    def to_scope(self) -> ApplicabilityScope:
        return ApplicabilityScope(
            province=self.province,
            application_year=self.application_year,
            category=self.category,
            batch=self.batch,
        )


@dataclass(frozen=True, slots=True)
class RetrievalRunBinding:
    """由 RunLifecycle 权威记录构造，不接受用户正文覆盖。"""

    principal_id: str
    run_id: str
    release_id: str
    task_kind: str
    confirmed_profile: ConfirmedProfileScope | None = None
    task_scope: ApplicabilityScope | None = None
    school_ids: tuple[str, ...] = ()
    document_types: tuple[str, ...] = ()
    mode: str = "hybrid"
    top_k: int = 5
    pool_k: int = 20


def _confirmed_scope(binding: RetrievalRunBinding) -> ApplicabilityScope:
    if binding.confirmed_profile is None:
        raise RetrievalNodeError("PROFILE_NOT_CONFIRMED")
    return binding.confirmed_profile.to_scope()


def build_retrieve_node(
    retrieve_policy: RetrievePolicyPort,
    binding: RetrievalRunBinding,
):
    """绑定可信 run 上下文，返回只执行检索的 LangGraph 节点。"""

    async def run(state: GraphState) -> GraphState:
        state["principal_id"] = binding.principal_id
        state["run_id"] = binding.run_id
        state["release_id"] = binding.release_id
        state["task_kind"] = binding.task_kind
        scope = binding.task_scope or _confirmed_scope(binding)
        query = PolicyRetrievalQuery(
            principal_id=binding.principal_id,
            query_text=str(state.get("user_text") or ""),
            scope=scope,
            release_id=binding.release_id,
            mode=binding.mode,
            school_ids=binding.school_ids,
            document_types=binding.document_types,
            top_k=binding.top_k,
            pool_k=binding.pool_k,
        )
        try:
            result = await asyncio.to_thread(retrieve_policy.retrieve, query)
        except PolicyRetrievalError as exc:
            state["retrieval_status"] = exc.reason
            state["retrieval_error"] = exc.reason
            state["policy_evidence"] = ()
            state["retrieval_diagnostics"] = None
            state["degraded"] = False
            return state
        if result.release_id != binding.release_id or any(
            item.release_id != binding.release_id
            or item.document.release_id != binding.release_id
            for item in result.evidence
        ):
            state["retrieval_status"] = "EVIDENCE_RELEASE_MISMATCH"
            state["retrieval_error"] = "EVIDENCE_RELEASE_MISMATCH"
            state["policy_evidence"] = ()
            state["retrieval_diagnostics"] = None
            state["degraded"] = False
            return state
        state["retrieval_status"] = result.status
        state["policy_evidence"] = result.evidence
        state["retrieval_diagnostics"] = result.diagnostics
        state["retrieval_error"] = None
        state["degraded"] = result.diagnostics.degraded_reason is not None
        return state

    return run


async def run(state: GraphState) -> GraphState:
    raise RetrievalNodeError(
        "RETRIEVAL_BINDING_REQUIRED",
        "在线检索节点必须绑定可信 run 上下文和 RetrievePolicy",
    )
