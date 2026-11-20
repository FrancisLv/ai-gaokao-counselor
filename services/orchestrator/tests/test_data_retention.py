"""#47：终态数据按版本化策略清理，用户读取面不返回过期私有内容。"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from run_lifecycle_support import RunLifecyclePostgresTest


class DataRetentionTests(RunLifecyclePostgresTest):
    async def test_missing_versioned_policy_fails_closed(self):
        from app.data_lifecycle import (
            DataRetention,
            DataRetentionError,
            SweepRetentionCommand,
        )

        retention = DataRetention(self.dsn)
        with self.assertRaises(DataRetentionError) as raised:
            await retention.SweepExpiredData(
                SweepRetentionCommand(policy_version="missing-retention-policy")
            )

        self.assertEqual(raised.exception.reason, "RETENTION_POLICY_INVALID")
        conn = await self._connect()
        try:
            sweep_count = await (
                await conn.execute("SELECT count(*) AS count FROM data_retention_sweep")
            ).fetchone()
        finally:
            await conn.close()
        self.assertEqual(sweep_count["count"], 0)

    async def test_partial_resource_failure_is_retryable_and_audited(self):
        from app.data_lifecycle import DataRetention, SweepRetentionCommand
        from app.run_lifecycle import (
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )

        await self.seed_release()
        lifecycle = RunLifecycle(self.dsn, worker_owner="worker-retention-retry")
        self.addAsyncCleanup(lifecycle.close)
        submitted = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-retention-retry",
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
                idempotency_key=f"retention-retry-submit-{uuid4()}",
            )
        )
        conn = await self._connect()
        try:
            await conn.execute(
                """
                UPDATE run_event
                SET occurred_at = NOW() - INTERVAL '8 days'
                WHERE run_id = %s
                """,
                (submitted.run_id,),
            )
            await conn.execute(
                "ALTER TABLE run_event RENAME TO run_event_retention_unavailable"
            )
        finally:
            await conn.close()

        retention = DataRetention(self.dsn)
        failed = await retention.SweepExpiredData(
            SweepRetentionCommand(policy_version="m5b-retention-v1")
        )
        self.assertEqual(failed.status, "RETENTION_FAILED_RETRYABLE")
        self.assertIn("RUN_EVENTS", failed.failed_resources)

        conn = await self._connect()
        try:
            await conn.execute(
                "ALTER TABLE run_event_retention_unavailable RENAME TO run_event"
            )
            failed_ledger = await (
                await conn.execute(
                    """
                    SELECT result, error_code
                    FROM data_retention_ledger
                    WHERE sweep_id = %s AND resource_kind = 'RUN_EVENTS'
                    """,
                    (failed.sweep_id,),
                )
            ).fetchone()
        finally:
            await conn.close()
        self.assertEqual(failed_ledger, {
            "result": "FAILED",
            "error_code": "UndefinedTable",
        })

        retried = await retention.SweepExpiredData(
            SweepRetentionCommand(policy_version="m5b-retention-v1")
        )
        self.assertEqual(retried.status, "COMPLETED")
        conn = await self._connect()
        try:
            event_count = await (
                await conn.execute(
                    "SELECT count(*) AS count FROM run_event WHERE run_id = %s",
                    (submitted.run_id,),
                )
            ).fetchone()
        finally:
            await conn.close()
        self.assertEqual(event_count["count"], 0)

    async def test_repeated_sweep_is_idempotent(self):
        from app.data_lifecycle import DataRetention, SweepRetentionCommand

        retention = DataRetention(self.dsn)
        command = SweepRetentionCommand(policy_version="m5b-retention-v1")
        first = await retention.SweepExpiredData(command)
        second = await retention.SweepExpiredData(command)

        self.assertEqual(first.status, "COMPLETED")
        self.assertEqual(second.status, "COMPLETED")
        self.assertEqual(
            sum(second.affected_rows.values()),
            0,
        )

    async def test_security_audit_is_deidentified_after_180_days(self):
        from app.data_lifecycle import DataRetention, SweepRetentionCommand

        principal_id = "principal-audit-retention"
        principal_ref = "f" * 64
        request_id = "user-request-private-id"
        conn = await self._connect()
        try:
            await conn.execute(
                """
                INSERT INTO principal_data_state (
                    principal_id, status, deletion_request_id, updated_at
                )
                VALUES (
                    %s, 'DELETED', %s, NOW() - INTERVAL '181 days'
                )
                """,
                (principal_id, request_id),
            )
            await conn.execute(
                """
                INSERT INTO principal_deletion_request (
                    principal_ref, request_id, request_hash, status,
                    receipt, created_at, updated_at, completed_at
                )
                VALUES (
                    %s, %s, %s, 'DELETION_COMPLETED',
                    '{"accepted":true,"status":"DELETION_COMPLETED"}'::jsonb,
                    NOW() - INTERVAL '181 days',
                    NOW() - INTERVAL '181 days',
                    NOW() - INTERVAL '181 days'
                )
                """,
                (principal_ref, request_id, "a" * 64),
            )
            await conn.execute(
                """
                INSERT INTO principal_deletion_ledger (
                    principal_ref, request_id, resource_kind, result,
                    affected_rows, error_code, occurred_at
                )
                VALUES (
                    %s, %s, 'RESULT', 'FAILED', 0,
                    'private-error-code', NOW() - INTERVAL '181 days'
                )
                """,
                (principal_ref, request_id),
            )
            await conn.execute(
                """
                INSERT INTO principal_deletion_tombstone (
                    principal_ref, request_id, reason_code,
                    created_at, completed_at
                )
                VALUES (
                    %s, %s, 'USER_REQUEST',
                    NOW() - INTERVAL '181 days',
                    NOW() - INTERVAL '181 days'
                )
                """,
                (principal_ref, request_id),
            )
        finally:
            await conn.close()

        receipt = await DataRetention(self.dsn).SweepExpiredData(
            SweepRetentionCommand(policy_version="m5b-retention-v1")
        )

        self.assertEqual(receipt.status, "COMPLETED")
        self.assertGreater(receipt.affected_rows["SECURITY_AUDIT"], 0)
        conn = await self._connect()
        try:
            state = await (
                await conn.execute(
                    """
                    SELECT status, deletion_request_id
                    FROM principal_data_state WHERE principal_id = %s
                    """,
                    (principal_id,),
                )
            ).fetchone()
            ledger = await (
                await conn.execute(
                    """
                    SELECT request_id, error_code
                    FROM principal_deletion_ledger
                    WHERE principal_ref = %s
                    """,
                    (principal_ref,),
                )
            ).fetchone()
            tombstone = await (
                await conn.execute(
                    """
                    SELECT request_id, reason_code
                    FROM principal_deletion_tombstone
                    WHERE principal_ref = %s
                    """,
                    (principal_ref,),
                )
            ).fetchone()
            request = await (
                await conn.execute(
                    """
                    SELECT count(*) AS count
                    FROM principal_deletion_request
                    WHERE principal_ref = %s
                    """,
                    (principal_ref,),
                )
            ).fetchone()
        finally:
            await conn.close()
        self.assertEqual(state, {
            "status": "DELETED",
            "deletion_request_id": "retention-expired",
        })
        self.assertEqual(ledger, {
            "request_id": "retention-expired",
            "error_code": None,
        })
        self.assertEqual(tombstone, {
            "request_id": "retention-expired",
            "reason_code": "RETENTION_EXPIRED",
        })
        self.assertEqual(request["count"], 0)

    async def test_expired_successful_result_is_not_readable_from_get_run(self):
        from app.data_lifecycle import DataRetention, SweepRetentionCommand
        from app.run_lifecycle import (
            RunLifecycle,
            RunLifecycleError,
            StructuredProfileInput,
            SubmitRunCommand,
        )

        release_id = await self.seed_release()
        lifecycle = RunLifecycle(self.dsn, worker_owner="worker-retention")
        self.addAsyncCleanup(lifecycle.close)
        submitted = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-retention",
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
                idempotency_key=f"retention-submit-{uuid4()}",
            )
        )
        profile_version = uuid4()
        result_version = uuid4()
        conn = await self._connect()
        try:
            await conn.execute(
                """
                INSERT INTO profile_revision (
                    profile_version, session_id, run_id, revision_no,
                    lifecycle, fields, created_at, confirmed_at
                )
                VALUES (
                    %s, %s, %s, 1, 'CONFIRMED',
                    '{"score":{"state":"KNOWN","value":600}}'::jsonb,
                    NOW() - INTERVAL '31 days',
                    NOW() - INTERVAL '31 days'
                )
                """,
                (profile_version, submitted.session_id, submitted.run_id),
            )
            await conn.execute(
                """
                INSERT INTO profile_confirmation (
                    confirmation_id, profile_version, confirmed_by, confirmed_at
                )
                VALUES (%s, %s, 'principal-retention', NOW() - INTERVAL '31 days')
                """,
                (uuid4(), profile_version),
            )
            await conn.execute(
                """
                INSERT INTO run_result (
                    result_version, run_id, release_id,
                    confirmed_profile_version, result_kind, payload,
                    verification_summary, unverified_conditions,
                    source_package_sha256, evidence_package_sha256,
                    verification_report_sha256, created_at
                )
                VALUES (
                    %s, %s, %s, %s, 'V1_FORMAL',
                    '{"private":"expired report"}'::jsonb,
                    '[]'::jsonb, '[]'::jsonb,
                    %s, %s, %s,
                    NOW() - INTERVAL '31 days'
                )
                """,
                (
                    result_version,
                    submitted.run_id,
                    release_id,
                    profile_version,
                    "a" * 64,
                    "b" * 64,
                    "c" * 64,
                ),
            )
            await conn.execute(
                """
                UPDATE run
                SET state = 'SUCCEEDED',
                    stage = 'FORMAL_READY',
                    confirmed_profile_version = %s,
                    result_version = %s,
                    result_kind = 'V1_FORMAL',
                    updated_at = NOW() - INTERVAL '31 days'
                WHERE run_id = %s
                """,
                (profile_version, result_version, submitted.run_id),
            )
        finally:
            await conn.close()

        retention = DataRetention(
            self.dsn,
            clock=lambda: datetime(2026, 11, 20, tzinfo=timezone.utc),
        )
        receipt = await retention.SweepExpiredData(
            SweepRetentionCommand(policy_version="m5b-retention-v1")
        )

        self.assertEqual(receipt.status, "COMPLETED")
        with self.assertRaises(RunLifecycleError) as raised:
            await lifecycle.GetRun(
                principal_id="principal-retention",
                run_id=submitted.run_id,
            )
        self.assertEqual(raised.exception.reason, "RUN_NOT_FOUND")

        conn = await self._connect()
        try:
            private_counts = await (
                await conn.execute(
                    """
                    SELECT
                        (SELECT count(*) FROM run_result WHERE run_id = %s) AS results,
                        (SELECT count(*) FROM profile_revision WHERE run_id = %s) AS profiles,
                        (SELECT count(*) FROM run_interrupt WHERE run_id = %s) AS interrupts
                    """,
                    (submitted.run_id, submitted.run_id, submitted.run_id),
                )
            ).fetchone()
            run = await (
                await conn.execute(
                    """
                    SELECT principal_id, task_input, state, result_version,
                           confirmed_profile_version, working_profile_version
                    FROM run WHERE run_id = %s
                    """,
                    (submitted.run_id,),
                )
            ).fetchone()
            public_release = await (
                await conn.execute(
                    "SELECT status FROM release WHERE release_id = %s",
                    (release_id,),
                )
            ).fetchone()
        finally:
            await conn.close()
        self.assertEqual(private_counts, {
            "results": 0,
            "profiles": 0,
            "interrupts": 0,
        })
        self.assertEqual(run["principal_id"], "retention:deidentified")
        self.assertEqual(run["task_input"], {})
        self.assertEqual(run["state"], "SUCCEEDED")
        self.assertIsNone(run["result_version"])
        self.assertIsNone(run["confirmed_profile_version"])
        self.assertIsNone(run["working_profile_version"])
        self.assertEqual(public_release["status"], "PUBLISHED")

    async def test_expired_events_and_command_receipts_are_removed_at_seven_days(self):
        from app.data_lifecycle import DataRetention, SweepRetentionCommand
        from app.run_lifecycle import (
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )

        await self.seed_release()
        lifecycle = RunLifecycle(self.dsn, worker_owner="worker-retention-7d")
        self.addAsyncCleanup(lifecycle.close)
        submitted = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-retention-7d",
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
                idempotency_key=f"retention-7d-submit-{uuid4()}",
            )
        )
        conn = await self._connect()
        try:
            await conn.execute(
                """
                UPDATE run_event
                SET occurred_at = NOW() - INTERVAL '8 days'
                WHERE run_id = %s
                """,
                (submitted.run_id,),
            )
            await conn.execute(
                """
                UPDATE run_command_receipt
                SET created_at = NOW() - INTERVAL '9 days',
                    expires_at = NOW() - INTERVAL '8 days'
                WHERE target_run_id = %s
                """,
                (submitted.run_id,),
            )
        finally:
            await conn.close()

        receipt = await DataRetention(self.dsn).SweepExpiredData(
            SweepRetentionCommand(policy_version="m5b-retention-v1")
        )

        self.assertEqual(receipt.status, "COMPLETED")
        self.assertGreater(receipt.affected_rows["RUN_EVENTS"], 0)
        self.assertGreater(receipt.affected_rows["COMMAND_RECEIPTS"], 0)
        conn = await self._connect()
        try:
            counts = await (
                await conn.execute(
                    """
                    SELECT
                        (SELECT count(*) FROM run_event WHERE run_id = %s) AS events,
                        (SELECT count(*) FROM run_command_receipt
                         WHERE target_run_id = %s) AS receipts
                    """,
                    (submitted.run_id, submitted.run_id),
                )
            ).fetchone()
            ledger = await (
                await conn.execute(
                    """
                    SELECT resource_kind, result, affected_rows, error_code
                    FROM data_retention_ledger
                    WHERE sweep_id = %s
                    ORDER BY resource_kind
                    """,
                    (receipt.sweep_id,),
                )
            ).fetchall()
        finally:
            await conn.close()
        self.assertEqual(counts, {"events": 0, "receipts": 0})
        self.assertTrue(all(row["error_code"] is None for row in ledger))

    async def test_checkpoints_follow_success_and_failure_retention_windows(self):
        from app.data_lifecycle import DataRetention, SweepRetentionCommand
        from app.run_lifecycle import (
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )

        runs = {}
        await self.seed_release()
        lifecycle = RunLifecycle(self.dsn, worker_owner="worker-retention-cp")
        self.addAsyncCleanup(lifecycle.close)
        for suffix in ("recent-success", "expired-success", "recent-failed", "expired-failed"):
            runs[suffix] = await lifecycle.SubmitRun(
                SubmitRunCommand(
                    principal_id=f"principal-{suffix}",
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
                    idempotency_key=f"retention-cp-{suffix}-{uuid4()}",
                )
            )

        conn = await self._connect()
        try:
            await conn.execute(
                "CREATE TABLE checkpoints (thread_id TEXT NOT NULL)"
            )
            await conn.execute(
                "CREATE TABLE checkpoint_blobs (thread_id TEXT NOT NULL)"
            )
            await conn.execute(
                "CREATE TABLE checkpoint_writes (thread_id TEXT NOT NULL)"
            )
            for suffix, receipt in runs.items():
                await conn.execute(
                    "INSERT INTO checkpoints (thread_id) VALUES (%s)",
                    (str(receipt.run_id),),
                )
                await conn.execute(
                    "INSERT INTO checkpoint_blobs (thread_id) VALUES (%s)",
                    (str(receipt.run_id),),
                )
                await conn.execute(
                    "INSERT INTO checkpoint_writes (thread_id) VALUES (%s)",
                    (str(receipt.run_id),),
                )
            await conn.execute(
                """
                UPDATE run
                SET state = 'SUCCEEDED',
                    updated_at = CASE
                        WHEN run_id = %s THEN NOW()
                        ELSE NOW() - INTERVAL '31 days'
                    END
                WHERE run_id = ANY(%s::uuid[])
                """,
                (
                    runs["recent-success"].run_id,
                    [
                        runs["recent-success"].run_id,
                        runs["expired-success"].run_id,
                    ],
                ),
            )
            await conn.execute(
                """
                UPDATE run
                SET state = 'FAILED',
                    terminal_reason = 'VALIDATION_FAILED',
                    updated_at = CASE
                        WHEN run_id = %s THEN NOW()
                        ELSE NOW() - INTERVAL '8 days'
                    END
                WHERE run_id = ANY(%s::uuid[])
                """,
                (
                    runs["recent-failed"].run_id,
                    [
                        runs["recent-failed"].run_id,
                        runs["expired-failed"].run_id,
                    ],
                ),
            )
        finally:
            await conn.close()

        receipt = await DataRetention(self.dsn).SweepExpiredData(
            SweepRetentionCommand(policy_version="m5b-retention-v1")
        )

        self.assertEqual(receipt.status, "COMPLETED")
        conn = await self._connect()
        try:
            remaining = await (
                await conn.execute(
                    "SELECT thread_id FROM checkpoints ORDER BY thread_id"
                )
            ).fetchall()
        finally:
            await conn.close()
        self.assertEqual(
            {row["thread_id"] for row in remaining},
            {
                str(runs["recent-success"].run_id),
                str(runs["recent-failed"].run_id),
            },
        )

    async def test_provider_ledger_is_deidentified_after_180_days(self):
        from app.data_lifecycle import DataRetention, SweepRetentionCommand
        from app.run_lifecycle import (
            RunLifecycle,
            StructuredProfileInput,
            SubmitRunCommand,
        )

        release_id = await self.seed_release()
        lifecycle = RunLifecycle(self.dsn, worker_owner="worker-retention-llm")
        self.addAsyncCleanup(lifecycle.close)
        submitted = await lifecycle.SubmitRun(
            SubmitRunCommand(
                principal_id="principal-ledger-retention",
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
                idempotency_key=f"retention-ledger-{uuid4()}",
            )
        )
        logical_call_pk = uuid4()
        budget_scope_id = uuid4()
        attempt_id = uuid4()
        conn = await self._connect()
        try:
            await conn.execute(
                """
                UPDATE run
                SET state = 'SUCCEEDED',
                    updated_at = NOW() - INTERVAL '181 days'
                WHERE run_id = %s
                """,
                (submitted.run_id,),
            )
            await conn.execute(
                """
                INSERT INTO llm_price_table (
                    price_table_version, currency_unit, effective_at, published_at
                )
                VALUES (
                    'price-v1', 'USD_MICRO',
                    NOW() - INTERVAL '2 years', NOW() - INTERVAL '2 years'
                )
                """
            )
            await conn.execute(
                """
                INSERT INTO llm_budget_scope (
                    budget_scope_id, principal_id, session_id, created_at, updated_at
                )
                VALUES (
                    %s, 'principal-ledger-retention', %s,
                    NOW() - INTERVAL '181 days', NOW() - INTERVAL '181 days'
                )
                """,
                (budget_scope_id, submitted.session_id),
            )
            await conn.execute(
                """
                INSERT INTO llm_logical_call (
                    logical_call_pk, logical_call_id, principal_id, session_id,
                    run_id, role, release_id, route_version, price_table_version,
                    model_id, request_hash, budget_scope_id, status,
                    reserved_input_tokens, reserved_output_tokens,
                    reserved_cost_micro_usd, actual_input_tokens,
                    actual_output_tokens, actual_cost_micro_usd,
                    response_payload, error_reason, created_at, updated_at,
                    completed_at
                )
                VALUES (
                    %s, 'logical-retention', 'principal-ledger-retention', %s,
                    %s, 'report', %s, 'route-v1', 'price-v1',
                    'model-v1', %s, %s, 'SUCCEEDED',
                    10, 10, 100, 10, 10, 100,
                    '{"private":"model response"}'::jsonb, 'private error',
                    NOW() - INTERVAL '181 days',
                    NOW() - INTERVAL '181 days',
                    NOW() - INTERVAL '181 days'
                )
                """,
                (
                    logical_call_pk,
                    submitted.session_id,
                    submitted.run_id,
                    release_id,
                    "d" * 64,
                    budget_scope_id,
                ),
            )
            await conn.execute(
                """
                INSERT INTO llm_provider_attempt (
                    attempt_id, logical_call_pk, logical_call_id, model_id,
                    provider, status, reserved_tokens, reserved_cost_micro_usd,
                    input_tokens, output_tokens, cost_micro_usd,
                    provider_request_id, error_code, created_at
                )
                VALUES (
                    %s, %s, 'logical-retention', 'model-v1',
                    'provider-v1', 'SUCCEEDED', 10, 100,
                    10, 10, 100, 'provider-request-private', 'private-error',
                    NOW() - INTERVAL '181 days'
                )
                """,
                (attempt_id, logical_call_pk),
            )
            await conn.execute(
                """
                INSERT INTO llm_call_log (
                    trace_id, session_id, node, error_message, created_at
                )
                VALUES (
                    'trace-private', %s, 'report', 'private-error',
                    NOW() - INTERVAL '181 days'
                )
                """,
                (submitted.session_id,),
            )
        finally:
            await conn.close()

        receipt = await DataRetention(self.dsn).SweepExpiredData(
            SweepRetentionCommand(policy_version="m5b-retention-v1")
        )

        self.assertEqual(receipt.status, "COMPLETED")
        conn = await self._connect()
        try:
            logical = await (
                await conn.execute(
                    """
                    SELECT principal_id, response_payload, error_reason
                    FROM llm_logical_call WHERE logical_call_pk = %s
                    """,
                    (logical_call_pk,),
                )
            ).fetchone()
            budget = await (
                await conn.execute(
                    """
                    SELECT principal_id FROM llm_budget_scope
                    WHERE budget_scope_id = %s
                    """,
                    (budget_scope_id,),
                )
            ).fetchone()
            attempt = await (
                await conn.execute(
                    """
                    SELECT provider_request_id, error_code, input_tokens,
                           output_tokens, cost_micro_usd
                    FROM llm_provider_attempt WHERE attempt_id = %s
                    """,
                    (attempt_id,),
                )
            ).fetchone()
            call_log = await (
                await conn.execute(
                    """
                    SELECT trace_id, session_id, node, error_message
                    FROM llm_call_log
                    WHERE created_at < NOW() - INTERVAL '180 days'
                    """
                )
            ).fetchone()
        finally:
            await conn.close()
        self.assertEqual(logical, {
            "principal_id": "retention:deidentified",
            "response_payload": None,
            "error_reason": None,
        })
        self.assertEqual(budget["principal_id"], "retention:deidentified")
        self.assertEqual(attempt, {
            "provider_request_id": None,
            "error_code": None,
            "input_tokens": 10,
            "output_tokens": 10,
            "cost_micro_usd": 100,
        })
        self.assertEqual(call_log, {
            "trace_id": None,
            "session_id": None,
            "node": None,
            "error_message": None,
        })
