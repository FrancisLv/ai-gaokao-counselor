"""#48：备份恢复先重放 principal 删除，再开放业务读取。"""
from __future__ import annotations

import asyncio
import hashlib
from uuid import UUID, uuid4

from run_lifecycle_support import RunLifecyclePostgresTest
from test_run_lifecycle_algorithm_preview import FakeAlgoClient


class InMemoryProjection:
    def __init__(self) -> None:
        self.entries = ("已撤回的敏感结果",)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def reset_after_restore(self) -> None:
        self.started.set()
        await self.release.wait()
        self.entries = ()


class NoOpProjection:
    async def reset_after_restore(self) -> None:
        return None


class FailingCleaner:
    async def cleanup(self, conn, context) -> None:
        raise RuntimeError("injected replay failure")


class PrincipalDataRestoreTests(RunLifecyclePostgresTest):
    async def test_restore_keeps_reads_closed_until_deleted_data_is_replayed(self):
        from app.data_lifecycle import (
            PrincipalDataRestore,
            RestoreBackupCommand,
        )
        from app.run_lifecycle import (
            RunLifecycle,
            RunLifecycleError,
            StructuredProfileInput,
            SubmitRunCommand,
        )

        principal_id = "principal-restore"
        release_id = await self.seed_release()
        lifecycle = RunLifecycle(self.dsn, worker_owner="worker-restore")
        self.addAsyncCleanup(lifecycle.close)
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
                idempotency_key=f"restore-submit-{uuid4()}",
            )
        )
        await self._seed_restored_private_data(
            principal_id=principal_id,
            session_id=submitted.session_id,
            run_id=submitted.run_id,
        )
        request_id = f"deletion-{uuid4()}"
        await self._seed_completed_deletion_metadata(
            principal_id=principal_id,
            request_id=request_id,
        )
        projection = InMemoryProjection()
        restore = PrincipalDataRestore(
            self.dsn,
            projection=projection,
        )

        restoring = asyncio.create_task(
            restore.RestoreFromBackup(
                RestoreBackupCommand(restore_id=f"restore-{uuid4()}")
            )
        )
        await asyncio.wait_for(projection.started.wait(), timeout=1)
        with self.assertRaises(RunLifecycleError) as closed:
            await lifecycle.GetRun(
                principal_id=principal_id,
                run_id=submitted.run_id,
            )
        self.assertEqual(closed.exception.reason, "RESTORE_IN_PROGRESS")

        projection.release.set()
        receipt = await restoring

        self.assertEqual(receipt.status, "OPEN")
        self.assertEqual(receipt.replayed_principal_count, 1)
        self.assertEqual(projection.entries, ())
        with self.assertRaises(RunLifecycleError) as deleted:
            await lifecycle.GetRun(
                principal_id=principal_id,
                run_id=submitted.run_id,
            )
        self.assertEqual(deleted.exception.reason, "PRINCIPAL_DELETED")
        conn = await self._connect()
        try:
            rows = await (
                await conn.execute(
                    """
                    SELECT
                        (SELECT count(*) FROM profile_revision
                         WHERE session_id = %s) AS profiles,
                        (SELECT count(*) FROM release
                         WHERE release_id = %s) AS releases
                    """,
                    (submitted.session_id, release_id),
                )
            ).fetchone()
        finally:
            await conn.close()
        self.assertEqual(rows["profiles"], 0)
        self.assertEqual(rows["releases"], 1)

    async def test_repeated_restore_replays_without_reprocessing_deletion(self):
        from app.data_lifecycle import (
            PrincipalDataRestore,
            RestoreBackupCommand,
        )
        from app.run_lifecycle import (
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )

        principal_id = "principal-restore-replay"
        await self.seed_release()
        lifecycle = RunLifecycle(self.dsn, worker_owner="worker-restore-replay")
        self.addAsyncCleanup(lifecycle.close)
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
                idempotency_key=f"restore-replay-submit-{uuid4()}",
            )
        )
        await self._seed_restored_private_data(
            principal_id=principal_id,
            session_id=submitted.session_id,
            run_id=submitted.run_id,
        )
        request_id = f"deletion-{uuid4()}"
        await self._seed_completed_deletion_metadata(
            principal_id=principal_id,
            request_id=request_id,
        )
        restore_id = f"restore-{uuid4()}"
        restore = PrincipalDataRestore(
            self.dsn,
            projection=NoOpProjection(),
        )

        first = await restore.RestoreFromBackup(
            RestoreBackupCommand(restore_id=restore_id)
        )
        second = await restore.RestoreFromBackup(
            RestoreBackupCommand(restore_id=restore_id)
        )

        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(second.status, "OPEN")
        conn = await self._connect()
        try:
            counts = await (
                await conn.execute(
                    """
                    SELECT
                        (SELECT count(*) FROM data_lifecycle_restore_replay
                         WHERE restore_id = %s) AS replays,
                        (SELECT count(*) FROM principal_deletion_ledger
                         WHERE request_id = %s) AS ledgers
                    """,
                    (restore_id, request_id),
                )
            ).fetchone()
        finally:
            await conn.close()
        self.assertEqual(counts["replays"], 1)
        self.assertEqual(counts["ledgers"], 6)

    async def test_replay_failure_keeps_reads_closed_until_retry_succeeds(self):
        from app.data_lifecycle import (
            PrincipalDataRestore,
            RestoreBackupCommand,
        )
        from app.run_lifecycle import (
            RunLifecycle,
            RunLifecycleError,
            StructuredProfileInput,
            SubmitRunCommand,
        )

        principal_id = "principal-restore-failure"
        await self.seed_release()
        lifecycle = RunLifecycle(self.dsn, worker_owner="worker-restore-failure")
        self.addAsyncCleanup(lifecycle.close)
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
                idempotency_key=f"restore-failure-submit-{uuid4()}",
            )
        )
        await self._seed_restored_private_data(
            principal_id=principal_id,
            session_id=submitted.session_id,
            run_id=submitted.run_id,
        )
        await self._seed_completed_deletion_metadata(
            principal_id=principal_id,
            request_id=f"deletion-{uuid4()}",
        )
        restore_id = f"restore-{uuid4()}"
        failed = await PrincipalDataRestore(
            self.dsn,
            projection=NoOpProjection(),
            cleaner=FailingCleaner(),
        ).RestoreFromBackup(
            RestoreBackupCommand(restore_id=restore_id)
        )

        self.assertFalse(failed.accepted)
        self.assertEqual(failed.status, "RESTORE_FAILED_RETRYABLE")
        with self.assertRaises(RunLifecycleError) as closed:
            await lifecycle.GetRun(
                principal_id=principal_id,
                run_id=submitted.run_id,
            )
        self.assertEqual(closed.exception.reason, "RESTORE_IN_PROGRESS")

        retried = await PrincipalDataRestore(
            self.dsn,
            projection=NoOpProjection(),
        ).RestoreFromBackup(
            RestoreBackupCommand(restore_id=restore_id)
        )

        self.assertTrue(retried.accepted)
        self.assertEqual(retried.status, "OPEN")
        with self.assertRaises(RunLifecycleError) as deleted:
            await lifecycle.GetRun(
                principal_id=principal_id,
                run_id=submitted.run_id,
            )
        self.assertEqual(deleted.exception.reason, "PRINCIPAL_DELETED")

    async def test_restore_replay_removes_restored_formal_result_and_evidence(self):
        from app.data_lifecycle import (
            PrincipalDataRestore,
            RestoreBackupCommand,
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

        principal_id = "principal-restore-formal"
        await self.seed_release()
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=True,
            worker_owner="worker-restore-formal",
            algo_client=FakeAlgoClient(),
        )
        self.addAsyncCleanup(lifecycle.close)
        worker = RunLifecycleWorker(lifecycle, poll_interval=0.01)
        await worker.start()
        self.addAsyncCleanup(worker.stop)
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
                idempotency_key=f"restore-formal-submit-{uuid4()}",
            )
        )
        waiting = await self._wait_for_interrupt(
            lifecycle,
            principal_id,
            submitted.run_id,
        )
        await lifecycle.ResumeRun(
            ResumeRunCommand(
                principal_id=principal_id,
                run_id=submitted.run_id,
                interrupt_id=waiting.current_interrupt.interrupt_id,
                expected_state_version=waiting.state_version,
                profile_version=UUID(
                    waiting.current_interrupt.payload["profile_version"]
                ),
                answer=ConfirmProfileAnswer(),
                idempotency_key=f"restore-formal-confirm-{uuid4()}",
            )
        )
        completed = await self._wait_for_terminal(
            lifecycle,
            principal_id,
            submitted.run_id,
        )
        self.assertEqual(completed.state, "SUCCEEDED")
        self.assertEqual(completed.result.result_kind, "V1_FORMAL")
        await self._seed_completed_deletion_metadata(
            principal_id=principal_id,
            request_id=f"deletion-{uuid4()}",
        )

        restored = await PrincipalDataRestore(
            self.dsn,
            projection=NoOpProjection(),
        ).RestoreFromBackup(
            RestoreBackupCommand(restore_id=f"restore-{uuid4()}")
        )

        self.assertTrue(restored.accepted)
        with self.assertRaises(RunLifecycleError) as deleted:
            await lifecycle.GetRun(
                principal_id=principal_id,
                run_id=submitted.run_id,
            )
        self.assertEqual(deleted.exception.reason, "PRINCIPAL_DELETED")
        conn = await self._connect()
        try:
            counts = await (
                await conn.execute(
                    """
                    SELECT
                        (SELECT count(*) FROM run_result
                         WHERE run_id = %s) AS results,
                        (SELECT count(*) FROM run_evidence_package
                         WHERE run_id = %s) AS packages,
                        (SELECT count(*) FROM run_verification_report
                         WHERE run_id = %s) AS reports
                    """,
                    (
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

    async def test_tombstone_without_restored_private_data_replays_idempotently(self):
        from app.data_lifecycle import (
            PrincipalDataRestore,
            RestoreBackupCommand,
        )

        principal_id = "principal-was-already-clean"
        request_id = f"deletion-{uuid4()}"
        await self._seed_completed_deletion_metadata(
            principal_id=principal_id,
            request_id=request_id,
        )
        restore_id = f"restore-{uuid4()}"
        restore = PrincipalDataRestore(
            self.dsn,
            projection=NoOpProjection(),
        )

        first = await restore.RestoreFromBackup(
            RestoreBackupCommand(restore_id=restore_id)
        )
        second = await restore.RestoreFromBackup(
            RestoreBackupCommand(restore_id=restore_id)
        )

        self.assertTrue(first.accepted)
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(second.replayed_principal_count, 1)

    async def _seed_restored_private_data(
        self,
        *,
        principal_id: str,
        session_id,
        run_id,
    ) -> None:
        conn = await self._connect()
        try:
            profile_version = uuid4()
            await conn.execute(
                "UPDATE chat_session SET profile = %s::jsonb WHERE id = %s",
                (
                    '{"vision":"色弱，健康敏感原文"}',
                    session_id,
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
                    session_id,
                    run_id,
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
                (uuid4(), profile_version, principal_id),
            )
        finally:
            await conn.close()

    async def _seed_completed_deletion_metadata(
        self,
        *,
        principal_id: str,
        request_id: str,
    ) -> None:
        principal_ref = hashlib.sha256(principal_id.encode()).hexdigest()
        conn = await self._connect()
        try:
            await conn.execute(
                """
                INSERT INTO principal_deletion_request (
                    principal_ref, request_id, request_hash, status,
                    receipt, completed_at
                )
                VALUES (%s, %s, %s, 'DELETION_COMPLETED', %s::jsonb, NOW())
                """,
                (
                    principal_ref,
                    request_id,
                    "a" * 64,
                    '{"accepted":true,"status":"DELETION_COMPLETED",'
                    '"reason":null,"request_id":"'
                    + request_id
                    + '","replayed":false}',
                ),
            )
            await conn.execute(
                """
                INSERT INTO principal_deletion_tombstone (
                    principal_ref, request_id, reason_code, completed_at
                )
                VALUES (%s, %s, 'USER_REQUEST', NOW())
                """,
                (principal_ref, request_id),
            )
            for resource_kind in (
                "CHECKPOINTS",
                "RESULT",
                "PROFILE",
                "RUN_DATA",
                "SESSION",
                "LLM_LEDGER",
            ):
                await conn.execute(
                    """
                    INSERT INTO principal_deletion_ledger (
                        principal_ref, request_id, resource_kind, result
                    )
                    VALUES (%s, %s, %s, 'SUCCEEDED')
                    """,
                    (principal_ref, request_id, resource_kind),
                )
        finally:
            await conn.close()

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
