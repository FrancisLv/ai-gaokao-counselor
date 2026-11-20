"""质量、SLO、安全证据与聚合发布结论的稳定数据类型。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class RunSloSample:
    run_id: str
    session_id: str
    release_id: str
    active_execution_ms: int
    provider_token_count: int
    provider_cost_micro_usd: int
    budget_violation_count: int
    reconciliation_pending: bool
    reproducible: bool


@dataclass(frozen=True, slots=True)
class PerformanceContext:
    environment: str
    hardware_profile: str
    database_data_size: str
    model_route_version: str
    cache_state: str
    concurrency: int


@dataclass(frozen=True, slots=True)
class ReleaseGateReport:
    gate_status: str
    failed_metrics: tuple[str, ...]
    metrics: dict[str, float | int | str]
    failure_details: tuple[dict, ...]


@dataclass(frozen=True, slots=True)
class SecurityGateEvidence:
    gate_id: str
    passed: bool
    actual: Any
    threshold: Any
    baseline: Any = None
    failed_case_ids: tuple[str, ...] = ()
    resource_types: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReleaseGateDecision:
    schema_version: str
    gate_status: str
    failed_gates: tuple[str, ...]
    failed_metrics: tuple[str, ...]
    failed_case_ids: tuple[str, ...]
    release_id: str
    eval_run_id: str
    dataset_version: str
    dataset_sha256: str
    git_sha: str
    gates: tuple[dict[str, Any], ...]
    failure_details: tuple[dict[str, Any], ...]
    artifact_sha256: str
