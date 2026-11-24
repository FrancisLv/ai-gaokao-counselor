"""svc-orchestrator 入口：启动 grpc.aio 服务。

本地开发：从仓库根目录运行 make run-orchestrator PY=/path/to/env/bin/python3。
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date


async def _serve() -> None:
    import grpc

    from app.run_lifecycle import RunLifecycle
    from app.data_lifecycle import PrincipalDataDeletion
    from app.run_lifecycle.algo import GrpcAlgoClient
    from app.run_lifecycle.conversation_intent import (
        GrpcConversationIntentClassifier,
    )
    from app.run_lifecycle.llm_report import GrpcReportNarrativeWriter
    from app.run_lifecycle.policy import PostgresPolicyRetriever
    from app.run_lifecycle.worker import RunLifecycleWorker
    from app.server import OrchestratorServicer
    from api.orchestrator.v1 import orchestrator_pb2_grpc

    addr = os.getenv("ORCHESTRATOR_ADDR", "[::]:9100")
    dsn = os.getenv("ORCHESTRATOR_DSN") or os.getenv("DATABASE_URL")
    algo_client = (
        GrpcAlgoClient(address=os.getenv("ALGO_ADDR", "localhost:9002"))
        if dsn
        else None
    )
    llm_gateway_addr = os.getenv("LLM_GATEWAY_ADDR")
    llm_gateway_channel = (
        grpc.aio.insecure_channel(llm_gateway_addr)
        if llm_gateway_addr
        else None
    )
    report_narrative_writer = (
        GrpcReportNarrativeWriter(llm_gateway_channel)
        if llm_gateway_channel is not None
        else None
    )
    conversation_intent_classifier = (
        GrpcConversationIntentClassifier(llm_gateway_channel)
        if llm_gateway_channel is not None
        else None
    )
    lifecycle = (
        RunLifecycle(
            dsn,
            auto_profile=False,
            algo_client=algo_client,
            policy_retriever=PostgresPolicyRetriever(
                dsn,
                embedding_model=os.getenv("POLICY_EMBEDDING_MODEL"),
                as_of=(
                    date.fromisoformat(os.environ["POLICY_RETRIEVAL_AS_OF"])
                    if os.getenv("POLICY_RETRIEVAL_AS_OF")
                    else None
                ),
            ),
            report_narrative_writer=report_narrative_writer,
            conversation_intent_classifier=conversation_intent_classifier,
        )
        if dsn
        else None
    )
    worker = RunLifecycleWorker(lifecycle) if lifecycle is not None else None
    deletion = (
        PrincipalDataDeletion(
            dsn,
            confirmation_authority=_GatewayDeletionAuthority(),
        )
        if dsn
        else None
    )
    server = grpc.aio.server()
    orchestrator_pb2_grpc.add_OrchestratorServicer_to_server(
        OrchestratorServicer(lifecycle=lifecycle, deletion=deletion),
        server,
    )
    server.add_insecure_port(addr)
    logging.info("svc-orchestrator listening on %s", addr)
    if worker is not None:
        await worker.start()
    await server.start()
    try:
        await server.wait_for_termination()
    finally:
        if worker is not None:
            await worker.stop()
        if lifecycle is not None:
            await lifecycle.close()
        if algo_client is not None:
            await algo_client.Close()
        if llm_gateway_channel is not None:
            await llm_gateway_channel.close()


class _GatewayDeletionAuthority:
    async def consume(
        self,
        *,
        principal_id: str,
        request_id: str,
        confirmation_token: str,
    ) -> bool:
        return (
            bool(principal_id.strip())
            and bool(request_id.strip())
            and confirmation_token == "gateway-authenticated"
        )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(_serve())
    except ImportError as e:
        raise SystemExit(
            "缺少生成的 pb —— 先运行 `make api-py`（需要 grpcio-tools）。原始错误: %r" % (e,)
        )


if __name__ == "__main__":
    main()
