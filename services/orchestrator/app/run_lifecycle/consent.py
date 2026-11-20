"""敏感资格数据使用的 consent 闸门；由 RunLifecycle 内部调用。"""
from __future__ import annotations

from app.run_lifecycle.models import ProfileFields, RunLifecycleError


CONSENT_POLICY_VERSION = "m5b-consent-v1"
CONSENT_PURPOSE = "SENSITIVE_ELIGIBILITY_EVALUATION"
CONSENT_DATA_CLASS = "SENSITIVE_ELIGIBILITY_DATA"

_SENSITIVE_FIELD_KEYS = frozenset(
    {
        "gender",
        "household_province",
        "school_registration_province",
    }
)
_SENSITIVE_FIELD_PREFIXES = (
    "physical_exam.",
    "vision.",
    "health.",
)


def sensitive_field_keys(fields: ProfileFields) -> tuple[str, ...]:
    keys: list[str] = []
    for key in sorted(fields):
        field = fields[key]
        if field.get("state") not in {"PROVIDED", "DERIVED"}:
            continue
        if field.get("value") is None:
            continue
        if key in _SENSITIVE_FIELD_KEYS or key.startswith(
            _SENSITIVE_FIELD_PREFIXES
        ):
            keys.append(key)
    return tuple(keys)


def redact_sensitive_profile_fields(fields: ProfileFields) -> ProfileFields:
    sensitive = set(sensitive_field_keys(fields))
    if not sensitive:
        return fields
    redacted: ProfileFields = {}
    for key, field in fields.items():
        if key not in sensitive:
            redacted[key] = field
            continue
        redacted[key] = {
            "state": field["state"],
            "source": field["source"],
            "value": None,
        }
    return redacted


async def require_sensitive_consent(
    conn,
    *,
    principal_id: str,
    fields: ProfileFields,
    policy_version: str = CONSENT_POLICY_VERSION,
) -> None:
    if not sensitive_field_keys(fields):
        return
    row = await (
        await conn.execute(
            """
            SELECT
                COALESCE(BOOL_OR(
                    revoked_at IS NULL
                    AND policy_version = %s
                ), FALSE) AS active,
                COALESCE(BOOL_OR(
                    revoked_at IS NOT NULL
                    AND policy_version = %s
                ), FALSE) AS revoked
            FROM consent_record
            WHERE principal_id = %s
              AND purpose = %s
              AND %s = ANY(data_classes)
            """,
            (
                policy_version,
                policy_version,
                principal_id,
                CONSENT_PURPOSE,
                CONSENT_DATA_CLASS,
            ),
        )
    ).fetchone()
    if row is not None and row["active"]:
        return
    if row is not None and row["revoked"]:
        raise RunLifecycleError(
            "CONSENT_REVOKED",
            "敏感资格数据的同意已撤销",
        )
    raise RunLifecycleError(
        "CONSENT_REQUIRED",
        "敏感资格数据处理需要当前有效的同意",
    )
