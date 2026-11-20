"""M5b-03：端到端 case 从 RunLifecycle 主 seam 获得最终结果。"""
from __future__ import annotations

from pathlib import Path

from run_lifecycle_support import RunLifecyclePostgresTest
from test_run_lifecycle_algorithm_preview import FakeAlgoClient


ROOT = Path(__file__).resolve().parents[3]
DATASET = ROOT / "data" / "golden" / "evaluation-hunan-2025-v0.1.0-draft"


class RunLifecycleCaseExecutorTests(RunLifecyclePostgresTest):
    async def test_confirmed_case_returns_the_final_formal_claims(self):
        from app.run_lifecycle import RunLifecycle
        from app.run_lifecycle.worker import RunLifecycleWorker
        from eval.e2e_eval import evaluate_end_to_end
        from eval.eval_dataset import (
            JointEvalDataset,
            load_eval_dataset,
        )
        from eval.run_lifecycle_runner import (
            RunLifecycleCaseExecutor,
            ScriptedReportNarrativeWriter,
        )

        await self.seed_release()
        dataset = load_eval_dataset(DATASET)
        case = dataset.e2e_cases[0]
        lifecycle = RunLifecycle(
            self.dsn,
            auto_profile=False,
            worker_owner="worker-e2e-eval",
            algo_client=FakeAlgoClient(),
            report_narrative_writer=ScriptedReportNarrativeWriter(
                case["scripted_model_responses"]
            ),
        )
        self.addAsyncCleanup(lifecycle.close)
        worker = RunLifecycleWorker(lifecycle, poll_interval=0.01)
        await worker.start()
        self.addAsyncCleanup(worker.stop)

        outcome = await RunLifecycleCaseExecutor(lifecycle).execute(case)
        single_case_dataset = JointEvalDataset(
            manifest=dataset.manifest,
            retrieval_cases=(),
            e2e_cases=(case,),
        )
        report = evaluate_end_to_end(
            single_case_dataset,
            {case["case_id"]: outcome},
        )

        self.assertEqual(outcome.terminal_status, "SUCCEEDED")
        self.assertEqual(outcome.result_kind, "V1_FORMAL")
        self.assertIn("LLM_ASSISTED", outcome.report_text)
        self.assertIn("U001|01", outcome.candidate_keys)
        self.assertGreater(len(outcome.claims), 0)
        self.assertEqual(
            report.gate_status,
            "PASS",
            report.cases[0].first_diff,
        )
        self.assertEqual(report.metrics.citation_coverage, 1.0)
        self.assertEqual(report.metrics.hallucination_rate, 0.0)


if __name__ == "__main__":
    import unittest

    unittest.main()
