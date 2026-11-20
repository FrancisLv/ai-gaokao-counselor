"""M5b 安全测试的受控执行、不可变 attestation 与数据库读取。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Protocol

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from eval.eval_dataset import JointEvalDataset
from eval.eval_run import EvalRunRecord
from eval.release_models import SecurityGateEvidence


SECURITY_GATE_SUITES = {
    "CONSENT": "test_run_lifecycle_consent",
    "DELETION": "test_data_deletion",
    "RETENTION": "test_data_retention",
    "RESTORE": "test_data_restore",
}


class SecurityGateEvidenceIntegrityError(ValueError):
    """安全证据无法由内容、身份或数据库记录自证。"""


@dataclass(frozen=True, slots=True)
class SecurityGateSuiteResult:
    gate_id: str
    passed: bool
    suite: str
    exit_code: int
    failed_case_ids: tuple[str, ...] = ()
    resource_types: tuple[str, ...] = ()


class SecurityGateRunner(Protocol):
    async def run(self, gate_id: str) -> SecurityGateSuiteResult: ...


class SubprocessSecurityGateRunner:
    """在独立进程中运行仓库内固定的安全测试模块。"""

    def __init__(self, *, dsn: str, root: Path) -> None:
        self._dsn = dsn
        self._root = root

    async def run(self, gate_id: str) -> SecurityGateSuiteResult:
        suite = SECURITY_GATE_SUITES[gate_id]
        orchestrator = self._root / "services" / "orchestrator"
        env = {
            **os.environ,
            "ORCHESTRATOR_TEST_DSN": self._dsn,
            "PYTHONPATH": os.pathsep.join(
                (
                    str(orchestrator),
                    str(orchestrator / "pb"),
                    str(orchestrator / "tests"),
                )
            ),
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
        }
        process = await asyncio.create_subprocess_exec(
            os.sys.executable,
            "-m",
            "unittest",
            suite,
            cwd=orchestrator,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, _ = await process.communicate()
        passed = process.returncode == 0
        return SecurityGateSuiteResult(
            gate_id=gate_id,
            passed=passed,
            suite=suite,
            exit_code=process.returncode or 0,
            failed_case_ids=() if passed else (suite,),
        )


def finalize_security_gate_evidence(
    *,
    suite_result: SecurityGateSuiteResult,
    eval_run: EvalRunRecord,
    dataset: JointEvalDataset,
    git_sha: str,
) -> SecurityGateEvidence:
    if suite_result.gate_id not in SECURITY_GATE_SUITES:
        raise SecurityGateEvidenceIntegrityError(
            "security gate id is unsupported"
        )
    if suite_result.suite != SECURITY_GATE_SUITES[suite_result.gate_id]:
        raise SecurityGateEvidenceIntegrityError(
            "security suite does not match the gate"
        )
    if git_sha != eval_run.git_sha:
        raise SecurityGateEvidenceIntegrityError(
            "security evidence git_sha differs from EvalRun"
        )
    payload = {
        "schema_version": "release-gate-security-v1",
        "eval_run_id": eval_run.eval_run_id,
        "gate_id": suite_result.gate_id,
        "passed": suite_result.passed,
        "suite": suite_result.suite,
        "exit_code": suite_result.exit_code,
        "dataset_version": dataset.manifest["dataset_version"],
        "dataset_sha256": dataset.manifest["dataset_sha256"],
        "release_id": dataset.manifest["release_id"],
        "git_sha": git_sha,
        "failed_case_ids": sorted(set(suite_result.failed_case_ids)),
        "resource_types": sorted(set(suite_result.resource_types)),
        "actual": {"exit_code": suite_result.exit_code},
        "threshold": {"exit_code": 0},
        "baseline": None,
    }
    artifact_sha256 = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    artifact_id = f"security_{artifact_sha256}"
    return SecurityGateEvidence(
        artifact_id=artifact_id,
        artifact_sha256=artifact_sha256,
        eval_run_id=eval_run.eval_run_id,
        dataset_version=dataset.manifest["dataset_version"],
        dataset_sha256=dataset.manifest["dataset_sha256"],
        release_id=dataset.manifest["release_id"],
        git_sha=git_sha,
        suite=suite_result.suite,
        gate_id=suite_result.gate_id,
        passed=suite_result.passed,
        actual=payload["actual"],
        threshold=payload["threshold"],
        failed_case_ids=tuple(payload["failed_case_ids"]),
        resource_types=tuple(payload["resource_types"]),
    )


async def collect_security_gate_evidence(
    dsn: str,
    *,
    eval_run: EvalRunRecord,
    dataset: JointEvalDataset,
    git_sha: str,
    runner: SecurityGateRunner | None = None,
    root: Path | None = None,
) -> tuple[SecurityGateEvidence, ...]:
    runner = runner or SubprocessSecurityGateRunner(
        dsn=dsn,
        root=root or Path(__file__).resolve().parents[3],
    )
    records = []
    for gate_id in SECURITY_GATE_SUITES:
        result = await runner.run(gate_id)
        record = finalize_security_gate_evidence(
            suite_result=result,
            eval_run=eval_run,
            dataset=dataset,
            git_sha=git_sha,
        )
        await _persist_security_gate_evidence(dsn, record)
        records.append(record)
    return tuple(records)


async def load_security_gate_evidence(
    dsn: str,
    artifact_ids: tuple[str, ...],
) -> tuple[SecurityGateEvidence, ...]:
    if len(artifact_ids) != len(set(artifact_ids)):
        raise SecurityGateEvidenceIntegrityError(
            "security artifact ids must be unique"
        )
    conn = await psycopg.AsyncConnection.connect(dsn, row_factory=dict_row)
    try:
        rows = await (
            await conn.execute(
                """
                SELECT artifact_id, artifact_sha256, eval_run_id,
                       dataset_version, dataset_sha256, release_id, git_sha,
                       gate_id, passed, suite, payload
                FROM release_gate_security_record
                WHERE artifact_id = ANY(%s)
                """,
                (list(artifact_ids),),
            )
        ).fetchall()
    finally:
        await conn.close()
    by_id = {row["artifact_id"]: row for row in rows}
    if set(by_id) != set(artifact_ids):
        raise SecurityGateEvidenceIntegrityError(
            "one or more security artifacts were not found"
        )
    return tuple(
        verify_security_gate_evidence(by_id[artifact_id])
        for artifact_id in artifact_ids
    )


def verify_security_gate_evidence(row: dict) -> SecurityGateEvidence:
    payload = row.get("payload")
    if not isinstance(payload, dict):
        raise SecurityGateEvidenceIntegrityError(
            "security evidence payload is required"
        )
    artifact_sha256 = row.get("artifact_sha256")
    expected_id = f"security_{artifact_sha256}"
    if row.get("artifact_id") != expected_id:
        raise SecurityGateEvidenceIntegrityError(
            "security artifact id does not match hash"
        )
    canonical_payload = {
        key: payload.get(key)
        for key in (
            "schema_version",
            "eval_run_id",
            "gate_id",
            "passed",
            "suite",
            "exit_code",
            "dataset_version",
            "dataset_sha256",
            "release_id",
            "git_sha",
            "failed_case_ids",
            "resource_types",
            "actual",
            "threshold",
            "baseline",
        )
    }
    actual_sha256 = hashlib.sha256(
        json.dumps(
            canonical_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if actual_sha256 != artifact_sha256:
        raise SecurityGateEvidenceIntegrityError(
            "security artifact content hash mismatch"
        )
    identity_fields = (
        "eval_run_id",
        "gate_id",
        "dataset_version",
        "dataset_sha256",
        "release_id",
        "git_sha",
    )
    for field in identity_fields:
        if payload.get(field) != row.get(field):
            raise SecurityGateEvidenceIntegrityError(
                f"security evidence stored column mismatch: {field}"
            )
    return SecurityGateEvidence(
        artifact_id=row["artifact_id"],
        artifact_sha256=artifact_sha256,
        eval_run_id=row["eval_run_id"],
        dataset_version=row["dataset_version"],
        dataset_sha256=row["dataset_sha256"],
        release_id=row["release_id"],
        git_sha=row["git_sha"],
        suite=row["suite"],
        gate_id=row["gate_id"],
        passed=row["passed"],
        actual=payload.get("actual"),
        threshold=payload.get("threshold"),
        baseline=payload.get("baseline"),
        failed_case_ids=tuple(payload.get("failed_case_ids") or ()),
        resource_types=tuple(payload.get("resource_types") or ()),
    )


async def _persist_security_gate_evidence(
    dsn: str,
    record: SecurityGateEvidence,
) -> None:
    payload = {
        "schema_version": "release-gate-security-v1",
        "eval_run_id": record.eval_run_id,
        "gate_id": record.gate_id,
        "passed": record.passed,
        "suite": record.suite,
        "exit_code": record.actual["exit_code"],
        "dataset_version": record.dataset_version,
        "dataset_sha256": record.dataset_sha256,
        "release_id": record.release_id,
        "git_sha": record.git_sha,
        "failed_case_ids": list(record.failed_case_ids),
        "resource_types": list(record.resource_types),
        "actual": record.actual,
        "threshold": record.threshold,
        "baseline": record.baseline,
    }
    conn = await psycopg.AsyncConnection.connect(dsn, row_factory=dict_row)
    try:
        async with conn.transaction():
            existing = await (
                await conn.execute(
                    """
                    SELECT artifact_sha256
                    FROM release_gate_security_record
                    WHERE artifact_id = %s
                    FOR UPDATE
                    """,
                    (record.artifact_id,),
                )
            ).fetchone()
            if existing is not None:
                if existing["artifact_sha256"] != record.artifact_sha256:
                    raise SecurityGateEvidenceIntegrityError(
                        "security artifact id conflict"
                    )
                return
            await conn.execute(
                """
                INSERT INTO release_gate_security_record (
                    artifact_id, artifact_sha256, eval_run_id,
                    dataset_version, dataset_sha256, release_id, git_sha,
                    gate_id, passed, suite, payload
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    record.artifact_id,
                    record.artifact_sha256,
                    record.eval_run_id,
                    record.dataset_version,
                    record.dataset_sha256,
                    record.release_id,
                    record.git_sha,
                    record.gate_id,
                    record.passed,
                    record.suite,
                    Jsonb(payload),
                ),
            )
    finally:
        await conn.close()
