"""svc-orchestrator 入口：启动 grpc.aio 服务。

本地开发：从仓库根目录运行 make run-orchestrator PY=/path/to/env/bin/python3。
"""
from __future__ import annotations

import asyncio
import logging
import os


async def _serve() -> None:
    import grpc

    from app.run_lifecycle import RunLifecycle
    from app.run_lifecycle.worker import RunLifecycleWorker
    from app.server import OrchestratorServicer
    from api.orchestrator.v1 import orchestrator_pb2_grpc

    addr = os.getenv("ORCHESTRATOR_ADDR", "[::]:9100")
    dsn = os.getenv("ORCHESTRATOR_DSN") or os.getenv("DATABASE_URL")
    lifecycle = RunLifecycle(dsn, auto_profile=True) if dsn else None
    worker = RunLifecycleWorker(lifecycle) if lifecycle is not None else None
    server = grpc.aio.server()
    orchestrator_pb2_grpc.add_OrchestratorServicer_to_server(
        OrchestratorServicer(lifecycle=lifecycle),
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
