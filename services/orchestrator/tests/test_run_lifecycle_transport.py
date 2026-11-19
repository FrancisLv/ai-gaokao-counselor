"""RunLifecycle 的 gRPC 传输适配验收。"""
from __future__ import annotations

from uuid import UUID

import grpc

from api.algo.v1 import algo_pb2
from api.common.v1 import common_pb2
from api.orchestrator.v1 import orchestrator_pb2 as pb
from api.orchestrator.v1 import orchestrator_pb2_grpc as pb_grpc
from run_lifecycle_support import RunLifecyclePostgresTest
from test_run_lifecycle_algorithm_preview import FakeAlgoClient


class NoEvidencePolicyRetriever:
    async def retrieve(self, query):
        from rag.policy_retrieval import (
            PolicyRetrievalDiagnostics,
            PolicyRetrievalResult,
        )

        return PolicyRetrievalResult(
            status="NO_EVIDENCE",
            release_id=query.release_id,
            diagnostics=PolicyRetrievalDiagnostics(
                relevance_filtered_candidates=2,
                relevance_policy_version="m4a-relevance-v1",
            ),
        )


class MissingBasisAlgoClient(FakeAlgoClient):
    async def EstimateProb(self, request):
        self.calls.append(("EstimateProb", request))
        return algo_pb2.EstimateProbReply(stable_candidates=[self.candidate])


class RunLifecycleGrpcTests(RunLifecyclePostgresTest):
    async def test_grpc_policy_qa_requires_query_text(self):
        await self.seed_release()
        with self.assertRaises(grpc.aio.AioRpcError) as raised:
            await self.client.SubmitRun(
                pb.SubmitRunRequest(
                    task_kind=pb.POLICY_QA,
                    policy_input=pb.PolicyQaInput(
                        province="湖南",
                        application_year=2025,
                        category="物理类",
                        batch="本科批",
                    ),
                    idempotency_key="grpc-policy-no-query",
                ),
                metadata=(("x-principal-id", "principal-policy"),),
            )
        self.assertEqual(
            raised.exception.details(),
            "RETRIEVAL_QUERY_REQUIRED",
        )

    async def test_grpc_policy_qa_returns_typed_no_evidence_result(self):
        import asyncio

        from app.run_lifecycle import RunLifecycle
        from app.server import OrchestratorServicer

        await self.seed_release()
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            policy_retriever=NoEvidencePolicyRetriever(),
        )
        self.addAsyncCleanup(lifecycle.close)
        server = grpc.aio.server()
        pb_grpc.add_OrchestratorServicer_to_server(
            OrchestratorServicer(lifecycle=lifecycle),
            server,
        )
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        self.addAsyncCleanup(server.stop, None)
        channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
        self.addAsyncCleanup(channel.close)
        client = pb_grpc.OrchestratorStub(channel)
        metadata = (("x-principal-id", "principal-policy"),)

        receipt = await client.SubmitRun(
            pb.SubmitRunRequest(
                task_kind=pb.POLICY_QA,
                policy_input=pb.PolicyQaInput(
                    query_text="转专业政策是什么？",
                    province="湖南",
                    application_year=2025,
                    category="物理类",
                    batch="本科批",
                    mode="keyword",
                ),
                idempotency_key="grpc-policy-qa",
            ),
            metadata=metadata,
        )

        for _ in range(200):
            view = await client.GetRun(
                pb.GetRunRequest(run_id=receipt.run_id),
                metadata=metadata,
            )
            if view.state == pb.SUCCEEDED:
                break
            await asyncio.sleep(0.01)

        self.assertTrue(receipt.accepted)
        self.assertEqual(view.task_kind, pb.POLICY_QA)
        self.assertEqual(view.result.result_kind, "POLICY_EVIDENCE")
        self.assertEqual(
            view.policy_evidence.retrieval_status,
            "NO_EVIDENCE",
        )
        self.assertEqual(
            view.policy_evidence.relevance_filtered_candidates,
            2,
        )
        self.assertEqual(
            view.policy_evidence.relevance_policy_version,
            "m4a-relevance-v1",
        )
        self.assertEqual(len(view.policy_evidence.evidence), 0)
        events = client.WatchRun(
            pb.WatchRunRequest(
                run_id=receipt.run_id,
                after_event_seq=0,
            ),
            metadata=metadata,
        )
        result_event = None
        while result_event is None:
            event = await asyncio.wait_for(events.read(), timeout=1)
            if event.type == pb.RESULT:
                result_event = event
        events.cancel()
        self.assertEqual(
            result_event.policy_evidence.retrieval_status,
            "NO_EVIDENCE",
        )
        self.assertEqual(
            result_event.policy_evidence.relevance_filtered_candidates,
            2,
        )
        self.assertEqual(
            result_event.policy_evidence.relevance_policy_version,
            "m4a-relevance-v1",
        )
        self.assertEqual(
            result_event.policy_evidence,
            view.policy_evidence,
        )

    async def test_legacy_start_graph_delegates_to_submit_run(self):
        import json

        from app.run_lifecycle import RunLifecycle
        from app.server import OrchestratorServicer

        await self.seed_release()
        lifecycle = RunLifecycle(self.dsn, auto_profile=True)
        self.addAsyncCleanup(lifecycle.close)
        server = grpc.aio.server()
        pb_grpc.add_OrchestratorServicer_to_server(
            OrchestratorServicer(lifecycle=lifecycle),
            server,
        )
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        self.addAsyncCleanup(server.stop, None)
        channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
        self.addAsyncCleanup(channel.close)
        client = pb_grpc.OrchestratorStub(channel)
        metadata = (("x-principal-id", "principal-a"),)

        stream = client.StartGraph(
            pb.StartGraphRequest(
                trace=common_pb2.Trace(trace_id="legacy-trace"),
                user_text=json.dumps(
                    {
                        "province": "湖南",
                        "application_year": 2025,
                        "category": "物理类",
                        "batch": "本科批",
                        "score": 600,
                        "subjects": ["物理", "化学", "生物"],
                        "idempotency_key": "legacy-submit",
                    },
                    ensure_ascii=False,
                ),
            ),
            metadata=metadata,
        )
        try:
            event = await stream.read()
        finally:
            stream.cancel()
        payload = json.loads(event.payload_json)
        self.assertEqual(event.node, "ACCEPTED")
        self.assertTrue(payload["run_id"])
        view = await lifecycle.GetRun(
            principal_id="principal-a",
            run_id=UUID(payload["run_id"]),
        )
        self.assertEqual(view.state, "WAITING_HITL")

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

    async def test_grpc_resume_confirms_the_interrupt_profile_version(self):
        from app.run_lifecycle import RunLifecycle
        from app.server import OrchestratorServicer

        await self.seed_release()
        server = grpc.aio.server()
        pb_grpc.add_OrchestratorServicer_to_server(
            OrchestratorServicer(
                lifecycle=RunLifecycle(self.dsn, auto_profile=True),
            ),
            server,
        )
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
        client = pb_grpc.OrchestratorStub(channel)
        metadata = (("x-principal-id", "principal-a"),)

        try:
            receipt = await client.SubmitRun(
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
                    idempotency_key="grpc-profile-submit",
                ),
                metadata=metadata,
            )
            waiting = await self._wait_for_grpc_interrupt(client, receipt.run_id, metadata)
            profile_version = waiting.current_interrupt.profile_version
            events = client.WatchRun(
                pb.WatchRunRequest(run_id=receipt.run_id, after_event_seq=0),
                metadata=metadata,
            )
            replayed_interrupt = None
            progress_event = None
            try:
                while replayed_interrupt is None or progress_event is None:
                    event = await events.read()
                    if event.type == pb.INTERRUPT:
                        replayed_interrupt = event.interrupt
                    elif event.type == pb.PROGRESS:
                        progress_event = event.progress
            finally:
                events.cancel()

            resumed = await client.ResumeRun(
                pb.ResumeRunRequest(
                    run_id=receipt.run_id,
                    interrupt_id=waiting.current_interrupt.interrupt_id,
                    expected_state_version=waiting.state_version,
                    profile_version=profile_version,
                    confirm_profile=pb.ConfirmProfileAnswer(),
                    idempotency_key="grpc-profile-confirm",
                ),
                metadata=metadata,
            )
            view = await self._wait_for_grpc_confirmation(client, receipt.run_id, metadata)

            self.assertTrue(resumed.accepted)
            self.assertEqual(resumed.state, pb.QUEUED)
            self.assertEqual(view.confirmed_profile_version, profile_version)
            self.assertFalse(view.HasField("current_interrupt"))
            self.assertEqual(replayed_interrupt.profile_version, profile_version)
            self.assertGreater(view.expires_at.seconds, 0)
            self.assertEqual(progress_event.stage, "INTAKE")
            self.assertTrue(progress_event.metadata["profile_version"])
        finally:
            await channel.close()
            await server.stop(grace=None)

    async def test_grpc_result_event_and_get_run_share_the_algorithm_preview(self):
        from app.run_lifecycle import RunLifecycle
        from app.run_lifecycle.worker import RunLifecycleWorker
        from app.server import OrchestratorServicer

        await self.seed_release()
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="grpc-preview-worker",
            algo_client=FakeAlgoClient(),
        )
        self.addAsyncCleanup(lifecycle.close)
        worker = RunLifecycleWorker(lifecycle, poll_interval=0.01)
        await worker.start()
        self.addAsyncCleanup(worker.stop)
        server = grpc.aio.server()
        pb_grpc.add_OrchestratorServicer_to_server(
            OrchestratorServicer(lifecycle=lifecycle),
            server,
        )
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        self.addAsyncCleanup(server.stop, None)
        channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
        self.addAsyncCleanup(channel.close)
        client = pb_grpc.OrchestratorStub(channel)
        metadata = (("x-principal-id", "principal-a"),)

        receipt = await client.SubmitRun(
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
                idempotency_key="grpc-preview-submit",
            ),
            metadata=metadata,
        )
        waiting = await self._wait_for_grpc_interrupt(client, receipt.run_id, metadata)
        await client.ResumeRun(
            pb.ResumeRunRequest(
                run_id=receipt.run_id,
                interrupt_id=waiting.current_interrupt.interrupt_id,
                expected_state_version=waiting.state_version,
                profile_version=waiting.current_interrupt.profile_version,
                confirm_profile=pb.ConfirmProfileAnswer(),
                idempotency_key="grpc-preview-confirm",
            ),
            metadata=metadata,
        )
        view = await self._wait_for_grpc_preview(client, receipt.run_id, metadata)
        events = client.WatchRun(
            pb.WatchRunRequest(run_id=receipt.run_id, after_event_seq=0),
            metadata=metadata,
        )
        result_event = None
        try:
            while result_event is None:
                event = await events.read()
                if event.type == pb.RESULT:
                    result_event = event
        finally:
            events.cancel()

        self.assertEqual(view.state, pb.SUCCEEDED)
        self.assertEqual(view.result.result_kind, "ALGORITHM_PREVIEW")
        self.assertEqual(view.result_preview.result_kind, "ALGORITHM_PREVIEW")
        self.assertTrue(view.result_preview.internal_controlled_only)
        self.assertFalse(view.result_preview.formal_recommendation)
        self.assertEqual(
            view.result_preview.stable_candidates[0].candidate.university_code,
            "U001",
        )
        self.assertEqual(result_event.result, view.result_preview)

    async def test_grpc_error_event_has_a_typed_stable_reason(self):
        from app.run_lifecycle import RunLifecycle
        from app.run_lifecycle.worker import RunLifecycleWorker
        from app.server import OrchestratorServicer

        await self.seed_release()
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="grpc-error-worker",
            algo_client=MissingBasisAlgoClient(),
        )
        self.addAsyncCleanup(lifecycle.close)
        worker = RunLifecycleWorker(lifecycle, poll_interval=0.01)
        await worker.start()
        self.addAsyncCleanup(worker.stop)
        server = grpc.aio.server()
        pb_grpc.add_OrchestratorServicer_to_server(
            OrchestratorServicer(lifecycle=lifecycle),
            server,
        )
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        self.addAsyncCleanup(server.stop, None)
        channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
        self.addAsyncCleanup(channel.close)
        client = pb_grpc.OrchestratorStub(channel)
        metadata = (("x-principal-id", "principal-a"),)

        receipt = await client.SubmitRun(
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
                idempotency_key="grpc-error-submit",
            ),
            metadata=metadata,
        )
        waiting = await self._wait_for_grpc_interrupt(client, receipt.run_id, metadata)
        await client.ResumeRun(
            pb.ResumeRunRequest(
                run_id=receipt.run_id,
                interrupt_id=waiting.current_interrupt.interrupt_id,
                expected_state_version=waiting.state_version,
                profile_version=waiting.current_interrupt.profile_version,
                confirm_profile=pb.ConfirmProfileAnswer(),
                idempotency_key="grpc-error-confirm",
            ),
            metadata=metadata,
        )
        events = client.WatchRun(
            pb.WatchRunRequest(run_id=receipt.run_id, after_event_seq=0),
            metadata=metadata,
        )
        error_event = None
        try:
            while error_event is None:
                event = await events.read()
                if event.type == pb.ERROR:
                    error_event = event.error
        finally:
            events.cancel()

        view = await client.GetRun(
            pb.GetRunRequest(run_id=receipt.run_id),
            metadata=metadata,
        )
        self.assertEqual(error_event.code, "VALIDATION_FAILED")
        self.assertFalse(error_event.retryable)
        self.assertEqual(view.state, pb.FAILED)
        self.assertFalse(view.HasField("result_preview"))

    async def _wait_for_grpc_interrupt(self, client, run_id, metadata):
        import asyncio

        for _ in range(200):
            view = await client.GetRun(
                pb.GetRunRequest(run_id=run_id),
                metadata=metadata,
            )
            if (
                view.state == pb.WAITING_HITL
                and view.HasField("current_interrupt")
            ):
                return view
            await asyncio.sleep(0.01)
        self.fail("gRPC run 未进入 interrupt")

    async def _wait_for_grpc_confirmation(self, client, run_id, metadata):
        import asyncio

        for _ in range(200):
            view = await client.GetRun(
                pb.GetRunRequest(run_id=run_id),
                metadata=metadata,
            )
            if view.confirmed_profile_version:
                return view
            await asyncio.sleep(0.01)
        self.fail("gRPC run 未确认画像")

    async def _wait_for_grpc_preview(self, client, run_id, metadata):
        import asyncio

        for _ in range(300):
            view = await client.GetRun(
                pb.GetRunRequest(run_id=run_id),
                metadata=metadata,
            )
            if view.HasField("result_preview"):
                return view
            await asyncio.sleep(0.01)
        self.fail("gRPC run 未发布 algorithm preview")
