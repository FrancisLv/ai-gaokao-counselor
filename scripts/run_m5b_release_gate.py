#!/usr/bin/env python3
"""执行 M5b 聚合发布门禁；仅 PASS 时退出 0。"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATOR = ROOT / "services" / "orchestrator"
for path in (str(ORCHESTRATOR), str(ORCHESTRATOR / "pb")):
    if path not in sys.path:
        sys.path.insert(0, path)

from eval.eval_dataset import load_eval_dataset  # noqa: E402
from eval.eval_run import (  # noqa: E402
    EvalRunIntegrityError,
    load_eval_run,
)
from eval.release_gate import evaluate_m5b_release_gate  # noqa: E402
from eval.security_evidence import (  # noqa: E402
    SecurityGateEvidenceIntegrityError,
    SubprocessSecurityGateRunner,
    collect_security_gate_evidence,
)


async def run_release_gate(
    *,
    dsn: str,
    security_test_dsn: str,
    dataset_dir: Path,
    eval_run_id: str,
    git_sha: str | None = None,
    security_runner=None,
) -> tuple[dict, int]:
    git_sha = git_sha or _current_git_sha()
    dataset = load_eval_dataset(dataset_dir)
    eval_run = await load_eval_run(dsn, eval_run_id)
    if security_runner is None:
        if security_test_dsn == dsn:
            raise ValueError(
                "security test DSN must be isolated from the release DSN"
            )
        security_runner = SubprocessSecurityGateRunner(
            dsn=security_test_dsn,
            root=ROOT,
        )
    records = await collect_security_gate_evidence(
        dsn,
        eval_run=eval_run,
        dataset=dataset,
        git_sha=git_sha,
        runner=security_runner,
        root=ROOT,
    )
    decision = await evaluate_m5b_release_gate(
        dsn=dsn,
        dataset=dataset,
        eval_run_id=eval_run_id,
        security_artifact_ids=tuple(
            record.artifact_id for record in records
        ),
        expected_git_sha=git_sha,
    )
    payload = asdict(decision)
    return payload, 0 if decision.gate_status == "PASS" else 2


def _current_git_sha() -> str:
    return subprocess.check_output(
        ("git", "rev-parse", "HEAD"),
        cwd=ROOT,
        text=True,
    ).strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--security-test-dsn", required=True)
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument("--eval-run-id", required=True)
    args = parser.parse_args(argv)
    try:
        import asyncio

        payload, exit_code = asyncio.run(
            run_release_gate(
                dsn=args.dsn,
                security_test_dsn=args.security_test_dsn,
                dataset_dir=args.dataset_dir,
                eval_run_id=args.eval_run_id,
            )
        )
    except (
        ValueError,
        EvalRunIntegrityError,
        SecurityGateEvidenceIntegrityError,
    ) as exc:
        print(json.dumps({"gate_status": "FAIL", "error": str(exc)}))
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
