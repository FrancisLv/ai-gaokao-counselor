"""Ticket #3：画像 revision、interrupt 与官方 saver 恢复。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID

from run_lifecycle_support import RunLifecyclePostgresTest


class ProfileLifecycleTests(RunLifecyclePostgresTest):
    async def test_complete_profile_opens_a_versioned_confirmation_interrupt(self):
        from app.run_lifecycle import RunLifecycle, StructuredProfileInput, SubmitRunCommand

        await self.seed_release()
        lifecycle = RunLifecycle(self.dsn, auto_profile=True)
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
                    batch=None,
                    score=600,
                    subjects=("物理", "化学", "生物"),
                ),
                idempotency_key="profile-submit",
            )
        )

        view = await self._wait_for_interrupt(lifecycle, receipt.run_id)

        self.assertEqual(view.state, "WAITING_HITL")
        self.assertEqual(view.stage, "PROFILE_CONFIRM")
        self.assertIsNone(view.confirmed_profile_version)
        self.assertIsNotNone(view.current_interrupt)
        interrupt = view.current_interrupt
        self.assertEqual(interrupt.kind, "PROFILE_CONFIRM")
        self.assertEqual(interrupt.state, "OPEN")
        self.assertEqual(interrupt.payload["fields"]["batch"]["value"], "本科批")
        self.assertEqual(interrupt.payload["fields"]["batch"]["state"], "DEFAULT")
        self.assertIn("profile_version", interrupt.payload)

    async def test_confirm_profile_resumes_the_exact_revision(self):
        from app.run_lifecycle import (
            ConfirmProfileAnswer,
            ResumeRunCommand,
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )

        await self.seed_release()
        lifecycle = RunLifecycle(self.dsn, auto_profile=True)
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
                idempotency_key="confirm-profile-submit",
            )
        )
        waiting = await self._wait_for_interrupt(lifecycle, receipt.run_id)
        profile_version = UUID(waiting.current_interrupt.payload["profile_version"])

        resumed = await lifecycle.ResumeRun(
            ResumeRunCommand(
                principal_id="principal-a",
                run_id=receipt.run_id,
                interrupt_id=waiting.current_interrupt.interrupt_id,
                expected_state_version=waiting.state_version,
                profile_version=profile_version,
                answer=ConfirmProfileAnswer(),
                idempotency_key="confirm-profile",
            )
        )

        self.assertTrue(resumed.accepted)
        self.assertEqual(resumed.state, "QUEUED")
        view = await self._wait_for_confirmed_profile(lifecycle, receipt.run_id)
        self.assertEqual(view.confirmed_profile_version, profile_version)
        self.assertIsNone(view.current_interrupt)
        self.assertEqual(view.state, "QUEUED")

    async def test_missing_field_answer_creates_a_new_revision_before_confirmation(self):
        from app.run_lifecycle import (
            AnswerFields,
            ResumeRunCommand,
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )

        await self.seed_release()
        lifecycle = RunLifecycle(self.dsn, auto_profile=True)
        self.addAsyncCleanup(lifecycle.close)
        receipt = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-a",
                session_id=None,
                task_kind="GENERATE_PLAN",
                input=StructuredProfileInput(
                    province="湖南",
                    application_year=None,
                    category="物理类",
                    batch="本科批",
                    score=600,
                    subjects=("物理", "化学", "生物"),
                ),
                idempotency_key="missing-year-submit",
            )
        )
        asking = await self._wait_for_interrupt(lifecycle, receipt.run_id)
        self.assertEqual(asking.current_interrupt.kind, "INTAKE_ASK")
        old_version = UUID(asking.current_interrupt.payload["profile_version"])
        self.assertIn(
            "PROFILE_REQUIRED_MISSING",
            {finding["code"] for finding in asking.current_interrupt.payload["findings"]},
        )

        await lifecycle.ResumeRun(
            ResumeRunCommand(
                principal_id="principal-a",
                run_id=receipt.run_id,
                interrupt_id=asking.current_interrupt.interrupt_id,
                expected_state_version=asking.state_version,
                profile_version=old_version,
                answer=AnswerFields(
                    fields={
                        "application_year": {
                            "state": "PROVIDED",
                            "source": "USER_FORM",
                            "value": 2025,
                        }
                    }
                ),
                idempotency_key="answer-year",
            )
        )

        confirming = await self._wait_for_interrupt(lifecycle, receipt.run_id)
        self.assertEqual(confirming.current_interrupt.kind, "PROFILE_CONFIRM")
        new_version = UUID(confirming.current_interrupt.payload["profile_version"])
        self.assertNotEqual(new_version, old_version)
        self.assertEqual(
            confirming.current_interrupt.payload["fields"]["application_year"]["value"],
            2025,
        )

    async def test_retried_confirmation_returns_the_original_receipt_without_reopening_hitl(self):
        from app.run_lifecycle import (
            ConfirmProfileAnswer,
            ResumeRunCommand,
            RunLifecycle,
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
                idempotency_key="retry-confirm-submit",
            )
        )
        waiting = await self._wait_for_interrupt(lifecycle, submitted.run_id)
        command = ResumeRunCommand(
            principal_id="principal-a",
            run_id=submitted.run_id,
            interrupt_id=waiting.current_interrupt.interrupt_id,
            expected_state_version=waiting.state_version,
            profile_version=UUID(waiting.current_interrupt.payload["profile_version"]),
            answer=ConfirmProfileAnswer(),
            idempotency_key="retry-confirm",
        )

        first = await lifecycle.ResumeRun(command)
        replay = await lifecycle.ResumeRun(command)
        view = await lifecycle.GetRun(
            principal_id="principal-a",
            run_id=submitted.run_id,
        )

        self.assertTrue(first.accepted)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.state_version, first.state_version)
        self.assertIsNotNone(view.confirmed_profile_version)
        self.assertIsNone(view.current_interrupt)

    async def test_two_tabs_answering_one_interrupt_commit_only_one_command(self):
        from app.run_lifecycle import (
            AnswerFields,
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
                    application_year=None,
                    category="物理类",
                    batch="本科批",
                    score=600,
                    subjects=("物理", "化学", "生物"),
                ),
                idempotency_key="two-tabs-submit",
            )
        )
        waiting = await self._wait_for_interrupt(lifecycle, submitted.run_id)
        profile_version = UUID(waiting.current_interrupt.payload["profile_version"])

        async def answer(year, key):
            return await lifecycle.ResumeRun(
                ResumeRunCommand(
                    principal_id="principal-a",
                    run_id=submitted.run_id,
                    interrupt_id=waiting.current_interrupt.interrupt_id,
                    expected_state_version=waiting.state_version,
                    profile_version=profile_version,
                    answer=AnswerFields(
                        fields={
                            "application_year": {
                                "state": "PROVIDED",
                                "source": "USER_FORM",
                                "value": year,
                            }
                        }
                    ),
                    idempotency_key=key,
                )
            )

        results = await asyncio.gather(
            answer(2025, "tab-a"),
            answer(2026, "tab-b"),
            return_exceptions=True,
        )
        receipts = [result for result in results if not isinstance(result, Exception)]
        errors = [result for result in results if isinstance(result, Exception)]

        self.assertEqual(len(receipts), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RunLifecycleError)
        self.assertEqual(errors[0].reason, "INTERRUPT_CONSUMED")

    async def test_confirmation_bound_to_a_different_profile_version_is_rejected(self):
        from app.run_lifecycle import (
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
                idempotency_key="wrong-version-submit",
            )
        )
        waiting = await self._wait_for_interrupt(lifecycle, submitted.run_id)
        wrong_version = UUID("00000000-0000-0000-0000-000000000001")

        with self.assertRaises(RunLifecycleError) as raised:
            await lifecycle.ResumeRun(
                ResumeRunCommand(
                    principal_id="principal-a",
                    run_id=submitted.run_id,
                    interrupt_id=waiting.current_interrupt.interrupt_id,
                    expected_state_version=waiting.state_version,
                    profile_version=wrong_version,
                    answer=ConfirmProfileAnswer(),
                    idempotency_key="wrong-version-confirm",
                )
            )
        view = await lifecycle.GetRun(principal_id="principal-a", run_id=submitted.run_id)

        self.assertEqual(raised.exception.reason, "PROFILE_VERSION_CONFLICT")
        self.assertEqual(view.state, "WAITING_HITL")
        self.assertEqual(view.state_version, waiting.state_version)
        self.assertEqual(
            view.current_interrupt.interrupt_id,
            waiting.current_interrupt.interrupt_id,
        )

    async def test_resume_rejects_a_revoked_release_without_consuming_the_interrupt(self):
        from app.run_lifecycle import (
            ConfirmProfileAnswer,
            ResumeRunCommand,
            RunLifecycle,
            RunLifecycleError,
            StructuredProfileInput,
            SubmitRunCommand,
        )

        release_id = await self.seed_release()
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
                idempotency_key="revoked-release-submit",
            )
        )
        waiting = await self._wait_for_interrupt(lifecycle, submitted.run_id)
        await self.revoke_release(release_id)

        with self.assertRaises(RunLifecycleError) as raised:
            await lifecycle.ResumeRun(
                ResumeRunCommand(
                    principal_id="principal-a",
                    run_id=submitted.run_id,
                    interrupt_id=waiting.current_interrupt.interrupt_id,
                    expected_state_version=waiting.state_version,
                    profile_version=UUID(waiting.current_interrupt.payload["profile_version"]),
                    answer=ConfirmProfileAnswer(),
                    idempotency_key="revoked-release-confirm",
                )
            )
        view = await lifecycle.GetRun(principal_id="principal-a", run_id=submitted.run_id)

        self.assertEqual(raised.exception.reason, "RELEASE_REVOKED")
        self.assertEqual(view.state, "WAITING_HITL")
        self.assertEqual(
            view.current_interrupt.interrupt_id,
            waiting.current_interrupt.interrupt_id,
        )

    async def test_crash_after_checkpoint_never_exposes_interrupt_without_a_restart(self):
        from app.run_lifecycle import RunLifecycle, StructuredProfileInput, SubmitRunCommand

        await self.seed_release()
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
            idempotency_key="checkpoint-crash-submit",
        )

        class SimulatedCrash(RuntimeError):
            pass

        crashed_run_ids = []
        now = datetime(2026, 11, 18, 8, 0, tzinfo=timezone.utc)

        def crash_after_checkpoint(run_id):
            crashed_run_ids.append(run_id)
            raise SimulatedCrash()

        crashing = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="worker-a",
            clock=lambda: now,
            _checkpoint_persisted=crash_after_checkpoint,
        )
        self.addAsyncCleanup(crashing.close)
        with self.assertRaises(SimulatedCrash):
            await crashing.SubmitRun(command)
        await crashing.close()

        restarted = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="worker-b",
            clock=lambda: now + timedelta(seconds=31),
        )
        self.addAsyncCleanup(restarted.close)
        before_recovery = await restarted.GetRun(
            principal_id="principal-a",
            run_id=crashed_run_ids[0],
        )
        self.assertEqual(before_recovery.state, "RUNNING")
        self.assertIsNone(before_recovery.current_interrupt)

        replay = await restarted.SubmitRun(command)
        recovered = await self._wait_for_interrupt(restarted, replay.run_id)
        self.assertTrue(replay.replayed)
        self.assertEqual(recovered.current_interrupt.kind, "PROFILE_CONFIRM")

    async def test_resume_crash_after_receipt_applies_the_stored_answer_exactly_once(self):
        from app.run_lifecycle import (
            ConfirmProfileAnswer,
            ResumeRunCommand,
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )

        await self.seed_release()
        submitting = RunLifecycle(self.dsn, auto_profile=True)
        self.addAsyncCleanup(submitting.close)
        submitted = await submitting.SubmitRun(
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
                idempotency_key="resume-crash-submit",
            )
        )
        waiting = await self._wait_for_interrupt(submitting, submitted.run_id)
        command = ResumeRunCommand(
            principal_id="principal-a",
            run_id=submitted.run_id,
            interrupt_id=waiting.current_interrupt.interrupt_id,
            expected_state_version=waiting.state_version,
            profile_version=UUID(waiting.current_interrupt.payload["profile_version"]),
            answer=ConfirmProfileAnswer(),
            idempotency_key="resume-crash-confirm",
        )

        class SimulatedCrash(RuntimeError):
            pass

        crashing = RunLifecycle(
            self.dsn,
            auto_profile=True,
            _resume_accepted=lambda _run_id: (_ for _ in ()).throw(SimulatedCrash()),
        )
        self.addAsyncCleanup(crashing.close)
        with self.assertRaises(SimulatedCrash):
            await crashing.ResumeRun(command)
        await crashing.close()

        restarted = RunLifecycle(self.dsn, auto_profile=True)
        self.addAsyncCleanup(restarted.close)
        replay = await restarted.ResumeRun(command)
        view = await self._wait_for_confirmed_profile(restarted, submitted.run_id)
        second_replay = await restarted.ResumeRun(command)
        view_after_second_replay = await restarted.GetRun(
            principal_id="principal-a",
            run_id=submitted.run_id,
        )

        self.assertTrue(replay.replayed)
        self.assertTrue(second_replay.replayed)
        self.assertEqual(
            view.confirmed_profile_version,
            UUID(waiting.current_interrupt.payload["profile_version"]),
        )
        self.assertIsNone(view.current_interrupt)
        self.assertEqual(
            view_after_second_replay.state_version,
            view.state_version,
        )

    async def test_editing_a_confirmed_card_revises_the_draft_before_confirmation(self):
        from app.run_lifecycle import (
            RejectOrEditProfileAnswer,
            ResumeRunCommand,
            RunLifecycle,
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
                idempotency_key="edit-profile-submit",
            )
        )
        waiting = await self._wait_for_interrupt(lifecycle, submitted.run_id)
        old_version = UUID(waiting.current_interrupt.payload["profile_version"])

        await lifecycle.ResumeRun(
            ResumeRunCommand(
                principal_id="principal-a",
                run_id=submitted.run_id,
                interrupt_id=waiting.current_interrupt.interrupt_id,
                expected_state_version=waiting.state_version,
                profile_version=old_version,
                answer=RejectOrEditProfileAnswer(
                    patch={
                        "score": {
                            "state": "PROVIDED",
                            "source": "USER_FORM",
                            "value": 580,
                        }
                    }
                ),
                idempotency_key="edit-profile",
            )
        )

        revised = await self._wait_for_interrupt(lifecycle, submitted.run_id)
        new_version = UUID(revised.current_interrupt.payload["profile_version"])
        self.assertEqual(revised.current_interrupt.kind, "PROFILE_CONFIRM")
        self.assertNotEqual(new_version, old_version)
        self.assertEqual(
            revised.current_interrupt.payload["fields"]["score"]["value"],
            580,
        )
        self.assertIsNone(revised.confirmed_profile_version)

    async def test_invalid_subject_combination_is_sent_back_for_correction(self):
        from app.run_lifecycle import RunLifecycle, StructuredProfileInput, SubmitRunCommand

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
                    subjects=("历史", "化学", "生物"),
                ),
                idempotency_key="invalid-subjects-submit",
            )
        )

        asking = await self._wait_for_interrupt(lifecycle, submitted.run_id)
        self.assertEqual(asking.current_interrupt.kind, "INTAKE_ASK")
        self.assertIn(
            "PROFILE_INVALID_VALUE",
            {finding["code"] for finding in asking.current_interrupt.payload["findings"]},
        )
        self.assertIsNone(asking.confirmed_profile_version)

    async def test_rank_only_input_asks_for_score_without_reverse_conversion(self):
        from app.run_lifecycle import RunLifecycle, StructuredProfileInput, SubmitRunCommand

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
                    score=None,
                    subjects=("物理", "化学", "生物"),
                    rank=15860,
                ),
                idempotency_key="rank-only-submit",
            )
        )

        asking = await self._wait_for_interrupt(lifecycle, submitted.run_id)
        codes = {
            finding["code"]
            for finding in asking.current_interrupt.payload["findings"]
        }
        self.assertEqual(asking.current_interrupt.kind, "INTAKE_ASK")
        self.assertIn("PROFILE_UNSUPPORTED_INPUT", codes)
        self.assertIn("PROFILE_REQUIRED_MISSING", codes)
        self.assertNotIn("score", asking.current_interrupt.payload["fields"])

    async def test_scope_outside_the_bound_release_is_not_silently_fallback_adjusted(self):
        from app.run_lifecycle import RunLifecycle, StructuredProfileInput, SubmitRunCommand

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
                    application_year=2026,
                    category="物理类",
                    batch="本科批",
                    score=600,
                    subjects=("物理", "化学", "生物"),
                ),
                idempotency_key="unsupported-year-submit",
            )
        )

        asking = await self._wait_for_interrupt(lifecycle, submitted.run_id)
        self.assertEqual(asking.current_interrupt.kind, "INTAKE_ASK")
        self.assertIn(
            "PROFILE_UNSUPPORTED_SCOPE",
            {finding["code"] for finding in asking.current_interrupt.payload["findings"]},
        )
        self.assertEqual(
            asking.current_interrupt.payload["fields"]["application_year"]["value"],
            2026,
        )

    async def test_watch_run_replays_the_interrupt_after_a_contiguous_event_history(self):
        from app.run_lifecycle import RunLifecycle, StructuredProfileInput, SubmitRunCommand

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
                idempotency_key="watch-interrupt-submit",
            )
        )
        waiting = await self._wait_for_interrupt(lifecycle, submitted.run_id)

        events = lifecycle.WatchRun(
            principal_id="principal-a",
            run_id=submitted.run_id,
            after_event_seq=0,
            poll_interval=0.01,
        )
        replayed = []
        try:
            while True:
                event = await anext(events)
                replayed.append(event)
                if event.type == "INTERRUPT":
                    break
        finally:
            await events.aclose()
        view_after_watch = await lifecycle.GetRun(
            principal_id="principal-a",
            run_id=submitted.run_id,
        )

        self.assertEqual(
            [event.event_seq for event in replayed],
            list(range(1, len(replayed) + 1)),
        )
        self.assertEqual(replayed[-1].payload["interrupt_id"], str(waiting.current_interrupt.interrupt_id))
        self.assertEqual(view_after_watch.state_version, waiting.state_version)

    async def test_declined_field_stays_explicitly_unknown_across_revisions(self):
        from app.run_lifecycle import (
            DeclineFieldsAnswer,
            ResumeRunCommand,
            RunLifecycle,
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
                    application_year=None,
                    category="物理类",
                    batch="本科批",
                    score=600,
                    subjects=("物理", "化学", "生物"),
                ),
                idempotency_key="decline-field-submit",
            )
        )
        asking = await self._wait_for_interrupt(lifecycle, submitted.run_id)

        await lifecycle.ResumeRun(
            ResumeRunCommand(
                principal_id="principal-a",
                run_id=submitted.run_id,
                interrupt_id=asking.current_interrupt.interrupt_id,
                expected_state_version=asking.state_version,
                profile_version=UUID(asking.current_interrupt.payload["profile_version"]),
                answer=DeclineFieldsAnswer(field_keys=("application_year",)),
                idempotency_key="decline-field",
            )
        )

        revised = await self._wait_for_interrupt(lifecycle, submitted.run_id)
        application_year = revised.current_interrupt.payload["fields"]["application_year"]
        self.assertEqual(revised.current_interrupt.kind, "INTAKE_ASK")
        self.assertEqual(application_year["state"], "DECLINED")
        self.assertIsNone(application_year["value"])

    async def _wait_for_interrupt(self, lifecycle, run_id):
        for _ in range(200):
            view = await lifecycle.GetRun(principal_id="principal-a", run_id=run_id)
            if (
                view.state == "WAITING_HITL"
                and view.current_interrupt is not None
            ):
                return view
            await asyncio.sleep(0.01)
        self.fail("run 未在超时内进入 interrupt")

    async def _wait_for_confirmed_profile(self, lifecycle, run_id):
        for _ in range(200):
            view = await lifecycle.GetRun(principal_id="principal-a", run_id=run_id)
            if view.confirmed_profile_version is not None:
                return view
            await asyncio.sleep(0.01)
        self.fail("run 未在超时内确认画像")
