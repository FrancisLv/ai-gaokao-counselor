"""M5b 发布门禁必须存在可执行入口并实际阻断未签字数据。"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile

from run_lifecycle_support import RunLifecyclePostgresTest


ROOT = Path(__file__).resolve().parents[3]
DATASET = ROOT / "data" / "golden" / "evaluation-hunan-2025-v0.2.0-draft"
SCRIPT = ROOT / "scripts" / "run_m5b_release_gate.py"


class M5bReleaseEntrypointTests(RunLifecyclePostgresTest):
    async def test_executable_gate_blocks_pending_signoff_dataset(self):
        from eval.eval_dataset import load_eval_dataset
        from eval.eval_run import persist_eval_run

        dataset = load_eval_dataset(DATASET)
        security = [
            {
                "gate_id": gate_id,
                "passed": True,
                "actual": {"failed_cases": 0},
                "threshold": {"failed_cases": 0},
                "baseline": None,
                "failed_case_ids": [],
                "resource_types": [],
            }
            for gate_id in ("CONSENT", "DELETION", "RETENTION", "RESTORE")
        ]
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
        with tempfile.TemporaryDirectory() as directory:
            security_path = Path(directory) / "security.json"
            security_path.write_text(
                json.dumps(security),
                encoding="utf-8",
            )
            env = {
                **os.environ,
                "PYTHONPATH": (
                    f"{ROOT / 'services' / 'orchestrator'}:"
                    f"{ROOT / 'services' / 'orchestrator' / 'pb'}"
                ),
            }
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(SCRIPT),
                "--dsn",
                self.dsn,
                "--dataset-dir",
                str(DATASET),
                "--eval-run-id",
                record.eval_run_id,
                "--security-evidence",
                str(security_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await process.communicate()

        self.assertEqual(process.returncode, 2, stderr.decode())
        decision = json.loads(stdout)
        self.assertEqual(decision["gate_status"], "FAIL")
        self.assertIn("dataset_owner_status", decision["failed_metrics"])
