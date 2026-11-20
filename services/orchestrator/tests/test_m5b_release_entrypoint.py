"""M5b 发布门禁必须存在可执行入口并实际阻断未签字数据。"""
from __future__ import annotations

from pathlib import Path

from run_lifecycle_support import RunLifecyclePostgresTest
from test_release_gate_attestation import FakeSecurityGateRunner


ROOT = Path(__file__).resolve().parents[3]
DATASET = ROOT / "data" / "golden" / "evaluation-hunan-2025-v0.2.0-draft"
SCRIPT = ROOT / "scripts" / "run_m5b_release_gate.py"


class M5bReleaseEntrypointTests(RunLifecyclePostgresTest):
    async def test_executable_gate_blocks_pending_signoff_dataset(self):
        from eval.eval_dataset import load_eval_dataset
        from eval.eval_run import persist_eval_run
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "run_m5b_release_gate",
            SCRIPT,
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)

        dataset = load_eval_dataset(DATASET)
        record, _ = await persist_eval_run(
            self.dsn,
            {
                "schema_version": "eval-run-v1",
                "generated_at": "2026-11-20T00:00:00+00:00",
                "dataset": {
                    "version": dataset.manifest["dataset_version"],
                    "sha256": dataset.manifest["dataset_sha256"],
                },
                "release_id": dataset.manifest["release_id"],
                "code_identity": {"git_sha": "b" * 40},
                "baseline_eval_run_id": "eval_baseline",
                "performance_context": {
                    "environment": "ci",
                    "hardware_profile": "linux-x86",
                    "database_data_size": "fixture-small",
                    "model_route_version": "route-v1",
                    "cache_state": "warm",
                    "concurrency": 1,
                },
                "metrics": {
                    "retrieval_recall_at_5": 1.0,
                    "retrieval_mrr": 1.0,
                    "retrieval_scope_leak_rate": 0.0,
                    "factual_claim_count": 10,
                    "claim_citation_coverage": 1.0,
                    "claim_citation_accuracy": 1.0,
                    "claim_contradiction_rate": 0.0,
                    "claim_critical_unverifiable_rate": 0.0,
                    "claim_hallucination_rate": 0.0,
                },
                "gate_status": "FAIL",
                "release_gate_status": "BLOCKED_PENDING_SIGNOFF",
                "failed_metrics": ["dataset_owner_status"],
                "failed_case_ids": [],
                "failure_details": [],
            },
        )
        decision, exit_code = await module.run_release_gate(
            dsn=self.dsn,
            security_test_dsn=self.dsn,
            dataset_dir=DATASET,
            eval_run_id=record.eval_run_id,
            git_sha=record.git_sha,
            security_runner=FakeSecurityGateRunner(),
        )

        self.assertEqual(exit_code, 2)
        self.assertEqual(decision["gate_status"], "FAIL")
        self.assertIn("dataset_owner_status", decision["failed_metrics"])

    async def test_cli_has_no_untrusted_eval_run_file_path(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "run_m5b_release_gate_no_file",
            SCRIPT,
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)

        with self.assertRaises(SystemExit) as raised:
            module.main(
                [
                    "--dsn",
                    self.dsn,
                    "--security-test-dsn",
                    self.dsn,
                    "--dataset-dir",
                    str(DATASET),
                    "--eval-run-file",
                    "/tmp/pass.json",
                ]
            )
        self.assertEqual(raised.exception.code, 2)

    async def test_cli_does_not_accept_caller_supplied_security_json(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "run_m5b_release_gate_no_security_file",
            SCRIPT,
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)

        with self.assertRaises(SystemExit) as raised:
            module.main(
                [
                    "--dsn",
                    self.dsn,
                    "--security-test-dsn",
                    self.dsn,
                    "--dataset-dir",
                    str(DATASET),
                    "--eval-run-id",
                    "eval_" + "a" * 64,
                    "--security-evidence",
                    "/tmp/security.json",
                ]
            )
        self.assertEqual(raised.exception.code, 2)
