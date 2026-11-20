"""EvalRun 持久化必须是不可变且可重放的权威记录。"""
from __future__ import annotations

import psycopg

from run_lifecycle_support import RunLifecyclePostgresTest


def _payload(*, gate_status="PASS", metric=0.9):
    return {
        "schema_version": "eval-run-v1",
        "generated_at": "2026-11-20T00:00:00+00:00",
        "dataset": {
            "version": "eval-v1",
            "sha256": "a" * 64,
            "owner_status": "SIGNED_OFF",
        },
        "release_id": "rel-v1",
        "code_identity": {"git_sha": "b" * 40},
        "baseline_eval_run_id": "eval_baseline",
        "metrics": {
            "retrieval_recall_at_5": metric,
            "retrieval_mrr": 0.8,
            "retrieval_scope_leak_rate": 0.0,
            "factual_claim_count": 10,
            "claim_citation_coverage": 1.0,
            "claim_citation_accuracy": 1.0,
            "claim_contradiction_rate": 0.0,
            "claim_critical_unverifiable_rate": 0.0,
            "claim_hallucination_rate": 0.0,
            "illegal_candidate_leak_rate": 0.0,
            "run_sample_count": 100,
            "run_active_execution_p99_ms": 1000,
            "run_active_execution_max_ms": 1000,
            "session_token_count_max": 100,
            "session_cost_micro_usd_max": 100,
            "budget_violation_count": 0,
            "budget_reconciliation_pending_count": 0,
            "run_reproducibility_rate": 1.0,
        },
        "gate_status": gate_status,
        "release_gate_status": gate_status,
        "performance_context": {
            "environment": "ci",
            "hardware_profile": "linux-x86",
            "database_data_size": "fixture-small",
            "model_route_version": "route-v1",
            "cache_state": "warm",
            "concurrency": 1,
        },
        "failed_metrics": [],
        "failure_details": [],
    }


class EvalRunStoreTests(RunLifecyclePostgresTest):
    async def test_eval_run_round_trips_without_mutation(self):
        from eval.eval_run import load_eval_run, persist_eval_run

        created, replayed = await persist_eval_run(self.dsn, _payload())
        self.assertFalse(replayed)
        loaded = await load_eval_run(self.dsn, created.eval_run_id)
        self.assertEqual(loaded.payload, created.payload)

        replayed_record, replayed = await persist_eval_run(
            self.dsn,
            created.payload,
        )
        self.assertTrue(replayed)
        self.assertEqual(replayed_record.eval_run_id, created.eval_run_id)

        conn = await self._connect()
        try:
            with self.assertRaises(psycopg.Error):
                await conn.execute(
                    """
                    UPDATE eval_run_record
                    SET gate_status = 'FAIL'
                    WHERE eval_run_id = %s
                    """,
                    (created.eval_run_id,),
                )
        finally:
            await conn.close()

    async def test_eval_run_identity_cannot_be_rebound_to_different_content(self):
        from eval.eval_run import persist_eval_run

        created, _ = await persist_eval_run(self.dsn, _payload())
        changed = {
            **created.payload,
            "metrics": {"retrieval_recall_at_5": 0.1},
        }
        with self.assertRaises(ValueError):
            await persist_eval_run(
                self.dsn,
                {**changed, "result_sha256": created.result_sha256},
            )
