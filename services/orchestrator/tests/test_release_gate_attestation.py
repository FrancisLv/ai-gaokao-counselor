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
        )


class ReleaseGateAttestationTests(RunLifecyclePostgresTest):
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
        eval_run, _ = await persist_eval_run(self.dsn, _payload())
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
