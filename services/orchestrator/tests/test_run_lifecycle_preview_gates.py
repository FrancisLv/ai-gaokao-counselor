"""Ticket #6：ALGORITHM_PREVIEW 失败门禁与可复现性。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID

from api.algo.v1 import algo_pb2

from run_lifecycle_support import RunLifecyclePostgresTest
from test_run_lifecycle_algorithm_preview import FakeAlgoClient


class PreviewGateLifecycleTests(RunLifecyclePostgresTest):
    async def _run_preview(self, algo, suffix: str, release_id: str | None = None):
        from app.run_lifecycle import (
            ConfirmProfileAnswer,
            ResumeRunCommand,
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )
        from app.run_lifecycle.worker import RunLifecycleWorker

        release_id = release_id or await self.seed_release()
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner=f"worker-{suffix}",
            algo_client=algo,
        )
        self.addAsyncCleanup(lifecycle.close)
        worker = RunLifecycleWorker(lifecycle, poll_interval=0.01)
        await worker.start()
        self.addAsyncCleanup(worker.stop)
        submitted = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-a",
                session_id=None,
                task_kind="GENERATE_PLAN",
                input=StructuredProfileInput(
                    province="湖南",
                    application_year=2025,
                    category="物理类",
                    batch="本科批",
                    score=600,
                    subjects=("物理", "化学", "生物"),
                ),
                idempotency_key=f"{suffix}-submit",
            )
        )
        waiting = await self._wait_for_state(
            lifecycle,
            submitted.run_id,
            "WAITING_HITL",
        )
        await lifecycle.ResumeRun(
            ResumeRunCommand(
                principal_id="principal-a",
                run_id=submitted.run_id,
                interrupt_id=waiting.current_interrupt.interrupt_id,
                expected_state_version=waiting.state_version,
                profile_version=UUID(
                    waiting.current_interrupt.payload["profile_version"]
                ),
                answer=ConfirmProfileAnswer(),
                idempotency_key=f"{suffix}-confirm",
            )
        )
        return lifecycle, submitted.run_id, release_id

    async def test_invalid_probability_fails_before_downstream_algo_calls(self):
        class InvalidProbabilityAlgo(FakeAlgoClient):
            async def EstimateProb(self, request):
                self.calls.append(("EstimateProb", request))
                invalid = algo_pb2.CandidateWithProb(
                    candidate=self.candidate.candidate,
                    prob=algo_pb2.ProbEstimate(
                        probability=1.0,
                        method="weighted_hit",
                        data_years=3,
                        category="稳",
                    ),
                )
                return algo_pb2.EstimateProbReply(
                    stable_candidates=[invalid],
                    stable_total=1,
                    basis=algo_pb2.AuditBasis(
                        user_rank=15860,
                        user_percentile=0.1234,
                        sample_years=[2022, 2023, 2024],
                        method="weighted_hit",
                        province="湖南",
                        category="物理类",
                    ),
                )

        algo = InvalidProbabilityAlgo()
        lifecycle, run_id, _ = await self._run_preview(algo, "invalid-probability")

        failed = await self._wait_for_state(lifecycle, run_id, "terminal")

        self.assertEqual(failed.state, "FAILED")
        self.assertEqual(failed.terminal_reason, "VALIDATION_FAILED")
        self.assertIsNone(failed.result)
        self.assertEqual(
            [name for name, _ in algo.calls],
            ["RankConvert", "EstimateProb"],
        )

    async def test_constraints_cannot_inject_a_candidate_outside_estimate_prob(self):
        class CandidateInjectionAlgo(FakeAlgoClient):
            async def CheckConstraints(self, request):
                self.calls.append(("CheckConstraints", request))
                injected = algo_pb2.CandidateWithProb(
                    candidate=algo_pb2.Candidate(
                        university_code="U999",
                        university_name="注入大学",
                        major_group="99",
                    ),
                    prob=algo_pb2.ProbEstimate(
                        probability=0.5,
                        method="weighted_hit",
                        data_years=3,
                        category="稳",
                    ),
                )
                return algo_pb2.CheckConstraintsReply(
                    kept=[self.candidate, injected]
                )

        algo = CandidateInjectionAlgo()
        lifecycle, run_id, _ = await self._run_preview(algo, "candidate-injection")

        failed = await self._wait_for_state(lifecycle, run_id, "terminal")

        self.assertEqual(failed.state, "FAILED")
        self.assertEqual(failed.terminal_reason, "VALIDATION_FAILED")
        self.assertIsNone(failed.result)

    async def test_successful_preview_is_internal_controlled_only(self):
        algo = FakeAlgoClient()
        lifecycle, run_id, _ = await self._run_preview(algo, "controlled-marker")

        completed = await self._wait_for_state(lifecycle, run_id, "terminal")

        self.assertEqual(completed.state, "SUCCEEDED")
        self.assertIsNotNone(completed.result_payload)
        self.assertTrue(completed.result_payload["internal_controlled_only"])
        self.assertFalse(completed.result_payload["formal_recommendation"])
        self.assertIn(
            "不得作为正式志愿方案",
            completed.result_payload["disclaimer"],
        )
        self.assertNotIn(
            "保证录取",
            str(completed.result_payload["risk_notice"])
            + str(completed.result_payload["disclaimer"])
            + str(completed.result_payload["unverified_conditions"]),
        )

    async def test_rejected_candidates_cannot_inject_unestimated_identities(self):
        class RejectedInjectionAlgo(FakeAlgoClient):
            async def CheckConstraints(self, request):
                self.calls.append(("CheckConstraints", request))
                injected = algo_pb2.CandidateWithProb(
                    candidate=algo_pb2.Candidate(
                        university_code="U998",
                        major_group="98",
                    ),
                    prob=algo_pb2.ProbEstimate(
                        probability=0.4,
                        method="weighted_hit",
                        data_years=3,
                        category="冲",
                    ),
                )
                return algo_pb2.CheckConstraintsReply(
                    kept=[self.candidate],
                    rejected=[
                        algo_pb2.RejectedCandidate(
                            candidate=injected,
                            rule="subject_req",
                            reason="注入候选",
                        )
                    ],
                )

        algo = RejectedInjectionAlgo()
        lifecycle, run_id, _ = await self._run_preview(
            algo,
            "rejected-injection",
        )

        failed = await self._wait_for_state(lifecycle, run_id, "terminal")

        self.assertEqual(failed.state, "FAILED")
        self.assertEqual(failed.terminal_reason, "VALIDATION_FAILED")
        self.assertIsNone(failed.result)

    async def test_release_revoked_after_algo_results_blocks_publication(self):
        test_case = self

        class RevokedMidPreviewAlgo(FakeAlgoClient):
            async def OptimizeWishlist(self, request):
                await test_case.revoke_release(request.release_id)
                return await super().OptimizeWishlist(request)

        algo = RevokedMidPreviewAlgo()
        lifecycle, run_id, release_id = await self._run_preview(
            algo,
            "release-revoked-mid-preview",
        )

        failed = await self._wait_for_state(lifecycle, run_id, "terminal")

        self.assertEqual(failed.release_id, release_id)
        self.assertEqual(failed.state, "FAILED")
        self.assertEqual(failed.terminal_reason, "RELEASE_REVOKED")
        self.assertIsNone(failed.result)
        self.assertIsNone(failed.result_payload)

    async def test_execution_timeout_during_algo_call_blocks_publication(self):
        from app.run_lifecycle import (
            ConfirmProfileAnswer,
            ResumeRunCommand,
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )
        from app.run_lifecycle.worker import RunLifecycleWorker

        now = [datetime(2026, 11, 18, 8, 0, tzinfo=timezone.utc)]

        class SlowAlgo(FakeAlgoClient):
            async def OptimizeWishlist(self, request):
                now[0] += timedelta(minutes=10, seconds=1)
                return await super().OptimizeWishlist(request)

        await self.seed_release()
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="worker-execution-timeout",
            clock=lambda: now[0],
            algo_client=SlowAlgo(),
        )
        self.addAsyncCleanup(lifecycle.close)
        worker = RunLifecycleWorker(lifecycle, poll_interval=0.01)
        await worker.start()
        self.addAsyncCleanup(worker.stop)
        submitted = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-a",
                session_id=None,
                task_kind="GENERATE_PLAN",
                input=StructuredProfileInput(
                    province="湖南",
                    application_year=2025,
                    category="物理类",
                    batch="本科批",
                    score=600,
                    subjects=("物理", "化学", "生物"),
                ),
                idempotency_key="execution-timeout-submit",
            )
        )
        waiting = await self._wait_for_state(
            lifecycle,
            submitted.run_id,
            "WAITING_HITL",
        )
        await lifecycle.ResumeRun(
            ResumeRunCommand(
                principal_id="principal-a",
                run_id=submitted.run_id,
                interrupt_id=waiting.current_interrupt.interrupt_id,
                expected_state_version=waiting.state_version,
                profile_version=UUID(
                    waiting.current_interrupt.payload["profile_version"]
                ),
                answer=ConfirmProfileAnswer(),
                idempotency_key="execution-timeout-confirm",
            )
        )

        failed = await self._wait_for_state(
            lifecycle,
            submitted.run_id,
            "terminal",
        )

        self.assertEqual(failed.state, "FAILED")
        self.assertEqual(failed.terminal_reason, "EXECUTION_TIMEOUT")
        self.assertIsNone(failed.result)
        self.assertIsNone(failed.result_payload)

    async def test_cancel_during_preview_publication_never_leaks_a_result(self):
        from app.run_lifecycle import (
            CancelRunCommand,
            ConfirmProfileAnswer,
            ResumeRunCommand,
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )
        from app.run_lifecycle.worker import RunLifecycleWorker

        optimize_started = asyncio.Event()
        continue_optimize = asyncio.Event()

        class BlockingAlgo(FakeAlgoClient):
            async def OptimizeWishlist(self, request):
                self.calls.append(("OptimizeWishlist", request))
                optimize_started.set()
                await continue_optimize.wait()
                return algo_pb2.OptimizeWishlistReply(
                    wishlist=algo_pb2.WishlistResult(
                        candidates=[
                            algo_pb2.CandidateWithScore(
                                cwp=self.candidate,
                                utility=algo_pb2.UtilityScore(score=0.8),
                                final_rank=1,
                            )
                        ],
                        report=algo_pb2.OptimizationReport(
                            stable_count=1,
                            safe_guard_ok=False,
                        ),
                    )
                )

        await self.seed_release()
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=False,
            worker_owner="worker-cancel-race",
            algo_client=BlockingAlgo(),
        )
        self.addAsyncCleanup(lifecycle.close)
        worker = RunLifecycleWorker(lifecycle, poll_interval=0.01)
        await worker.start()
        self.addAsyncCleanup(worker.stop)
        submitted = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-a",
                session_id=None,
                task_kind="GENERATE_PLAN",
                input=StructuredProfileInput(
                    province="湖南",
                    application_year=2025,
                    category="物理类",
                    batch="本科批",
                    score=600,
                    subjects=("物理", "化学", "生物"),
                ),
                idempotency_key="cancel-publish-submit",
            )
        )
        waiting = await self._wait_for_state(
            lifecycle,
            submitted.run_id,
            "WAITING_HITL",
        )
        await lifecycle.ResumeRun(
            ResumeRunCommand(
                principal_id="principal-a",
                run_id=submitted.run_id,
                interrupt_id=waiting.current_interrupt.interrupt_id,
                expected_state_version=waiting.state_version,
                profile_version=UUID(
                    waiting.current_interrupt.payload["profile_version"]
                ),
                answer=ConfirmProfileAnswer(),
                idempotency_key="cancel-publish-confirm",
            )
        )
        await asyncio.wait_for(optimize_started.wait(), timeout=2)

        cancelled_receipt = await lifecycle.CancelRun(
            CancelRunCommand(
                principal_id="principal-a",
                run_id=submitted.run_id,
                idempotency_key="cancel-before-publish",
            )
        )
        continue_optimize.set()
        stopped = await self._wait_for_state(
            lifecycle,
            submitted.run_id,
            "terminal",
        )

        self.assertTrue(cancelled_receipt.accepted)
        self.assertEqual(stopped.state, "CANCELLED")
        self.assertEqual(stopped.terminal_reason, "CANCELLED_BY_USER")
        self.assertIsNone(stopped.result)
        self.assertIsNone(stopped.result_payload)

    async def test_stale_lease_epoch_cannot_overwrite_a_newer_preview(self):
        from app.run_lifecycle import (
            ConfirmProfileAnswer,
            ResumeRunCommand,
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )
        from app.run_lifecycle.worker import RunLifecycleWorker

        now = [datetime(2026, 11, 18, 8, 0, tzinfo=timezone.utc)]
        stale_optimize_started = asyncio.Event()
        allow_stale_optimize = asyncio.Event()

        class StaleAlgo(FakeAlgoClient):
            async def OptimizeWishlist(self, request):
                stale_optimize_started.set()
                await allow_stale_optimize.wait()
                return await FakeAlgoClient.OptimizeWishlist(self, request)

        await self.seed_release()
        old_algo = StaleAlgo()
        old_lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=False,
            worker_owner="worker-stale-preview",
            lease_duration=timedelta(seconds=30),
            clock=lambda: now[0],
            algo_client=old_algo,
        )
        self.addAsyncCleanup(old_lifecycle.close)
        old_worker = RunLifecycleWorker(old_lifecycle, poll_interval=0.01)
        await old_worker.start()
        self.addAsyncCleanup(old_worker.stop)
        submitted = await old_lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-a",
                session_id=None,
                task_kind="GENERATE_PLAN",
                input=StructuredProfileInput(
                    province="湖南",
                    application_year=2025,
                    category="物理类",
                    batch="本科批",
                    score=600,
                    subjects=("物理", "化学", "生物"),
                ),
                idempotency_key="stale-preview-submit",
            )
        )
        waiting = await self._wait_for_state(
            old_lifecycle,
            submitted.run_id,
            "WAITING_HITL",
        )
        await old_lifecycle.ResumeRun(
            ResumeRunCommand(
                principal_id="principal-a",
                run_id=submitted.run_id,
                interrupt_id=waiting.current_interrupt.interrupt_id,
                expected_state_version=waiting.state_version,
                profile_version=UUID(
                    waiting.current_interrupt.payload["profile_version"]
                ),
                answer=ConfirmProfileAnswer(),
                idempotency_key="stale-preview-confirm",
            )
        )
        await asyncio.wait_for(stale_optimize_started.wait(), timeout=2)

        now[0] += timedelta(seconds=31)
        fresh_algo = FakeAlgoClient()
        fresh_lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=False,
            worker_owner="worker-fresh-preview",
            lease_duration=timedelta(seconds=30),
            clock=lambda: now[0],
            algo_client=fresh_algo,
        )
        self.addAsyncCleanup(fresh_lifecycle.close)
        fresh_worker = RunLifecycleWorker(fresh_lifecycle, poll_interval=0.01)
        await fresh_worker.start()
        self.addAsyncCleanup(fresh_worker.stop)
        published = await self._wait_for_state(
            fresh_lifecycle,
            submitted.run_id,
            "terminal",
        )
        result_version = published.result.result_version

        with self.assertLogs("app.run_lifecycle.worker", level="ERROR"):
            allow_stale_optimize.set()
            await asyncio.sleep(0.1)
        final = await fresh_lifecycle.GetRun(
            principal_id="principal-a",
            run_id=submitted.run_id,
        )

        self.assertEqual(published.state, "SUCCEEDED")
        self.assertEqual(final.state, "SUCCEEDED")
        self.assertEqual(final.result.result_version, result_version)
        self.assertEqual(final.result_payload, published.result_payload)
        self.assertEqual(len(old_algo.calls), 4)
        self.assertEqual(len(fresh_algo.calls), 4)

    async def test_incomplete_profile_never_reaches_algo_or_success(self):
        from app.run_lifecycle import (
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )
        from app.run_lifecycle.worker import RunLifecycleWorker

        await self.seed_release()
        algo = FakeAlgoClient()
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=False,
            worker_owner="worker-incomplete-profile",
            algo_client=algo,
        )
        self.addAsyncCleanup(lifecycle.close)
        worker = RunLifecycleWorker(lifecycle, poll_interval=0.01)
        await worker.start()
        self.addAsyncCleanup(worker.stop)
        submitted = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-a",
                session_id=None,
                task_kind="GENERATE_PLAN",
                input=StructuredProfileInput(
                    province="湖南",
                    application_year=2025,
                    category="物理类",
                    batch="本科批",
                    score=None,
                    subjects=("物理", "化学", "生物"),
                ),
                idempotency_key="incomplete-profile-submit",
            )
        )

        waiting = await self._wait_for_state(
            lifecycle,
            submitted.run_id,
            "WAITING_HITL",
        )

        self.assertEqual(waiting.current_interrupt.kind, "INTAKE_ASK")
        self.assertEqual(algo.calls, [])
        self.assertNotEqual(waiting.state, "SUCCEEDED")
        self.assertIsNone(waiting.result)

    async def test_unsupported_scope_never_reaches_algo(self):
        from app.run_lifecycle import (
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )
        from app.run_lifecycle.worker import RunLifecycleWorker

        await self.seed_release()
        algo = FakeAlgoClient()
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=False,
            worker_owner="worker-unsupported-scope",
            algo_client=algo,
        )
        self.addAsyncCleanup(lifecycle.close)
        worker = RunLifecycleWorker(lifecycle, poll_interval=0.01)
        await worker.start()
        self.addAsyncCleanup(worker.stop)
        submitted = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-a",
                session_id=None,
                task_kind="GENERATE_PLAN",
                input=StructuredProfileInput(
                    province="湖南",
                    application_year=2025,
                    category="历史类",
                    batch="本科批",
                    score=600,
                    subjects=("历史", "政治", "地理"),
                ),
                idempotency_key="unsupported-scope-submit",
            )
        )

        waiting = await self._wait_for_state(
            lifecycle,
            submitted.run_id,
            "WAITING_HITL",
        )

        finding_codes = {
            finding["code"]
            for finding in waiting.current_interrupt.payload["findings"]
        }
        self.assertEqual(waiting.current_interrupt.kind, "INTAKE_ASK")
        self.assertIn("PROFILE_UNSUPPORTED_SCOPE", finding_codes)
        self.assertEqual(algo.calls, [])
        self.assertIsNone(waiting.result)

    async def test_empty_optimized_wishlist_fails_without_result(self):
        class EmptyWishlistAlgo(FakeAlgoClient):
            async def OptimizeWishlist(self, request):
                self.calls.append(("OptimizeWishlist", request))
                return algo_pb2.OptimizeWishlistReply(
                    wishlist=algo_pb2.WishlistResult()
                )

        algo = EmptyWishlistAlgo()
        lifecycle, run_id, _ = await self._run_preview(
            algo,
            "empty-wishlist",
        )

        failed = await self._wait_for_state(lifecycle, run_id, "terminal")

        self.assertEqual(failed.state, "FAILED")
        self.assertEqual(failed.terminal_reason, "NO_RELIABLE_DATA")
        self.assertIsNone(failed.result)
        self.assertIsNone(failed.result_payload)

    async def test_same_profile_release_and_algo_output_are_reproducible(self):
        release_id = await self.seed_release()
        first_algo = FakeAlgoClient()
        first_lifecycle, first_run_id, _ = await self._run_preview(
            first_algo,
            "repro-first",
            release_id,
        )
        first = await self._wait_for_state(
            first_lifecycle,
            first_run_id,
            "terminal",
        )
        second_algo = FakeAlgoClient()
        second_lifecycle, second_run_id, _ = await self._run_preview(
            second_algo,
            "repro-second",
            release_id,
        )
        second = await self._wait_for_state(
            second_lifecycle,
            second_run_id,
            "terminal",
        )

        self.assertEqual(first.state, "SUCCEEDED")
        self.assertEqual(second.state, "SUCCEEDED")
        fact_keys = {
            "rank",
            "percentile",
            "rush_candidates",
            "stable_candidates",
            "safe_candidates",
            "rush_total",
            "stable_total",
            "safe_total",
            "audit_basis",
            "kept_candidates",
            "rejected_candidates",
            "wishlist",
            "algorithm_method",
            "algorithm_config_version",
            "unverified_conditions",
            "risk_notice",
            "disclaimer",
            "result_kind",
            "formal_recommendation",
            "internal_controlled_only",
        }
        self.assertEqual(
            {key: first.result_payload[key] for key in fact_keys},
            {key: second.result_payload[key] for key in fact_keys},
        )
        self.assertEqual(
            [
                {"key": item["key"], "status": item["status"]}
                for item in first.result_payload["verification_summary"]
            ],
            [
                {"key": item["key"], "status": item["status"]}
                for item in second.result_payload["verification_summary"]
            ],
        )

    async def test_get_and_reconnected_watch_replay_one_immutable_result(self):
        from app.run_lifecycle import RunLifecycle

        algo = FakeAlgoClient()
        lifecycle, run_id, _ = await self._run_preview(algo, "immutable-replay")
        first = await self._wait_for_state(lifecycle, run_id, "terminal")

        restarted = RunLifecycle(self.dsn)
        self.addAsyncCleanup(restarted.close)
        second = await restarted.GetRun(
            principal_id="principal-a",
            run_id=run_id,
        )
        third = await restarted.GetRun(
            principal_id="principal-a",
            run_id=run_id,
        )
        watched = []
        for _ in range(2):
            events = restarted.WatchRun(
                principal_id="principal-a",
                run_id=run_id,
                after_event_seq=0,
                poll_interval=0.01,
            )
            try:
                while True:
                    event = await anext(events)
                    if event.type == "RESULT":
                        watched.append(event)
                        break
            finally:
                await events.aclose()

        self.assertEqual(first.result.result_version, second.result.result_version)
        self.assertEqual(second.result.result_version, third.result.result_version)
        self.assertEqual(second.result_payload, third.result_payload)
        self.assertEqual(
            watched[0].payload["result_version"],
            str(second.result.result_version),
        )
        self.assertEqual(watched[0].payload, watched[1].payload)
        self.assertEqual(len(algo.calls), 4)

    async def test_all_candidates_filtered_never_calls_optimize_or_publishes(self):
        from app.run_lifecycle import (
            ConfirmProfileAnswer,
            ResumeRunCommand,
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )

        class AllFilteredAlgo(FakeAlgoClient):
            async def CheckConstraints(self, request):
                self.calls.append(("CheckConstraints", request))
                return algo_pb2.CheckConstraintsReply(
                    rejected=[
                        algo_pb2.RejectedCandidate(
                            candidate=self.candidate,
                            rule="subject_req",
                            reason="选科不符",
                        )
                    ]
                )

            async def OptimizeWishlist(self, request):
                self.calls.append(("OptimizeWishlist", request))
                raise AssertionError("空 kept 池不得进入 OptimizeWishlist")

        await self.seed_release()
        algo = AllFilteredAlgo()
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="worker-preview-gates",
            algo_client=algo,
        )
        self.addAsyncCleanup(lifecycle.close)
        from app.run_lifecycle.worker import RunLifecycleWorker

        worker = RunLifecycleWorker(lifecycle, poll_interval=0.01)
        await worker.start()
        self.addAsyncCleanup(worker.stop)

        submitted = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-a",
                session_id=None,
                task_kind="GENERATE_PLAN",
                input=StructuredProfileInput(
                    province="湖南",
                    application_year=2025,
                    category="物理类",
                    batch="本科批",
                    score=600,
                    subjects=("物理", "化学", "生物"),
                ),
                idempotency_key="preview-gates-all-filtered-submit",
            )
        )
        waiting = await self._wait_for_state(
            lifecycle,
            submitted.run_id,
            "WAITING_HITL",
        )
        await lifecycle.ResumeRun(
            ResumeRunCommand(
                principal_id="principal-a",
                run_id=submitted.run_id,
                interrupt_id=waiting.current_interrupt.interrupt_id,
                expected_state_version=waiting.state_version,
                profile_version=UUID(
                    waiting.current_interrupt.payload["profile_version"]
                ),
                answer=ConfirmProfileAnswer(),
                idempotency_key="preview-gates-all-filtered-confirm",
            )
        )

        failed = await self._wait_for_state(lifecycle, submitted.run_id, "terminal")

        self.assertEqual(failed.state, "FAILED")
        self.assertEqual(failed.terminal_reason, "NO_RELIABLE_DATA")
        self.assertIsNone(failed.result)
        self.assertIsNone(failed.result_payload)
        self.assertEqual(
            [name for name, _ in algo.calls],
            ["RankConvert", "EstimateProb", "CheckConstraints"],
        )

    async def _wait_for_state(self, lifecycle, run_id, target: str):
        for _ in range(300):
            view = await lifecycle.GetRun(
                principal_id="principal-a",
                run_id=run_id,
            )
            if target == "WAITING_HITL" and (
                view.state == "WAITING_HITL"
                and view.current_interrupt is not None
            ):
                return view
            if target == "terminal" and view.state in {
                "EXPIRED",
                "SUCCEEDED",
                "FAILED",
                "CANCELLED",
            }:
                return view
            await asyncio.sleep(0.01)
        self.fail(f"run 未在超时内达到 {target}")
