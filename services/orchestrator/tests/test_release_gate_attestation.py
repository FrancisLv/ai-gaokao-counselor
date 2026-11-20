"""发布安全证据必须由受控执行记录生成并在 PostgreSQL 中不可变保存。"""
from __future__ import annotations

import psycopg

from run_lifecycle_support import ROOT, RunLifecyclePostgresTest
from test_eval_run_store import _payload
from test_m5b_release_gate import _dataset


class FakeSecurityGateRunner:
    async def run(self, gate_id):
        from eval.security_evidence import (
            SECURITY_GATE_SUITES,
            SecurityGateSuiteResult,
        )

        return SecurityGateSuiteResult(
            gate_id=gate_id,
            passed=True,
            suite=SECURITY_GATE_SUITES[gate_id],
            exit_code=0,
            executed_count=1,
            skipped_count=0,
            error_count=0,
            failure_count=0,
        )


class ReleaseGateAttestationTests(RunLifecyclePostgresTest):
    def test_security_runner_rejects_a_suite_with_zero_executed_tests(self):
        from eval.security_evidence import parse_unittest_result

        result = parse_unittest_result(
            gate_id="DELETION",
            suite="test_data_deletion",
            output="Ran 0 tests in 0.001s\n\nOK\n",
            exit_code=0,
        )

        self.assertFalse(result.passed)
        self.assertEqual(result.executed_count, 0)

    def test_security_runner_rejects_an_all_skipped_suite(self):
        from eval.security_evidence import parse_unittest_result

        result = parse_unittest_result(
            gate_id="RESTORE",
            suite="test_data_restore",
            output="Ran 3 tests in 0.010s\n\nOK (skipped=3)\n",
            exit_code=0,
        )

        self.assertFalse(result.passed)
        self.assertEqual(result.executed_count, 0)
        self.assertEqual(result.skipped_count, 3)

    async def test_public_gate_requires_a_persisted_baseline_eval_run(self):
        from eval.eval_run import persist_eval_run
        from eval.release_decision import (
            ReleaseGateAuthorityError,
            evaluate_m5b_release_gate,
        )
        from eval.security_evidence import collect_security_gate_evidence

        dataset = _dataset()
        eval_run, _ = await persist_eval_run(self.dsn, _payload())
        records = await collect_security_gate_evidence(
            self.dsn,
            eval_run=eval_run,
            dataset=dataset,
            git_sha=eval_run.git_sha,
            runner=FakeSecurityGateRunner(),
        )

        with self.assertRaises(ReleaseGateAuthorityError):
            await evaluate_m5b_release_gate(
                dsn=self.dsn,
                dataset=dataset,
                eval_run_id=eval_run.eval_run_id,
                security_artifact_ids=tuple(
                    record.artifact_id for record in records
                ),
                expected_git_sha=eval_run.git_sha,
            )

    async def test_public_gate_recomputes_p99_regression_from_baseline(self):
        from eval.eval_run import persist_eval_run
        from eval.release_decision import evaluate_m5b_release_gate
        from eval.security_evidence import collect_security_gate_evidence

        dataset = _dataset()
        baseline, _ = await persist_eval_run(self.dsn, _payload())
        current_payload = _payload()
        current_payload["baseline_eval_run_id"] = baseline.eval_run_id
        current_payload["metrics"]["run_active_execution_p99_ms"] = 1200
        current_payload["metrics"]["run_active_execution_max_ms"] = 1200
        for sample in current_payload["run_samples"]:
            sample["active_execution_ms"] = 1200
        current, _ = await persist_eval_run(self.dsn, current_payload)
        records = await collect_security_gate_evidence(
            self.dsn,
            eval_run=current,
            dataset=dataset,
            git_sha=current.git_sha,
            runner=FakeSecurityGateRunner(),
        )
        decision = await evaluate_m5b_release_gate(
            dsn=self.dsn,
            dataset=dataset,
            eval_run_id=current.eval_run_id,
            security_artifact_ids=tuple(
                record.artifact_id for record in records
            ),
            expected_git_sha=current.git_sha,
        )

        self.assertEqual(decision.gate_status, "FAIL")
        self.assertIn("run_p99_regression", decision.failed_metrics)

    async def test_security_suites_are_persisted_and_loaded_by_identity(self):
        from eval.eval_run import persist_eval_run
        from eval.security_evidence import (
            collect_security_gate_evidence,
            load_security_gate_evidence,
        )

        eval_run, _ = await persist_eval_run(self.dsn, _payload())
        records = await collect_security_gate_evidence(
            self.dsn,
            eval_run=eval_run,
            dataset=_dataset(),
            git_sha=eval_run.git_sha,
            runner=FakeSecurityGateRunner(),
        )
        loaded = await load_security_gate_evidence(
            self.dsn,
            tuple(record.artifact_id for record in records),
        )

        self.assertEqual(
            tuple(record.gate_id for record in loaded),
            ("CONSENT", "DELETION", "RETENTION", "RESTORE"),
        )
        self.assertTrue(all(record.passed for record in loaded))
        self.assertTrue(
            all(record.eval_run_id == eval_run.eval_run_id for record in loaded)
        )
        self.assertTrue(
            all(record.git_sha == eval_run.git_sha for record in loaded)
        )

        conn = await self._connect()
        try:
            with self.assertRaises(psycopg.Error):
                await conn.execute(
                    """
                    UPDATE release_gate_security_record
                    SET passed = FALSE
                    WHERE artifact_id = %s
                    """,
                    (records[0].artifact_id,),
                )
        finally:
            await conn.close()

    async def test_security_artifact_cannot_bind_to_another_git_sha(self):
        from eval.eval_run import persist_eval_run
        from eval.security_evidence import (
            SecurityGateEvidenceIntegrityError,
            collect_security_gate_evidence,
        )

        eval_run, _ = await persist_eval_run(self.dsn, _payload())
        with self.assertRaises(SecurityGateEvidenceIntegrityError):
            await collect_security_gate_evidence(
                self.dsn,
                eval_run=eval_run,
                dataset=_dataset(),
                git_sha="c" * 40,
                runner=FakeSecurityGateRunner(),
            )

    async def test_public_gate_loads_only_persisted_authority(self):
        from eval.eval_run import persist_eval_run
        from eval.release_decision import (
            ReleaseGateAuthorityError,
            evaluate_m5b_release_gate,
        )
        from eval.security_evidence import collect_security_gate_evidence

        dataset = _dataset()
        baseline, _ = await persist_eval_run(self.dsn, _payload())
        payload = _payload()
        payload["baseline_eval_run_id"] = baseline.eval_run_id
        eval_run, _ = await persist_eval_run(self.dsn, payload)
        records = await collect_security_gate_evidence(
            self.dsn,
            eval_run=eval_run,
            dataset=dataset,
            git_sha=eval_run.git_sha,
            runner=FakeSecurityGateRunner(),
        )
        decision = await evaluate_m5b_release_gate(
            dsn=self.dsn,
            dataset=dataset,
            eval_run_id=eval_run.eval_run_id,
            security_artifact_ids=tuple(
                record.artifact_id for record in records
            ),
            expected_git_sha=eval_run.git_sha,
        )
        self.assertEqual(decision.gate_status, "PASS")

        with self.assertRaises(ReleaseGateAuthorityError):
            await evaluate_m5b_release_gate(
                dsn=self.dsn,
                dataset=dataset,
                eval_run_id=eval_run.eval_run_id,
                security_artifact_ids=tuple(
                    record.artifact_id for record in records
                ),
                expected_git_sha="c" * 40,
            )

    async def test_server_runner_executes_the_allowlisted_security_suite(self):
        from eval.security_evidence import SubprocessSecurityGateRunner

        result = await SubprocessSecurityGateRunner(
            dsn=self.dsn,
            root=ROOT,
        ).run("CONSENT")

        self.assertTrue(result.passed)
        self.assertEqual(result.suite, "test_run_lifecycle_consent")
        self.assertEqual(result.exit_code, 0)
        self.assertGreater(result.executed_count, 0)
        self.assertEqual(result.skipped_count, 0)
        self.assertEqual(result.error_count, 0)
        self.assertEqual(result.failure_count, 0)
