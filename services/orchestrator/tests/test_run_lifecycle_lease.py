"""Ticket #4：worker 租约、fencing 与恢复行为。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from run_lifecycle_support import RunLifecyclePostgresTest


class LeaseLifecycleTests(RunLifecyclePostgresTest):
    async def test_submit_claim_records_running_lease_then_waits_for_profile(self):
        import asyncio

        from app.run_lifecycle import RunLifecycle, StructuredProfileInput, SubmitRunCommand

        await self.seed_release()
        now = datetime(2026, 11, 18, 8, 0, tzinfo=timezone.utc)
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="worker-a",
            lease_duration=timedelta(seconds=30),
            clock=lambda: now,
        )
        self.addAsyncCleanup(lifecycle.close)

        receipt = await lifecycle.SubmitRun(
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
                idempotency_key="lease-lifecycle-submit",
            )
        )
        view = None
        for _ in range(200):
            view = await lifecycle.GetRun(
                principal_id="principal-a",
                run_id=receipt.run_id,
            )
            if view.current_interrupt is not None:
                break
            await asyncio.sleep(0.01)
        else:
            self.fail("run 未在超时内进入 interrupt")
        events = lifecycle.WatchRun(
            principal_id="principal-a",
            run_id=receipt.run_id,
            after_event_seq=0,
            poll_interval=0.01,
        )
        transitions = []
        try:
            while True:
                event = await anext(events)
                if event.type == "STATE_CHANGED":
                    transitions.append(
                        (event.payload["from_state"], event.payload["to_state"])
                    )
                if event.state_version >= view.state_version:
                    break
        finally:
            await events.aclose()

        self.assertEqual(view.state, "WAITING_HITL")
        self.assertIn(("QUEUED", "RUNNING"), transitions)
        self.assertIn(("RUNNING", "WAITING_HITL"), transitions)

    async def test_expired_lease_takeover_fences_the_old_worker(self):
        import asyncio

        from app.run_lifecycle import RunLifecycle, StructuredProfileInput, SubmitRunCommand

        await self.seed_release()
        now = [datetime(2026, 11, 18, 8, 0, tzinfo=timezone.utc)]
        first_claimed = asyncio.Event()
        allow_old_worker = asyncio.Event()

        async def pause_after_first_claim(_run_id, lease_epoch):
            if lease_epoch == 1:
                first_claimed.set()
                await allow_old_worker.wait()

        command = SubmitRunCommand(
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
            idempotency_key="lease-takeover-submit",
        )
        old_worker = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="worker-a",
            lease_duration=timedelta(seconds=30),
            clock=lambda: now[0],
            _after_claim=pause_after_first_claim,
        )
        self.addAsyncCleanup(old_worker.close)
        old_task = asyncio.create_task(old_worker.SubmitRun(command))
        await asyncio.wait_for(first_claimed.wait(), timeout=2)

        now[0] += timedelta(seconds=31)
        new_worker = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="worker-b",
            lease_duration=timedelta(seconds=30),
            clock=lambda: now[0],
        )
        self.addAsyncCleanup(new_worker.close)
        replay = await new_worker.SubmitRun(command)
        taken_over = None
        for _ in range(200):
            taken_over = await new_worker.GetRun(
                principal_id="principal-a",
                run_id=replay.run_id,
            )
            if taken_over.current_interrupt is not None:
                break
            await asyncio.sleep(0.01)
        else:
            self.fail("新 worker 未能在旧租约过期后接管")

        allow_old_worker.set()
        await old_task
        after_old_attempt = await new_worker.GetRun(
            principal_id="principal-a",
            run_id=replay.run_id,
        )
        events = new_worker.WatchRun(
            principal_id="principal-a",
            run_id=replay.run_id,
            after_event_seq=0,
            poll_interval=0.01,
        )
        claims = []
        try:
            while True:
                event = await anext(events)
                if (
                    event.type == "STATE_CHANGED"
                    and event.payload.get("to_state") == "RUNNING"
                ):
                    claims.append(
                        (
                            event.payload.get("lease_owner"),
                            event.payload.get("lease_epoch"),
                        )
                    )
                if event.state_version >= after_old_attempt.state_version:
                    break
        finally:
            await events.aclose()

        self.assertEqual(claims, [("worker-a", 1), ("worker-b", 2)])
        self.assertEqual(
            after_old_attempt.current_interrupt.interrupt_id,
            taken_over.current_interrupt.interrupt_id,
        )
        self.assertEqual(
            after_old_attempt.state_version,
            taken_over.state_version,
        )

    async def test_running_worker_over_budget_fails_without_opening_interrupt(self):
        import asyncio

        from app.run_lifecycle import RunLifecycle, StructuredProfileInput, SubmitRunCommand

        await self.seed_release()
        now = [datetime(2026, 11, 18, 8, 0, tzinfo=timezone.utc)]
        claimed = asyncio.Event()
        continue_worker = asyncio.Event()

        async def hold_after_claim(_run_id, _lease_epoch):
            claimed.set()
            await continue_worker.wait()

        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="worker-a",
            lease_duration=timedelta(minutes=20),
            clock=lambda: now[0],
            _after_claim=hold_after_claim,
        )
        self.addAsyncCleanup(lifecycle.close)
        task = asyncio.create_task(
            lifecycle.SubmitRun(
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
                    idempotency_key="active-timeout-submit",
                )
            )
        )
        await asyncio.wait_for(claimed.wait(), timeout=2)
        now[0] += timedelta(minutes=10, seconds=1)
        continue_worker.set()
        receipt = await task

        view = await lifecycle.GetRun(
            principal_id="principal-a",
            run_id=receipt.run_id,
        )
        self.assertEqual(view.state, "FAILED")
        self.assertEqual(view.terminal_reason, "EXECUTION_TIMEOUT")
        self.assertIsNone(view.current_interrupt)

    async def test_hitl_wait_preserves_budget_then_expires_after_thirty_minutes(self):
        import asyncio
        from uuid import UUID

        from app.run_lifecycle import (
            ConfirmProfileAnswer,
            ResumeRunCommand,
            RunLifecycle,
            RunLifecycleError,
            StructuredProfileInput,
            SubmitRunCommand,
        )

        await self.seed_release()
        now = [datetime(2026, 11, 18, 8, 0, tzinfo=timezone.utc)]
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="worker-a",
            clock=lambda: now[0],
        )
        self.addAsyncCleanup(lifecycle.close)
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
                idempotency_key="hitl-timeout-submit",
            )
        )
        waiting = None
        for _ in range(200):
            waiting = await lifecycle.GetRun(
                principal_id="principal-a",
                run_id=submitted.run_id,
            )
            if waiting.current_interrupt is not None:
                break
            await asyncio.sleep(0.01)
        else:
            self.fail("run 未进入 HITL")

        now[0] += timedelta(minutes=20)
        after_wait = await lifecycle.GetRun(
            principal_id="principal-a",
            run_id=submitted.run_id,
        )
        self.assertEqual(after_wait.state, "WAITING_HITL")
        self.assertEqual(
            after_wait.active_execution_remaining_ms,
            waiting.active_execution_remaining_ms,
        )

        now[0] += timedelta(minutes=11)
        with self.assertRaises(RunLifecycleError) as raised:
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
                    idempotency_key="hitl-timeout-confirm",
                )
            )
        expired = await lifecycle.GetRun(
            principal_id="principal-a",
            run_id=submitted.run_id,
        )

        self.assertEqual(raised.exception.reason, "INTERRUPT_EXPIRED")
        self.assertEqual(expired.state, "EXPIRED")
        self.assertEqual(expired.terminal_reason, "HITL_TIMEOUT")
        self.assertIsNone(expired.current_interrupt)
        self.assertEqual(
            expired.active_execution_remaining_ms,
            waiting.active_execution_remaining_ms,
        )

    async def test_running_cancel_takes_effect_at_the_worker_safe_point(self):
        import asyncio

        from app.run_lifecycle import (
            CancelRunCommand,
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )

        await self.seed_release()
        claimed = asyncio.Event()
        continue_worker = asyncio.Event()

        async def hold_after_claim(_run_id, _lease_epoch):
            claimed.set()
            await continue_worker.wait()

        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="worker-a",
            _after_claim=hold_after_claim,
        )
        self.addAsyncCleanup(lifecycle.close)
        task = asyncio.create_task(
            lifecycle.SubmitRun(
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
                    idempotency_key="running-cancel-submit",
                )
            )
        )
        await asyncio.wait_for(claimed.wait(), timeout=2)
        receipt = await lifecycle.SubmitRun(
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
                idempotency_key="running-cancel-submit",
            )
        )
        cancelled = await lifecycle.CancelRun(
            CancelRunCommand(
                principal_id="principal-a",
                run_id=receipt.run_id,
                idempotency_key="running-cancel",
            )
        )
        running = await lifecycle.GetRun(
            principal_id="principal-a",
            run_id=receipt.run_id,
        )
        continue_worker.set()
        await task
        stopped = await lifecycle.GetRun(
            principal_id="principal-a",
            run_id=receipt.run_id,
        )

        self.assertTrue(cancelled.accepted)
        self.assertEqual(running.state, "RUNNING")
        self.assertIsNotNone(running.cancel_requested_at)
        self.assertEqual(stopped.state, "CANCELLED")
        self.assertEqual(stopped.terminal_reason, "CANCELLED_BY_USER")
        self.assertIsNone(stopped.current_interrupt)

    async def test_waiting_hitl_cancellation_blocks_the_old_interrupt(self):
        import asyncio
        from uuid import UUID

        from app.run_lifecycle import (
            CancelRunCommand,
            ConfirmProfileAnswer,
            ResumeRunCommand,
            RunLifecycle,
            RunLifecycleError,
            StructuredProfileInput,
            SubmitRunCommand,
        )

        await self.seed_release()
        lifecycle = RunLifecycle(self.dsn, auto_profile=True)
        self.addAsyncCleanup(lifecycle.close)
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
                idempotency_key="hitl-cancel-submit",
            )
        )
        waiting = None
        for _ in range(200):
            waiting = await lifecycle.GetRun(
                principal_id="principal-a",
                run_id=submitted.run_id,
            )
            if waiting.current_interrupt is not None:
                break
            await asyncio.sleep(0.01)
        else:
            self.fail("run 未进入 HITL")

        cancelled = await lifecycle.CancelRun(
            CancelRunCommand(
                principal_id="principal-a",
                run_id=submitted.run_id,
                idempotency_key="hitl-cancel",
            )
        )
        stopped = await lifecycle.GetRun(
            principal_id="principal-a",
            run_id=submitted.run_id,
        )
        with self.assertRaises(RunLifecycleError) as raised:
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
                    idempotency_key="cancel-then-confirm",
                )
            )

        self.assertTrue(cancelled.accepted)
        self.assertEqual(stopped.state, "CANCELLED")
        self.assertEqual(stopped.terminal_reason, "CANCELLED_BY_USER")
        self.assertIsNone(stopped.current_interrupt)
        self.assertEqual(raised.exception.reason, "RUN_ALREADY_TERMINAL")

    async def test_internal_worker_claims_queued_work_without_a_watch_connection(self):
        import asyncio

        from app.run_lifecycle import RunLifecycle, StructuredProfileInput, SubmitRunCommand
        from app.run_lifecycle.worker import RunLifecycleWorker

        await self.seed_release()
        lifecycle = RunLifecycle(self.dsn)
        self.addAsyncCleanup(lifecycle.close)
        worker = RunLifecycleWorker(lifecycle, poll_interval=0.01)
        await worker.start()
        self.addAsyncCleanup(worker.stop)
        receipt = await lifecycle.SubmitRun(
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
                idempotency_key="background-worker-submit",
            )
        )

        for _ in range(200):
            view = await lifecycle.GetRun(
                principal_id="principal-a",
                run_id=receipt.run_id,
            )
            if view.current_interrupt is not None:
                break
            await asyncio.sleep(0.01)
        else:
            self.fail("worker 未领取 queued run")

        self.assertEqual(view.state, "WAITING_HITL")
        self.assertEqual(view.current_interrupt.kind, "PROFILE_CONFIRM")
