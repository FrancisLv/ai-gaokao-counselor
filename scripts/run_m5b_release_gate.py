#!/usr/bin/env python3
"""执行 M5b 聚合发布门禁；仅 PASS 时退出 0。"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
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
    persist_eval_run,
)
from eval.release_gate import (  # noqa: E402
    SecurityGateEvidence,
    evaluate_m5b_release_gate,
)


def _security_evidence(path: Path) -> tuple[SecurityGateEvidence, ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("security evidence is unreadable") from exc
    if not isinstance(payload, list):
        raise ValueError("security evidence must be a JSON array")
    evidence = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("security evidence entries must be objects")
        if (
            not isinstance(item.get("gate_id"), str)
            or not item["gate_id"].strip()
            or not isinstance(item.get("passed"), bool)
            or "threshold" not in item
        ):
            raise ValueError("security evidence entry is incomplete")
        for field in ("failed_case_ids", "resource_types"):
            values = item.get(field, ())
            if not isinstance(values, list) or not all(
                isinstance(value, str) and value.strip()
                for value in values
            ):
                raise ValueError(
                    f"security evidence {field} must be a string array"
                )
        evidence.append(
            SecurityGateEvidence(
                gate_id=item["gate_id"],
                passed=item["passed"],
                actual=item.get("actual"),
                threshold=item.get("threshold"),
                baseline=item.get("baseline"),
                failed_case_ids=tuple(item.get("failed_case_ids", ())),
                resource_types=tuple(item.get("resource_types", ())),
            )
        )
    return tuple(evidence)


async def run_release_gate(
    *,
    dsn: str,
    dataset_dir: Path,
    security_evidence_path: Path,
    eval_run_id: str | None = None,
    eval_run_path: Path | None = None,
) -> tuple[dict, int]:
    dataset = load_eval_dataset(dataset_dir)
    if eval_run_path is not None:
        record = await _persist_file(dsn, eval_run_path)
    elif eval_run_id is not None:
        record = await load_eval_run(dsn, eval_run_id)
    else:
        raise ValueError("--eval-run-id or --eval-run-file is required")
    decision = evaluate_m5b_release_gate(
        dataset=dataset,
        eval_run=record,
        security_evidence=_security_evidence(security_evidence_path),
    )
    payload = asdict(decision)
    return payload, 0 if decision.gate_status == "PASS" else 2


async def _persist_file(dsn: str, path: Path):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("EvalRun file is unreadable") from exc
    record, _ = await persist_eval_run(dsn, payload)
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument("--security-evidence", type=Path, required=True)
    parser.add_argument("--eval-run-id")
    parser.add_argument("--eval-run-file", type=Path)
    args = parser.parse_args(argv)
    if bool(args.eval_run_id) == bool(args.eval_run_file):
        parser.error("exactly one of --eval-run-id or --eval-run-file is required")
    try:
        import asyncio

        payload, exit_code = asyncio.run(
            run_release_gate(
                dsn=args.dsn,
                dataset_dir=args.dataset_dir,
                security_evidence_path=args.security_evidence,
                eval_run_id=args.eval_run_id,
                eval_run_path=args.eval_run_file,
            )
        )
    except (ValueError, EvalRunIntegrityError) as exc:
        print(json.dumps({"gate_status": "FAIL", "error": str(exc)}))
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
