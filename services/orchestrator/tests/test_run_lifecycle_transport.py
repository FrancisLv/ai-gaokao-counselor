"""RunLifecycle 的 gRPC 传输适配验收。"""
from __future__ import annotations

import grpc

from api.orchestrator.v1 import orchestrator_pb2 as pb
from api.orchestrator.v1 import orchestrator_pb2_grpc as pb_grpc
from run_lifecycle_support import RunLifecyclePostgresTest


class RunLifecycleGrpcTests(RunLifecyclePostgresTest):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        from app.run_lifecycle import RunLifecycle
        from app.server import OrchestratorServicer

        self.server = grpc.aio.server()
        pb_grpc.add_OrchestratorServicer_to_server(
            OrchestratorServicer(lifecycle=RunLifecycle(self.dsn)),
            self.server,
        )
        port = self.server.add_insecure_port("127.0.0.1:0")
        await self.server.start()
        self.channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
        self.client = pb_grpc.OrchestratorStub(self.channel)

    async def asyncTearDown(self) -> None:
        await self.channel.close()
        await self.server.stop(grace=None)

    async def test_grpc_transport_exposes_submit_get_watch_and_cancel(self):
        release_id = await self.seed_release()
        metadata = (("x-principal-id", "principal-a"),)

        receipt = await self.client.SubmitRun(
            pb.SubmitRunRequest(
                task_kind=pb.GENERATE_PLAN,
                profile_input=pb.StructuredProfileInput(
                    province="湖南",
                    application_year=2025,
                    category="物理类",
                    batch="本科批",
                    score=600,
                    subjects=["物理", "化学", "生物"],
                ),
                idempotency_key="grpc-submit",
            ),
            metadata=metadata,
        )
        view = await self.client.GetRun(
            pb.GetRunRequest(run_id=receipt.run_id),
            metadata=metadata,
        )

        self.assertTrue(receipt.accepted)
        self.assertEqual(receipt.state, pb.QUEUED)
        self.assertEqual(view.release_id, release_id)

        events = self.client.WatchRun(
            pb.WatchRunRequest(run_id=receipt.run_id, after_event_seq=0),
            metadata=metadata,
        )
        event = await events.read()
        events.cancel()
        self.assertEqual(event.event_seq, 1)
        self.assertEqual(event.state_changed.to_state, "QUEUED")

        cancelled = await self.client.CancelRun(
            pb.CancelRunRequest(
                run_id=receipt.run_id,
                idempotency_key="grpc-cancel",
            ),
            metadata=metadata,
        )
        self.assertTrue(cancelled.accepted)
        self.assertEqual(cancelled.state, pb.CANCELLED)

    async def test_grpc_transport_requires_a_trusted_principal(self):
        await self.seed_release()

        with self.assertRaises(grpc.aio.AioRpcError) as raised:
            await self.client.SubmitRun(
                pb.SubmitRunRequest(
                    task_kind=pb.GENERATE_PLAN,
                    profile_input=pb.StructuredProfileInput(
                        province="湖南",
                        application_year=2025,
                        category="物理类",
                        batch="本科批",
                        score=600,
                        subjects=["物理", "化学", "生物"],
                    ),
                    idempotency_key="grpc-no-principal",
                )
            )

        self.assertEqual(raised.exception.code(), grpc.StatusCode.UNAUTHENTICATED)
        self.assertEqual(raised.exception.details(), "PRINCIPAL_REQUIRED")
