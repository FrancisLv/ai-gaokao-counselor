"""svc-orchestrator 的 gRPC servicer：驱动 LangGraph 图，把事件以流返回。

调用方向（见 docs/architecture）：
  · 被 svc-gateway 调用（StartGraph / ResumeGraph）；
  · 反过来调 svc-llm-gateway（LLM 治理）与 svc-algo（领域算法）；
  · 本地 Embedding / Rerank 在进程内完成，不经过 svc-llm-gateway。
"""
from __future__ import annotations

import json
import logging
from typing import AsyncIterator

from pb import orchestrator_pb2, orchestrator_pb2_grpc

from graph.build import build_graph
from graph.state import new_state

log = logging.getLogger(__name__)


class OrchestratorServicer(orchestrator_pb2_grpc.OrchestratorServicer):
    def __init__(self) -> None:
        self._graph = build_graph()

    async def StartGraph(self, request, context) -> AsyncIterator["orchestrator_pb2.GraphEvent"]:
        state = new_state(
            session_id=request.trace.session_id,
            trace_id=request.trace.trace_id,
            user_text=request.user_text,
        )
        async for ev in self._run(state):
            yield ev

    async def ResumeGraph(self, request, context) -> AsyncIterator["orchestrator_pb2.GraphEvent"]:
        # TODO(M3): 从 LangGraph Postgres saver 恢复检查点，合并用户回复后继续。
        yield orchestrator_pb2.GraphEvent(
            node="RESUME", type="progress",
            payload_json=json.dumps({"session_id": request.session_id}, ensure_ascii=False),
        )

    async def _run(self, state) -> AsyncIterator["orchestrator_pb2.GraphEvent"]:
        # TODO(M3): 识别 LangGraph 的 interrupt（HITL）→ type="interrupt" 推给 gateway。
        async for chunk in self._graph.astream(state):
            for node, payload in chunk.items():
                yield orchestrator_pb2.GraphEvent(
                    node=node, type="progress",
                    payload_json=json.dumps(payload, ensure_ascii=False, default=str),
                )
        yield orchestrator_pb2.GraphEvent(node="DONE", type="result", payload_json="{}")
