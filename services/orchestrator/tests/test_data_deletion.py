"""#46：principal 数据删除与即时访问封锁。"""
from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

from run_lifecycle_support import RunLifecyclePostgresTest
from test_run_lifecycle_algorithm_preview import FakeAlgoClient


class AllowDeletionConfirmation:
    async def consume(
        self,
        *,
        principal_id: str,
        request_id: str,
        confirmation_token: str,
    ) -> bool:
        return bool(principal_id and request_id and confirmation_token)


class FailDeletionCleanup:
    async def cleanup(self, conn, context) -> None:
        raise RuntimeError("injected cleanup failure")


class RejectDeletionConfirmation:
    async def consume(self, **kwargs) -> bool:
        return False


class CountingDeletionConfirmation:
    def __init__(self) -> None:
        self.calls = 0

    async def consume(self, **kwargs) -> bool:
        self.calls += 1
        return True


class PrincipalDataDeletionTests(RunLifecyclePostgresTest):
    async def test_deletion_schema_installs_on_a_fresh_database(self):
        await self.reset_to_fresh_schema()
        conn = await self._connect()
        try:
            rows = await (
                await conn.execute(
                    """
                    SELECT tablename
                    FROM pg_tables
                    WHERE schemaname = 'public'
                      AND tablename IN (
                          'principal_data_state',
                          'principal_deletion_request',
                          'principal_deletion_ledger',
                          'principal_deletion_tombstone'
                      )
                    ORDER BY tablename
                    """
                )
            ).fetchall()
        finally:
            await conn.close()
        self.assertEqual(
            [row["tablename"] for row in rows],
            [
                "principal_data_state",
                "principal_deletion_ledger",
                "principal_deletion_request",
                "principal_deletion_tombstone",
            ],
        )

    async def test_completed_deletion_hides_the_principals_run(self):
        from app.data_lifecycle import (
            DeletePrincipalDataCommand,
            PrincipalDataDeletion,
        )
        from app.run_lifecycle import (
            RunLifecycle,
            RunLifecycleError,
            StructuredProfileInput,
            SubmitRunCommand,
        )

        await self.seed_release()
        lifecycle = RunLifecycle(self.dsn, worker_owner="worker-delete")
        self.addAsyncCleanup(lifecycle.close)
        submitted = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-to-delete",
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
                idempotency_key=f"delete-submit-{uuid4()}",
            )
        )
        deletion = PrincipalDataDeletion(
            self.dsn,
            confirmation_authority=AllowDeletionConfirmation(),
        )

        receipt = await deletion.DeletePrincipalData(
            DeletePrincipalDataCommand(
                principal_id="principal-to-delete",
                request_id=f"delete-{uuid4()}",
                confirmation_token="confirmed",
            )
        )

        self.assertTrue(receipt.accepted)
        self.assertEqual(receipt.status, "DELETION_COMPLETED")
        with self.assertRaises(RunLifecycleError) as raised:
            await lifecycle.GetRun(
                principal_id="principal-to-delete",
                run_id=submitted.run_id,
            )
        self.assertEqual(raised.exception.reason, "PRINCIPAL_DELETED")

    async def test_retryable_cleanup_stays_locked_then_completes_on_retry(self):
        from app.data_lifecycle import (
            DeletePrincipalDataCommand,
            PrincipalDataDeletion,
        )
        from app.run_lifecycle import (
            RunLifecycle,
            RunLifecycleError,
            StructuredProfileInput,
            SubmitRunCommand,
        )

        await self.seed_release()
        lifecycle = RunLifecycle(self.dsn, worker_owner="worker-delete-retry")
        self.addAsyncCleanup(lifecycle.close)
        submitted = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-retry-delete",
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
                idempotency_key=f"delete-retry-submit-{uuid4()}",
            )
        )
        request_id = f"delete-retry-{uuid4()}"
        command = DeletePrincipalDataCommand(
            principal_id="principal-retry-delete",
            request_id=request_id,
            confirmation_token="confirmed",
        )
        failing = PrincipalDataDeletion(
            self.dsn,
            confirmation_authority=AllowDeletionConfirmation(),
            cleaner=FailDeletionCleanup(),
        )

        failed = await failing.DeletePrincipalData(command)

        self.assertEqual(failed.status, "DELETION_FAILED_RETRYABLE")
        with self.assertRaises(RunLifecycleError) as pending:
            await lifecycle.GetRun(
                principal_id="principal-retry-delete",
                run_id=submitted.run_id,
            )
        self.assertEqual(pending.exception.reason, "DELETION_IN_PROGRESS")

        retry = PrincipalDataDeletion(
            self.dsn,
            confirmation_authority=AllowDeletionConfirmation(),
        )
        completed = await retry.DeletePrincipalData(command)

        self.assertEqual(completed.status, "DELETION_COMPLETED")
        self.assertFalse(completed.replayed)
        with self.assertRaises(RunLifecycleError) as deleted:
            await lifecycle.GetRun(
                principal_id="principal-retry-delete",
                run_id=submitted.run_id,
            )
        self.assertEqual(deleted.exception.reason, "PRINCIPAL_DELETED")

    async def test_deleted_principal_cannot_submit_a_new_run(self):
        from app.data_lifecycle import (
            DeletePrincipalDataCommand,
            PrincipalDataDeletion,
        )
        from app.run_lifecycle import (
            ConfirmProfileAnswer,
            ResumeRunCommand,
            RunLifecycle,
            RunLifecycleError,
            StructuredProfileInput,
            SubmitRunCommand,
        )

        await self.seed_release()
        lifecycle = RunLifecycle(self.dsn, worker_owner="worker-delete-submit")
        self.addAsyncCleanup(lifecycle.close)
        input_value = StructuredProfileInput(
            province="湖南",
            application_year=2025,
            category="物理类",
            batch="本科批",
            score=600,
            subjects=("物理", "化学", "生物"),
        )
        submitted = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-no-return",
                session_id=None,
                task_kind="GENERATE_PLAN",
                input=input_value,
                idempotency_key=f"delete-no-return-before-{uuid4()}",
            )
        )
        deletion = PrincipalDataDeletion(
            self.dsn,
            confirmation_authority=AllowDeletionConfirmation(),
        )
        await deletion.DeletePrincipalData(
            DeletePrincipalDataCommand(
                principal_id="principal-no-return",
                request_id=f"delete-no-return-{uuid4()}",
                confirmation_token="confirmed",
            )
        )

        with self.assertRaises(RunLifecycleError) as raised:
            await lifecycle.SubmitRun(
                SubmitRunCommand(
                    principal_id="principal-no-return",
                    session_id=None,
                    task_kind="GENERATE_PLAN",
                    input=input_value,
                    idempotency_key=f"delete-no-return-after-{uuid4()}",
                )
            )
        self.assertEqual(raised.exception.reason, "PRINCIPAL_DELETED")

        with self.assertRaises(RunLifecycleError) as resume:
            await lifecycle.ResumeRun(
                ResumeRunCommand(
                    principal_id="principal-no-return",
                    run_id=submitted.run_id,
                    interrupt_id=uuid4(),
                    expected_state_version=1,
                    profile_version=None,
                    answer=ConfirmProfileAnswer(),
                    idempotency_key=f"delete-no-return-resume-{uuid4()}",
                )
            )
        self.assertEqual(resume.exception.reason, "PRINCIPAL_DELETED")

    async def test_deletion_removes_private_payloads_and_keeps_public_release(self):
        from app.data_lifecycle import (
            DeletePrincipalDataCommand,
            PrincipalDataDeletion,
        )
        from app.run_lifecycle import (
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )

        release_id = await self.seed_release()
        lifecycle = RunLifecycle(self.dsn, worker_owner="worker-delete-data")
        self.addAsyncCleanup(lifecycle.close)
        submitted = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-private-data",
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
                idempotency_key=f"delete-data-submit-{uuid4()}",
            )
        )
        conn = await self._connect()
        try:
            profile_version = uuid4()
            await conn.execute(
                "UPDATE chat_session SET profile = %s::jsonb, user_id = %s WHERE id = %s",
                (
                    '{"vision":"色弱，健康敏感原文"}',
                    "raw-user-id",
                    submitted.session_id,
                ),
            )
            await conn.execute(
                """
                INSERT INTO profile_revision (
                    profile_version, session_id, run_id, revision_no,
                    lifecycle, fields
                )
                VALUES (%s, %s, %s, 1, 'CONFIRMED', %s::jsonb)
                """,
                (
                    profile_version,
                    submitted.session_id,
                    submitted.run_id,
                    '{"vision":{"state":"KNOWN","value":"色弱"}}',
                ),
            )
            await conn.execute(
                """
                INSERT INTO profile_confirmation (
                    confirmation_id, profile_version, confirmed_by
                )
                VALUES (%s, %s, %s)
                """,
                (uuid4(), profile_version, "principal-private-data"),
            )
            await conn.execute(
                """
                INSERT INTO plan_checkpoint (session_id, node, state, seq)
                VALUES (%s, 'PROFILE_CONFIRM', %s::jsonb, 1)
                """,
                (
                    submitted.session_id,
                    '{"profile":{"vision":"色弱"}}',
                ),
            )
        finally:
            await conn.close()
        request_id = f"delete-private-data-{uuid4()}"
        deletion = PrincipalDataDeletion(
            self.dsn,
            confirmation_authority=AllowDeletionConfirmation(),
        )

        receipt = await deletion.DeletePrincipalData(
            DeletePrincipalDataCommand(
                principal_id="principal-private-data",
                request_id=request_id,
                confirmation_token="confirmed",
                reason="用户原文：请删除身份证号和色弱记录",
            )
        )

        self.assertEqual(receipt.status, "DELETION_COMPLETED")
        conn = await self._connect()
        try:
            session = await (
                await conn.execute(
                    "SELECT principal_id, user_id, profile FROM chat_session WHERE id = %s",
                    (submitted.session_id,),
                )
            ).fetchone()
            run = await (
                await conn.execute(
                    "SELECT principal_id, task_input FROM run WHERE run_id = %s",
                    (submitted.run_id,),
                )
            ).fetchone()
            counts = await (
                await conn.execute(
                    """
                    SELECT
                        (SELECT count(*) FROM profile_revision WHERE session_id = %s) AS profiles,
                        (SELECT count(*) FROM run_event WHERE run_id = %s) AS events,
                        (SELECT count(*) FROM plan_checkpoint WHERE session_id = %s) AS checkpoints,
                        (SELECT count(*) FROM release WHERE release_id = %s) AS releases
                    """,
                    (
                        submitted.session_id,
                        submitted.run_id,
                        submitted.session_id,
                        release_id,
                    ),
                )
            ).fetchone()
            ledger = await (
                await conn.execute(
                    """
                    SELECT resource_kind, result
                    FROM principal_deletion_ledger
                    WHERE request_id = %s
                    ORDER BY ledger_id
                    """,
                    (request_id,),
                )
            ).fetchall()
            tombstone = await (
                await conn.execute(
                    """
                    SELECT principal_ref, reason_code
                    FROM principal_deletion_tombstone
                    WHERE request_id = %s
                    """,
                    (request_id,),
                )
            ).fetchone()
        finally:
            await conn.close()

        self.assertIsNone(session["user_id"])
        self.assertIsNone(session["profile"])
        self.assertTrue(session["principal_id"].startswith("deleted:"))
        self.assertEqual(run["task_input"], {})
        self.assertTrue(run["principal_id"].startswith("deleted:"))
        self.assertEqual(counts["profiles"], 0)
        self.assertEqual(counts["events"], 0)
        self.assertEqual(counts["checkpoints"], 0)
        self.assertEqual(counts["releases"], 1)
        self.assertGreaterEqual(
            {row["resource_kind"] for row in ledger if row["result"] == "SUCCEEDED"},
            {"PROFILE", "RUN_DATA", "SESSION"},
        )
        self.assertNotEqual(
            tombstone["principal_ref"],
            "principal-private-data",
        )
        self.assertEqual(tombstone["reason_code"], "USER_REQUEST")

    async def test_invalid_confirmation_has_a_stable_error_and_preserves_access(self):
        from app.data_lifecycle import (
            DeletePrincipalDataCommand,
            PrincipalDataDeletion,
            PrincipalDataDeletionError,
        )
        from app.run_lifecycle import (
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )

        await self.seed_release()
        lifecycle = RunLifecycle(
            self.dsn,
            worker_owner="worker-delete-confirmation",
        )
        self.addAsyncCleanup(lifecycle.close)
        submitted = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-confirmation",
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
                idempotency_key=f"delete-confirmation-{uuid4()}",
            )
        )
        deletion = PrincipalDataDeletion(
            self.dsn,
            confirmation_authority=RejectDeletionConfirmation(),
        )

        with self.assertRaises(PrincipalDataDeletionError) as raised:
            await deletion.DeletePrincipalData(
                DeletePrincipalDataCommand(
                    principal_id="principal-confirmation",
                    request_id=f"delete-confirmation-request-{uuid4()}",
                    confirmation_token="expired",
                )
            )

        self.assertEqual(
            raised.exception.reason,
            "DELETION_CONFIRMATION_INVALID",
        )
        view = await lifecycle.GetRun(
            principal_id="principal-confirmation",
            run_id=submitted.run_id,
        )
        self.assertEqual(view.state, "QUEUED")

    async def test_deletion_removes_a_published_formal_result(self):
        from app.data_lifecycle import (
            DeletePrincipalDataCommand,
            PrincipalDataDeletion,
        )
        from app.run_lifecycle import (
            ConfirmProfileAnswer,
            ResumeRunCommand,
            RunLifecycle,
            RunLifecycleError,
            StructuredProfileInput,
            SubmitRunCommand,
        )
        from app.run_lifecycle.worker import RunLifecycleWorker

        await self.seed_release()
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="worker-delete-formal",
            algo_client=FakeAlgoClient(),
        )
        self.addAsyncCleanup(lifecycle.close)
        worker = RunLifecycleWorker(lifecycle, poll_interval=0.01)
        await worker.start()
        self.addAsyncCleanup(worker.stop)
        submitted = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-formal-delete",
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
                idempotency_key=f"delete-formal-submit-{uuid4()}",
            )
        )
        waiting = await self._wait_for_interrupt(
            lifecycle,
            "principal-formal-delete",
            submitted.run_id,
        )
        await lifecycle.ResumeRun(
            ResumeRunCommand(
                principal_id="principal-formal-delete",
                run_id=submitted.run_id,
                interrupt_id=waiting.current_interrupt.interrupt_id,
                expected_state_version=waiting.state_version,
                profile_version=UUID(
                    waiting.current_interrupt.payload["profile_version"]
                ),
                answer=ConfirmProfileAnswer(),
                idempotency_key=f"delete-formal-confirm-{uuid4()}",
            )
        )
        completed = await self._wait_for_terminal(
            lifecycle,
            "principal-formal-delete",
            submitted.run_id,
        )
        self.assertEqual(completed.state, "SUCCEEDED")
        self.assertEqual(completed.result.result_kind, "V1_FORMAL")
        deletion = PrincipalDataDeletion(
            self.dsn,
            confirmation_authority=AllowDeletionConfirmation(),
        )

        receipt = await deletion.DeletePrincipalData(
            DeletePrincipalDataCommand(
                principal_id="principal-formal-delete",
                request_id=f"delete-formal-{uuid4()}",
                confirmation_token="confirmed",
            )
        )

        self.assertEqual(receipt.status, "DELETION_COMPLETED")
        with self.assertRaises(RunLifecycleError) as deleted:
            await lifecycle.GetRun(
                principal_id="principal-formal-delete",
                run_id=submitted.run_id,
            )
        self.assertEqual(deleted.exception.reason, "PRINCIPAL_DELETED")
        conn = await self._connect()
        try:
            counts = await (
                await conn.execute(
                    """
                    SELECT
                        (SELECT count(*) FROM run_result WHERE run_id = %s) AS results,
                        (SELECT count(*) FROM run_evidence_package WHERE run_id = %s) AS packages,
                        (SELECT count(*) FROM run_verification_report WHERE run_id = %s) AS reports,
                        (SELECT count(*) FROM run_formal_gate_artifact WHERE run_id = %s) AS artifacts
                    """,
                    (
                        submitted.run_id,
                        submitted.run_id,
                        submitted.run_id,
                        submitted.run_id,
                    ),
                )
            ).fetchone()
        finally:
            await conn.close()
        self.assertEqual(counts["results"], 0)
        self.assertEqual(counts["packages"], 0)
        self.assertEqual(counts["reports"], 0)
        self.assertEqual(counts["artifacts"], 0)

    async def test_completed_request_replays_without_reconsuming_confirmation(self):
        from app.data_lifecycle import (
            DeletePrincipalDataCommand,
            PrincipalDataDeletion,
        )
        from app.run_lifecycle import (
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )

        await self.seed_release()
        lifecycle = RunLifecycle(self.dsn, worker_owner="worker-delete-replay")
        self.addAsyncCleanup(lifecycle.close)
        await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-delete-replay",
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
                idempotency_key=f"delete-replay-submit-{uuid4()}",
            )
        )
        confirmation = CountingDeletionConfirmation()
        deletion = PrincipalDataDeletion(
            self.dsn,
            confirmation_authority=confirmation,
        )
        command = DeletePrincipalDataCommand(
            principal_id="principal-delete-replay",
            request_id=f"delete-replay-{uuid4()}",
            confirmation_token="confirmed",
        )

        first = await deletion.DeletePrincipalData(command)
        replay = await deletion.DeletePrincipalData(command)

        self.assertEqual(first.status, "DELETION_COMPLETED")
        self.assertFalse(first.replayed)
        self.assertEqual(replay.status, "DELETION_COMPLETED")
        self.assertTrue(replay.replayed)
        self.assertEqual(confirmation.calls, 1)

    async def _wait_for_interrupt(self, lifecycle, principal_id, run_id):
        for _ in range(300):
            view = await lifecycle.GetRun(
                principal_id=principal_id,
                run_id=run_id,
            )
            if view.state == "WAITING_HITL" and view.current_interrupt is not None:
                return view
            await asyncio.sleep(0.01)
        self.fail("run did not expose a profile interrupt")

    async def _wait_for_terminal(self, lifecycle, principal_id, run_id):
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
