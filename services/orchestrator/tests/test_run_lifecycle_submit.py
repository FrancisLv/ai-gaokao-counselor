"""Ticket #2：持久受理、release 绑定与事件读侧。"""
from __future__ import annotations

import asyncio

from run_lifecycle_support import RunLifecyclePostgresTest


class SubmitRunTests(RunLifecyclePostgresTest):
    async def test_submit_returns_durable_receipt_and_initial_event(self):
        from app.run_lifecycle import RunLifecycle, StructuredProfileInput, SubmitRunCommand

        release_id = await self.seed_release()
        lifecycle = RunLifecycle(self.dsn)

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
                idempotency_key="submit-1",
            )
        )

        self.assertTrue(receipt.accepted)
        self.assertEqual(receipt.state, "QUEUED")
        self.assertEqual(receipt.first_event_seq, 1)
        self.assertFalse(receipt.replayed)

        view = await lifecycle.GetRun(
            principal_id="principal-a",
            run_id=receipt.run_id,
        )
        self.assertEqual(view.release_id, release_id)
        self.assertEqual(view.state, "QUEUED")
        self.assertIsNone(view.result)

        events = lifecycle.WatchRun(
            principal_id="principal-a",
            run_id=receipt.run_id,
            after_event_seq=0,
            poll_interval=0.01,
        )
        event = await anext(events)
        await events.aclose()
        self.assertEqual(event.event_seq, 1)
        self.assertEqual(event.type, "STATE_CHANGED")
        self.assertEqual(event.payload["to_state"], "QUEUED")

    async def test_retried_submit_replays_the_original_receipt(self):
        from app.run_lifecycle import RunLifecycle, StructuredProfileInput, SubmitRunCommand

        await self.seed_release()
        lifecycle = RunLifecycle(self.dsn)
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
            idempotency_key="submit-replay",
        )

        first = await lifecycle.SubmitRun(command)
        replay = await lifecycle.SubmitRun(command)

        self.assertEqual(replay.run_id, first.run_id)
        self.assertEqual(replay.session_id, first.session_id)
        self.assertTrue(replay.replayed)

    async def test_concurrent_submit_with_one_key_creates_only_one_run(self):
        from app.run_lifecycle import RunLifecycle, StructuredProfileInput, SubmitRunCommand

        await self.seed_release()
        lifecycle = RunLifecycle(self.dsn)
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
            idempotency_key="submit-concurrent",
        )

        receipts = await asyncio.gather(
            *(lifecycle.SubmitRun(command) for _ in range(4))
        )

        self.assertEqual(len({receipt.run_id for receipt in receipts}), 1)
        self.assertEqual(sum(receipt.replayed for receipt in receipts), 3)

    async def test_same_idempotency_key_with_different_input_conflicts(self):
        from app.run_lifecycle import (
            RunLifecycle,
            RunLifecycleError,
            StructuredProfileInput,
            SubmitRunCommand,
        )

        await self.seed_release()
        lifecycle = RunLifecycle(self.dsn)
        base = dict(
            principal_id="principal-a",
            session_id=None,
            task_kind="GENERATE_PLAN",
            idempotency_key="submit-conflict",
        )
        first_input = StructuredProfileInput(
            province="湖南",
            application_year=2025,
            category="物理类",
            batch="本科批",
            score=600,
            subjects=("物理", "化学", "生物"),
        )
        changed_input = StructuredProfileInput(
            province="湖南",
            application_year=2025,
            category="物理类",
            batch="本科批",
            score=580,
            subjects=("物理", "化学", "生物"),
        )

        await lifecycle.SubmitRun(SubmitRunCommand(input=first_input, **base))
        with self.assertRaises(RunLifecycleError) as raised:
            await lifecycle.SubmitRun(SubmitRunCommand(input=changed_input, **base))

        self.assertEqual(raised.exception.reason, "IDEMPOTENCY_CONFLICT")

    async def test_submit_without_an_active_release_is_rejected(self):
        from app.run_lifecycle import (
            RunLifecycle,
            RunLifecycleError,
            StructuredProfileInput,
            SubmitRunCommand,
        )

        await self.seed_release(status="STAGING")
        lifecycle = RunLifecycle(self.dsn)

        with self.assertRaises(RunLifecycleError) as raised:
            await lifecycle.SubmitRun(
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
                    idempotency_key="submit-no-release",
                )
            )

        self.assertEqual(raised.exception.reason, "RELEASE_UNAVAILABLE")

    async def test_submit_cannot_attach_to_another_principals_session(self):
        from app.run_lifecycle import (
            RunLifecycle,
            RunLifecycleError,
            StructuredProfileInput,
            SubmitRunCommand,
        )

        await self.seed_release()
        lifecycle = RunLifecycle(self.dsn)
        profile = StructuredProfileInput(
            province="湖南",
            application_year=2025,
            category="物理类",
            batch="本科批",
            score=600,
            subjects=("物理", "化学", "生物"),
        )
        first = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-a",
                session_id=None,
                task_kind="GENERATE_PLAN",
                input=profile,
                idempotency_key="owner-session",
            )
        )

        with self.assertRaises(RunLifecycleError) as raised:
            await lifecycle.SubmitRun(
                SubmitRunCommand(
                    principal_id="principal-b",
                    session_id=first.session_id,
                    task_kind="GENERATE_PLAN",
                    input=profile,
                    idempotency_key="foreign-session",
                )
            )

        self.assertEqual(raised.exception.reason, "RUN_FORBIDDEN")

    async def test_get_run_hides_another_principals_run(self):
        from app.run_lifecycle import (
            RunLifecycle,
            RunLifecycleError,
            StructuredProfileInput,
            SubmitRunCommand,
        )

        await self.seed_release()
        lifecycle = RunLifecycle(self.dsn)
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
                idempotency_key="private-run",
            )
        )

        with self.assertRaises(RunLifecycleError) as raised:
            await lifecycle.GetRun(
                principal_id="principal-b",
                run_id=receipt.run_id,
            )

        self.assertEqual(raised.exception.reason, "RUN_NOT_FOUND")

    async def test_cancel_queued_run_produces_a_stable_terminal_state(self):
        from app.run_lifecycle import (
            CancelRunCommand,
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )

        await self.seed_release()
        lifecycle = RunLifecycle(self.dsn)
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
                idempotency_key="cancel-submit",
            )
        )

        cancelled = await lifecycle.CancelRun(
            CancelRunCommand(
                principal_id="principal-a",
                run_id=submitted.run_id,
                idempotency_key="cancel-1",
            )
        )
        view = await lifecycle.GetRun(principal_id="principal-a", run_id=submitted.run_id)
        repeated = await lifecycle.CancelRun(
            CancelRunCommand(
                principal_id="principal-a",
                run_id=submitted.run_id,
                idempotency_key="cancel-2",
            )
        )

        self.assertTrue(cancelled.accepted)
        self.assertEqual(view.state, "CANCELLED")
        self.assertEqual(view.terminal_reason, "CANCELLED_BY_USER")
        self.assertFalse(repeated.accepted)
        self.assertEqual(repeated.reason, "RUN_ALREADY_TERMINAL")
        self.assertEqual(repeated.state, "CANCELLED")

    async def test_retried_cancel_replays_the_original_receipt(self):
        from app.run_lifecycle import (
            CancelRunCommand,
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )

        await self.seed_release()
        lifecycle = RunLifecycle(self.dsn)
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
                idempotency_key="cancel-replay-submit",
            )
        )
        command = CancelRunCommand(
            principal_id="principal-a",
            run_id=submitted.run_id,
            idempotency_key="cancel-replay",
        )

        first = await lifecycle.CancelRun(command)
        replay = await lifecycle.CancelRun(command)

        self.assertTrue(replay.accepted)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.state_version, first.state_version)
        self.assertEqual(replay.first_event_seq, first.first_event_seq)

    async def test_concurrent_cancel_commands_commit_only_one_terminal_change(self):
        from app.run_lifecycle import (
            CancelRunCommand,
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )

        await self.seed_release()
        lifecycle = RunLifecycle(self.dsn)
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
                idempotency_key="concurrent-cancel-submit",
            )
        )

        receipts = await asyncio.gather(
            *(
                lifecycle.CancelRun(
                    CancelRunCommand(
                        principal_id="principal-a",
                        run_id=submitted.run_id,
                        idempotency_key=f"cancel-{index}",
                    )
                )
                for index in range(2)
            )
        )
        view = await lifecycle.GetRun(
            principal_id="principal-a",
            run_id=submitted.run_id,
        )

        self.assertEqual(sum(receipt.accepted for receipt in receipts), 1)
        self.assertEqual(view.state, "CANCELLED")
        self.assertEqual(view.state_version, 2)

    async def test_cancelled_run_event_can_be_replayed_from_its_cursor(self):
        from app.run_lifecycle import (
            CancelRunCommand,
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )

        await self.seed_release()
        lifecycle = RunLifecycle(self.dsn)
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
                idempotency_key="watch-submit",
            )
        )
        cancelled = await lifecycle.CancelRun(
            CancelRunCommand(
                principal_id="principal-a",
                run_id=submitted.run_id,
                idempotency_key="watch-cancel",
            )
        )

        events = lifecycle.WatchRun(
            principal_id="principal-a",
            run_id=submitted.run_id,
            after_event_seq=1,
            poll_interval=0.01,
        )
        event = await anext(events)
        await events.aclose()
        view_after_watch = await lifecycle.GetRun(
            principal_id="principal-a",
            run_id=submitted.run_id,
        )

        self.assertEqual(event.event_seq, cancelled.first_event_seq)
        self.assertEqual(event.event_seq, 2)
        self.assertEqual(event.payload["to_state"], "CANCELLED")
        self.assertEqual(view_after_watch.state_version, cancelled.state_version)

    async def test_watch_run_hides_another_principals_run(self):
        from app.run_lifecycle import (
            RunLifecycle,
            RunLifecycleError,
            StructuredProfileInput,
            SubmitRunCommand,
        )

        await self.seed_release()
        lifecycle = RunLifecycle(self.dsn)
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
                idempotency_key="watch-private-submit",
            )
        )
        events = lifecycle.WatchRun(
            principal_id="principal-b",
            run_id=submitted.run_id,
            after_event_seq=0,
        )

        with self.assertRaises(RunLifecycleError) as raised:
            await anext(events)
        await events.aclose()

        self.assertEqual(raised.exception.reason, "RUN_NOT_FOUND")

    async def test_watch_rejects_a_cursor_older_than_event_retention(self):
        from app.run_lifecycle import (
            RunLifecycle,
            RunLifecycleError,
            StructuredProfileInput,
            SubmitRunCommand,
        )

        await self.seed_release()
        lifecycle = RunLifecycle(self.dsn)
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
                idempotency_key="expired-cursor-submit",
            )
        )
        await self.age_events_outside_retention(submitted.run_id)
        events = lifecycle.WatchRun(
            principal_id="principal-a",
            run_id=submitted.run_id,
            after_event_seq=0,
        )

        with self.assertRaises(RunLifecycleError) as raised:
            await anext(events)
        await events.aclose()

        self.assertEqual(raised.exception.reason, "EVENT_CURSOR_EXPIRED")
