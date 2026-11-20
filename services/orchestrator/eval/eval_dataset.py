"""联合检索与端到端评测集的不可变装载。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path

from eval.retrieval_eval import _validate_case as _validate_retrieval_case


_OWNER_STATUSES = frozenset({"DRAFT", "PENDING_SIGNOFF", "SIGNED_OFF"})
_E2E_CRITICALITIES = frozenset({"CRITICAL", "NORMAL"})
_E2E_TERMINAL_STATUSES = frozenset(
    {
        "SUCCEEDED",
        "FAILED",
        "EXPIRED",
        "CANCELLED",
        "WAITING_HITL",
        "CANCELLED_OR_SUCCEEDED",
    }
)
_REQUIRED_MANIFEST_FIELDS = (
    "dataset_version",
    "dataset_sha256",
    "retrieval_cases_file",
    "retrieval_cases_sha256",
    "retrieval_case_count",
    "e2e_cases_file",
    "e2e_cases_sha256",
    "e2e_case_count",
    "created_at",
    "release_id",
    "owner",
    "owner_status",
    "immutable",
)
_REQUIRED_E2E_FIELDS = (
    "case_id",
    "input_profile",
    "scripted_model_responses",
    "expected",
    "tags",
    "criticality",
)
_REQUIRED_EXPECTED_FIELDS = (
    "result_kind",
    "terminal_status",
    "excluded_candidate_keys",
    "required_claim_types",
    "forbidden_text_patterns",
    "max_provider_cost_micro_usd",
)


@dataclass(frozen=True, slots=True)
class JointEvalDataset:
    manifest: dict
    retrieval_cases: tuple[dict, ...]
    e2e_cases: tuple[dict, ...]

    @property
    def is_authoritative_quality(self) -> bool:
        return self.manifest["owner_status"] == "SIGNED_OFF"


def load_eval_dataset(dataset_dir: str | Path) -> JointEvalDataset:
    root = Path(dataset_dir)
    try:
        manifest = json.loads(
            (root / "manifest.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("EvalDataset manifest is unreadable") from exc
    _validate_manifest(manifest, expected_version=root.name)

    retrieval_path = root / manifest["retrieval_cases_file"]
    e2e_path = root / manifest["e2e_cases_file"]
    retrieval_bytes = _read_file(retrieval_path, "retrieval cases")
    e2e_bytes = _read_file(e2e_path, "end-to-end cases")
    retrieval_sha256 = hashlib.sha256(retrieval_bytes).hexdigest()
    e2e_sha256 = hashlib.sha256(e2e_bytes).hexdigest()
    if retrieval_sha256 != manifest["retrieval_cases_sha256"]:
        raise ValueError("retrieval cases sha256 does not match the manifest")
    if e2e_sha256 != manifest["e2e_cases_sha256"]:
        raise ValueError("end-to-end cases sha256 does not match the manifest")
    if combined_dataset_sha256(retrieval_sha256, e2e_sha256) != manifest[
        "dataset_sha256"
    ]:
        raise ValueError("dataset_sha256 does not match the immutable files")

    retrieval_cases = _decode_jsonl(retrieval_bytes, "retrieval cases")
    e2e_cases = _decode_jsonl(e2e_bytes, "end-to-end cases")
    if len(retrieval_cases) != manifest["retrieval_case_count"]:
        raise ValueError(
            "retrieval_case_count does not match the retrieval cases file"
        )
    if len(e2e_cases) != manifest["e2e_case_count"]:
        raise ValueError(
            "e2e_case_count does not match the end-to-end cases file"
        )
    if "case_count" in manifest and manifest["case_count"] != (
        len(retrieval_cases) + len(e2e_cases)
    ):
        raise ValueError("manifest.case_count does not match component counts")

    _validate_case_ids(retrieval_cases, "retrieval")
    for case in retrieval_cases:
        _validate_retrieval_case(case)
    _validate_case_ids(e2e_cases, "end-to-end")
    for case in e2e_cases:
        _validate_e2e_case(case, manifest)
    required_scenarios = set(manifest.get("required_e2e_scenarios", ()))
    covered_scenarios = {
        tag
        for case in e2e_cases
        for tag in case["tags"]
    }
    missing_scenarios = sorted(required_scenarios - covered_scenarios)
    if missing_scenarios:
        raise ValueError(
            "end-to-end cases are missing required scenarios: "
            + ", ".join(missing_scenarios)
        )

    return JointEvalDataset(
        manifest=manifest,
        retrieval_cases=retrieval_cases,
        e2e_cases=e2e_cases,
    )


def combined_dataset_sha256(
    retrieval_cases_sha256: str,
    e2e_cases_sha256: str,
) -> str:
    payload = (
        retrieval_cases_sha256.encode("ascii")
        + b"\0"
        + e2e_cases_sha256.encode("ascii")
    )
    return hashlib.sha256(payload).hexdigest()


def _read_file(path: Path, label: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ValueError(f"{label} file is unreadable") from exc


def _decode_jsonl(content: bytes, label: str) -> tuple[dict, ...]:
    try:
        values = tuple(
            json.loads(line)
            for line in content.decode("utf-8").splitlines()
            if line.strip()
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} contains invalid JSONL") from exc
    if not values:
        raise ValueError(f"{label} must not be empty")
    if not all(isinstance(value, dict) for value in values):
        raise ValueError(f"{label} entries must be JSON objects")
    return values


def _validate_manifest(
    manifest: dict,
    *,
    expected_version: str,
) -> None:
    if not isinstance(manifest, dict):
        raise ValueError("EvalDataset manifest must be an object")
    for field in _REQUIRED_MANIFEST_FIELDS:
        if field not in manifest:
            raise ValueError(f"manifest is missing required field: {field}")
    if manifest["immutable"] is not True:
        raise ValueError("manifest.immutable must be true")
    if manifest["owner_status"] not in _OWNER_STATUSES:
        raise ValueError("manifest.owner_status is unsupported")
    if manifest["dataset_version"] != expected_version:
        raise ValueError(
            "dataset_version does not match the immutable directory"
        )
    if manifest["retrieval_cases_file"] != "qa.jsonl":
        raise ValueError("retrieval_cases_file must be qa.jsonl")
    if manifest["e2e_cases_file"] != "cases.jsonl":
        raise ValueError("e2e_cases_file must be cases.jsonl")
    for field in (
        "dataset_sha256",
        "retrieval_cases_sha256",
        "e2e_cases_sha256",
    ):
        _validate_sha256(manifest[field], field)
    for field in ("retrieval_case_count", "e2e_case_count"):
        value = manifest[field]
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 1
        ):
            raise ValueError(f"manifest.{field} is invalid")
    try:
        datetime.fromisoformat(manifest["created_at"])
    except (TypeError, ValueError) as exc:
        raise ValueError("manifest.created_at is invalid") from exc
    if (
        manifest["owner_status"] == "SIGNED_OFF"
        and not str(manifest.get("owner", "")).strip()
    ):
        raise ValueError("SIGNED_OFF manifest requires an owner")


def _validate_sha256(value, field: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} is invalid")


def _validate_case_ids(cases: tuple[dict, ...], label: str) -> None:
    seen = set()
    for case in cases:
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"{label} case_id must be a non-empty string")
        if case_id in seen:
            raise ValueError(f"duplicate {label} case_id: {case_id}")
        seen.add(case_id)


def _validate_e2e_case(case: dict, manifest: dict) -> None:
    case_id = case["case_id"]
    for field in _REQUIRED_E2E_FIELDS:
        if field not in case:
            raise ValueError(f"{case_id}: missing field {field}")
    if case["criticality"] not in _E2E_CRITICALITIES:
        raise ValueError(f"{case_id}: criticality is unsupported")
    if (
        not isinstance(case["tags"], list)
        or not case["tags"]
        or not all(
            isinstance(tag, str) and tag.strip()
            for tag in case["tags"]
        )
    ):
        raise ValueError(f"{case_id}: tags must be a non-empty string list")
    if not isinstance(case["input_profile"], dict):
        raise ValueError(f"{case_id}: input_profile must be an object")
    if not isinstance(case["scripted_model_responses"], dict):
        raise ValueError(
            f"{case_id}: scripted_model_responses must be an object"
        )
    expected = case["expected"]
    if not isinstance(expected, dict):
        raise ValueError(f"{case_id}: expected must be an object")
    for field in _REQUIRED_EXPECTED_FIELDS:
        if field not in expected:
            raise ValueError(f"{case_id}: expected.{field} is required")
    if expected["terminal_status"] not in _E2E_TERMINAL_STATUSES:
        raise ValueError(f"{case_id}: expected.terminal_status is unsupported")
    for field in (
        "excluded_candidate_keys",
        "required_claim_types",
        "forbidden_text_patterns",
    ):
        if (
            not isinstance(expected[field], list)
            or not all(
                isinstance(value, str) and value
                for value in expected[field]
            )
        ):
            raise ValueError(f"{case_id}: expected.{field} is invalid")
    cost = expected["max_provider_cost_micro_usd"]
    if isinstance(cost, bool) or not isinstance(cost, int) or cost < 0:
        raise ValueError(
            f"{case_id}: expected.max_provider_cost_micro_usd is invalid"
        )
    if expected["terminal_status"] == "SUCCEEDED":
        if expected["result_kind"] != "V1_FORMAL":
            raise ValueError(
                f"{case_id}: successful cases must expect V1_FORMAL"
            )
        if not expected["required_claim_types"]:
            raise ValueError(
                f"{case_id}: successful cases require factual Claim types"
            )
    elif expected["result_kind"] is not None:
        raise ValueError(
            f"{case_id}: non-success cases must not expect a result kind"
        )
