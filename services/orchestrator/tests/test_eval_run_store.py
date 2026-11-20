"""EvalRun 持久化必须是不可变且可重放的权威记录。"""
from __future__ import annotations

import psycopg

from run_lifecycle_support import RunLifecyclePostgresTest


def _payload(*, gate_status="PASS", metric=1.0):
    claims = [
        {
            "claim_id": f"claim-{index}",
            "claim_type": "DERIVED_FACT",
            "criticality": "CRITICAL",
            "status": "VERIFIED",
            "checked_against": [f"evidence-{index}"],
        }
        for index in range(10)
    ]
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
            "retrieval_recall_at_20": metric,
            "retrieval_mrr": metric,
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
        "retrieval_results": [
            {
                "case_id": "retrieval-1",
                "status": "FOUND",
                "hit_at_5": True,
                "hit_at_20": True,
                "reciprocal_rank": 1.0,
                "expected_chunk_ids": ["chunk-1"],
                "expected_doc_ids": ["doc-1"],
                "returned_evidence_count": 1,
                "scope_leaks": [],
            }
        ],
        "e2e_results": [
            {
                "case_id": "e2e-1",
                "terminal_status": "SUCCEEDED",
                "result_kind": "V1_FORMAL",
                "result_version": "result-1",
                "run_id": "run-1",
                "provider_cost_micro_usd": 100,
                "report_text": "report",
                "candidate_keys": ["candidate-1"],
                "excluded_candidate_keys": [],
                "claims": claims,
                "passed": True,
                "first_diff": None,
            }
        ],
        "run_samples": [
            {
                "run_id": f"run-{index}",
                "session_id": "session-1",
                "release_id": "rel-v1",
                "active_execution_ms": 1000,
                "provider_token_count": 100,
                "provider_cost_micro_usd": 100,
                "budget_violation_count": 0,
                "reconciliation_pending": False,
                "reproducible": True,
            }
            for index in range(100)
        ],
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

    async def test_pass_summary_must_match_persisted_raw_results(self):
        from eval.eval_run import EvalRunIntegrityError, persist_eval_run

        payload = _payload()
        payload["retrieval_results"][0]["hit_at_5"] = False

        with self.assertRaises(EvalRunIntegrityError):
            await persist_eval_run(self.dsn, payload)

    async def test_pass_requires_persisted_raw_results(self):
        from eval.eval_run import EvalRunIntegrityError, persist_eval_run

        payload = _payload()
        payload.pop("retrieval_results")

        with self.assertRaises(EvalRunIntegrityError):
            await persist_eval_run(self.dsn, payload)
