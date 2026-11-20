"""#45：敏感资格数据使用前的 principal consent 闸门。"""
from __future__ import annotations

import asyncio
import json
import unittest
from uuid import UUID

from run_lifecycle_support import RunLifecyclePostgresTest
from test_run_lifecycle_algorithm_preview import FakeAlgoClient


class ConsentLifecycleTests(RunLifecyclePostgresTest):
    async def test_sensitive_eligibility_use_without_consent_fails_closed(self):
        principal_id = "principal-consent-required"
        lifecycle = await self._start_lifecycle(
            worker_owner="worker-consent-required",
        )
        run_id, confirmation = await self._sensitive_confirmation(
            lifecycle,
            principal_id=principal_id,
            suffix="consent-required",
        )
        await self._confirm(
            lifecycle,
            principal_id=principal_id,
            run_id=run_id,
            confirmation=confirmation,
            suffix="consent-required",
        )

        completed = await self._wait_for_terminal(
            lifecycle,
            principal_id=principal_id,
            run_id=run_id,
        )

        self.assertEqual(completed.state, "FAILED")
        self.assertEqual(completed.terminal_reason, "CONSENT_REQUIRED")
        self.assertIsNone(completed.result)
        self.assertIsNone(completed.formal_result)

    async def test_revoked_consent_blocks_new_sensitive_eligibility_use(self):
        principal_id = "principal-consent-revoked"
        lifecycle = await self._start_lifecycle(
            worker_owner="worker-consent-revoked",
        )
        run_id, confirmation = await self._sensitive_confirmation(
            lifecycle,
            principal_id=principal_id,
            suffix="consent-revoked",
        )
        await self.seed_consent(principal_id=principal_id, revoked=True)
        await self._confirm(
            lifecycle,
            principal_id=principal_id,
            run_id=run_id,
            confirmation=confirmation,
            suffix="consent-revoked",
        )

        completed = await self._wait_for_terminal(
            lifecycle,
            principal_id=principal_id,
            run_id=run_id,
        )

        self.assertEqual(completed.state, "FAILED")
        self.assertEqual(completed.terminal_reason, "CONSENT_REVOKED")
        self.assertIsNone(completed.result)

    async def test_matching_consent_allows_use_without_exposing_sensitive_values(self):
        principal_id = "principal-consent-valid"
        lifecycle = await self._start_lifecycle(
            worker_owner="worker-consent-valid",
        )
        run_id, confirmation = await self._sensitive_confirmation(
            lifecycle,
            principal_id=principal_id,
            suffix="consent-valid",
        )
        consent_id = await self.seed_consent(
            principal_id=principal_id,
            guardian_ack_recorded=True,
        )
        conn = await self._connect()
        try:
            consent = await (
                await conn.execute(
                    """
                    SELECT principal_id, purpose, data_classes,
                           policy_version, granted_at, revoked_at,
                           guardian_ack_recorded
                    FROM consent_record
                    WHERE consent_id = %s
                    """,
                    (consent_id,),
                )
            ).fetchone()
        finally:
            await conn.close()
        self.assertEqual(consent["principal_id"], principal_id)
        self.assertEqual(
            consent["purpose"],
            "SENSITIVE_ELIGIBILITY_EVALUATION",
        )
        self.assertEqual(
            consent["data_classes"],
            ["SENSITIVE_ELIGIBILITY_DATA"],
        )
        self.assertEqual(consent["policy_version"], "m5b-consent-v1")
        self.assertIsNotNone(consent["granted_at"])
        self.assertIsNone(consent["revoked_at"])
        self.assertTrue(consent["guardian_ack_recorded"])
        await self._confirm(
            lifecycle,
            principal_id=principal_id,
            run_id=run_id,
            confirmation=confirmation,
            suffix="consent-valid",
        )

        completed = await self._wait_for_terminal(
            lifecycle,
            principal_id=principal_id,
            run_id=run_id,
        )

        self.assertEqual(completed.state, "SUCCEEDED")
        self.assertEqual(completed.result.result_kind, "V1_FORMAL")
        events = []
        stream = lifecycle.WatchRun(
            principal_id=principal_id,
            run_id=run_id,
            after_event_seq=0,
            poll_interval=0.01,
        )
        try:
            while True:
                event = await anext(stream)
                events.append(event)
                if event.type in {"RESULT", "ERROR"}:
                    break
        finally:
            await stream.aclose()
        exposed = json.dumps(
            {
                "events": [str(event) for event in events],
                "result": completed.result_payload,
                "formal_result": (
                    completed.formal_result.verification_report
                    if completed.formal_result is not None
                    else None
                ),
            },
            ensure_ascii=False,
        )
        self.assertNotIn("COLOR_WEAK", exposed)
        self.assertNotIn("色弱", exposed)

    async def test_wrong_purpose_or_stale_policy_does_not_authorize_sensitive_use(self):
        lifecycle = await self._start_lifecycle(
            worker_owner="worker-consent-scope",
        )
        cases = (
            (
                "principal-consent-wrong-purpose",
                {
                    "purpose": "PLAN_GENERATION",
                },
            ),
            (
                "principal-consent-stale-policy",
                {
                    "policy_version": "m5b-consent-v0",
                },
            ),
            (
                "principal-consent-wrong-class",
                {
                    "data_classes": ("PUBLIC_DATA",),
                },
            ),
        )
        for principal_id, consent in cases:
            with self.subTest(principal_id=principal_id):
                run_id, confirmation = await self._sensitive_confirmation(
                    lifecycle,
                    principal_id=principal_id,
                    suffix=principal_id,
                )
                await self.seed_consent(
                    principal_id=principal_id,
                    **consent,
                )
                await self._confirm(
                    lifecycle,
                    principal_id=principal_id,
                    run_id=run_id,
                    confirmation=confirmation,
                    suffix=principal_id,
                )
                completed = await self._wait_for_terminal(
                    lifecycle,
                    principal_id=principal_id,
                    run_id=run_id,
                )
                self.assertEqual(completed.state, "FAILED")
                self.assertEqual(
                    completed.terminal_reason,
                    "CONSENT_REQUIRED",
                )
                self.assertIsNone(completed.result)

    async def _start_lifecycle(self, *, worker_owner):
        from app.run_lifecycle import RunLifecycle
        from app.run_lifecycle.worker import RunLifecycleWorker

        await self.seed_release()
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner=worker_owner,
            algo_client=FakeAlgoClient(),
        )
        self.addAsyncCleanup(lifecycle.close)
        worker = RunLifecycleWorker(lifecycle, poll_interval=0.01)
        await worker.start()
        self.addAsyncCleanup(worker.stop)
        return lifecycle

    async def _sensitive_confirmation(
        self,
        lifecycle,
        *,
        principal_id,
        suffix,
    ):
        from app.run_lifecycle import (
            RejectOrEditProfileAnswer,
            ResumeRunCommand,
            StructuredProfileInput,
            SubmitRunCommand,
        )

        submitted = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id=principal_id,
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
        first = await self._wait_for_interrupt(
            lifecycle,
            principal_id=principal_id,
            run_id=submitted.run_id,
        )
        await lifecycle.ResumeRun(
            ResumeRunCommand(
                principal_id=principal_id,
                run_id=submitted.run_id,
                interrupt_id=first.current_interrupt.interrupt_id,
                expected_state_version=first.state_version,
                profile_version=UUID(
                    first.current_interrupt.payload["profile_version"]
                ),
                answer=RejectOrEditProfileAnswer(
                    patch={
                        "physical_exam.color_vision": {
                            "state": "PROVIDED",
                            "source": "USER_FORM",
                            "value": "COLOR_WEAK",
                        }
                    }
                ),
                idempotency_key=f"{suffix}-add-sensitive",
            )
        )
        confirmation = await self._wait_for_interrupt(
            lifecycle,
            principal_id=principal_id,
            run_id=submitted.run_id,
        )
        return submitted.run_id, confirmation

    async def _confirm(
        self,
        lifecycle,
        *,
        principal_id,
        run_id,
        confirmation,
        suffix,
    ):
        from app.run_lifecycle import ConfirmProfileAnswer, ResumeRunCommand

        await lifecycle.ResumeRun(
            ResumeRunCommand(
                principal_id=principal_id,
                run_id=run_id,
                interrupt_id=confirmation.current_interrupt.interrupt_id,
                expected_state_version=confirmation.state_version,
                profile_version=UUID(
                    confirmation.current_interrupt.payload["profile_version"]
                ),
                answer=ConfirmProfileAnswer(),
                idempotency_key=f"{suffix}-confirm",
            )
        )

    async def _wait_for_interrupt(self, lifecycle, *, principal_id, run_id):
        for _ in range(300):
            view = await lifecycle.GetRun(
                principal_id=principal_id,
                run_id=run_id,
            )
            if view.state == "WAITING_HITL" and view.current_interrupt is not None:
                return view
            await asyncio.sleep(0.01)
        self.fail("run did not expose a profile interrupt")

    async def _wait_for_terminal(self, lifecycle, *, principal_id, run_id):
        for _ in range(300):
            view = await lifecycle.GetRun(
                principal_id=principal_id,
                run_id=run_id,
            )
            if view.state in {
                "EXPIRED",
                "SUCCEEDED",
                "FAILED",
                "CANCELLED",
            }:
                return view
            await asyncio.sleep(0.01)
        self.fail("run did not reach a terminal state")


if __name__ == "__main__":
    unittest.main()
