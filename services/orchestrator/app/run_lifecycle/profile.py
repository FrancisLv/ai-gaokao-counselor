from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from app.run_lifecycle.models import (
    ProfileFields,
    RunLifecycleError,
    StructuredProfileInput,
)


@dataclass(frozen=True, slots=True)
class ProfileValidation:
    readiness: str
    fields: ProfileFields
    findings: tuple[dict[str, Any], ...]


def build_profile_fields(profile: StructuredProfileInput) -> ProfileFields:
    fields: ProfileFields = {}
    if profile.province:
        fields["province"] = {"state": "PROVIDED", "source": "USER_FORM", "value": profile.province}
    if profile.application_year not in (None, 0):
        fields["application_year"] = {
            "state": "PROVIDED",
            "source": "USER_FORM",
            "value": profile.application_year,
        }
    if profile.category:
        fields["category"] = {"state": "PROVIDED", "source": "USER_FORM", "value": profile.category}
    if profile.batch:
        fields["batch"] = {"state": "PROVIDED", "source": "USER_FORM", "value": profile.batch}
    else:
        fields["batch"] = {"state": "DEFAULT", "source": "SYSTEM_DEFAULT", "value": "本科批"}
    if profile.score is not None and profile.score != 0:
        fields["score"] = {"state": "PROVIDED", "source": "USER_FORM", "value": profile.score}
    if profile.subjects:
        fields["subjects"] = {
            "state": "PROVIDED",
            "source": "USER_FORM",
            "value": list(profile.subjects),
        }
    if profile.rank is not None:
        fields["rank"] = {"state": "PROVIDED", "source": "USER_TEXT", "value": profile.rank}
    return fields


async def validate_profile(
    conn: psycopg.AsyncConnection,
    *,
    release_id: str,
    fields: ProfileFields,
) -> ProfileValidation:
    findings: list[dict[str, Any]] = []
    required = ("province", "application_year", "category", "batch", "score", "subjects")
    for key in required:
        field = fields.get(key)
        if field is None or field["state"] in {"UNKNOWN", "DECLINED"} or field.get("value") is None:
            findings.append(
                {
                    "code": "PROFILE_REQUIRED_MISSING",
                    "field_keys": [key],
                    "blocking": True,
                    "message_key": f"profile.required.{key}",
                }
            )

    if "rank" in fields and "score" not in fields:
        findings.append(
            {
                "code": "PROFILE_UNSUPPORTED_INPUT",
                "field_keys": ["rank"],
                "blocking": True,
                "message_key": "profile.input.score_required",
            }
        )

    if fields.get("province", {}).get("value") not in (None, "湖南"):
        findings.append(
            {
                "code": "PROFILE_UNSUPPORTED_SCOPE",
                "field_keys": ["province"],
                "blocking": True,
                "message_key": "profile.scope.unsupported",
            }
        )
    if fields.get("category", {}).get("value") not in (None, "物理类", "历史类"):
        findings.append(
            {
                "code": "PROFILE_INVALID_VALUE",
                "field_keys": ["category"],
                "blocking": True,
                "message_key": "profile.category.invalid",
            }
        )
    if fields.get("batch", {}).get("value") not in (None, "本科批"):
        findings.append(
            {
                "code": "PROFILE_UNSUPPORTED_SCOPE",
                "field_keys": ["batch"],
                "blocking": True,
                "message_key": "profile.batch.unsupported",
            }
        )
    score = fields.get("score", {}).get("value")
    if score is not None and not 0 <= score <= 750:
        findings.append(
            {
                "code": "PROFILE_INVALID_VALUE",
                "field_keys": ["score"],
                "blocking": True,
                "message_key": "profile.score.invalid",
            }
        )

    subjects = fields.get("subjects", {}).get("value")
    category = fields.get("category", {}).get("value")
    if subjects is not None:
        valid_second = {"化学", "生物", "政治", "地理"}
        expected_first = {"物理类": "物理", "历史类": "历史"}.get(category)
        if (
            len(subjects) != 3
            or len(set(subjects)) != 3
            or expected_first is None
            or subjects[0] != expected_first
            or any(subject not in valid_second for subject in subjects[1:])
        ):
            findings.append(
                {
                    "code": "PROFILE_INVALID_VALUE",
                    "field_keys": ["subjects"],
                    "blocking": True,
                    "message_key": "profile.subjects.invalid",
                }
            )

    province = fields.get("province", {}).get("value")
    application_year = fields.get("application_year", {}).get("value")
    batch = fields.get("batch", {}).get("value")
    if province and application_year and category and batch:
        scope = await (
            await conn.execute(
                """
                SELECT 1
                FROM release_scope
                WHERE release_id = %s
                  AND province = %s
                  AND application_year = %s
                  AND category = %s
                  AND batch = %s
                """,
                (release_id, province, application_year, category, batch),
            )
        ).fetchone()
        if scope is None:
            findings.append(
                {
                    "code": "PROFILE_UNSUPPORTED_SCOPE",
                    "field_keys": ["province", "application_year", "category", "batch"],
                    "blocking": True,
                    "message_key": "profile.scope.unsupported",
                }
            )

    findings.sort(key=lambda item: (item["code"], item["field_keys"], item["message_key"]))
    if any(item["blocking"] for item in findings):
        readiness = "UNSUPPORTED" if any(
            item["code"] in {"PROFILE_UNSUPPORTED_SCOPE", "PROFILE_UNSUPPORTED_INPUT"}
            for item in findings
        ) else "NEEDS_INPUT"
    else:
        readiness = "READY_FOR_CONFIRMATION"
    return ProfileValidation(readiness=readiness, fields=fields, findings=tuple(findings))


async def create_profile_revision(
    conn: psycopg.AsyncConnection,
    *,
    session_id,
    run_id,
    fields: ProfileFields,
    based_on=None,
) -> dict[str, Any]:
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"profile-session:{session_id}",),
    )
    row = await (
        await conn.execute(
            """
            SELECT COALESCE(MAX(revision_no), 0) + 1 AS next_revision
            FROM profile_revision
            WHERE session_id = %s
            """,
            (session_id,),
        )
    ).fetchone()
    profile_version = uuid4()
    revision_no = row["next_revision"]
    await conn.execute(
        """
        INSERT INTO profile_revision (
            profile_version, session_id, run_id, revision_no,
            based_on, lifecycle, fields
        )
        VALUES (%s, %s, %s, %s, %s, 'DRAFT', %s)
        """,
        (
            profile_version,
            session_id,
            run_id,
            revision_no,
            based_on,
            Jsonb(fields),
        ),
    )
    return {
        "profile_version": profile_version,
        "revision_no": revision_no,
        "based_on": based_on,
        "lifecycle": "DRAFT",
        "fields": fields,
    }


class ProfileRuntime:
    """RunLifecycle 内部使用的画像深模块；不是公开 seam。"""

    async def CreateDraft(
        self,
        conn,
        *,
        session_id,
        run_id,
        fields,
        based_on=None,
    ):
        return await create_profile_revision(
            conn,
            session_id=session_id,
            run_id=run_id,
            fields=fields,
            based_on=based_on,
        )

    async def ApplyPatch(
        self,
        conn,
        *,
        session_id,
        run_id,
        base_profile_version,
        patch,
        idempotency_key=None,
    ):
        revision = await (
            await conn.execute(
                """
                SELECT fields
                FROM profile_revision
                WHERE profile_version = %s
                  AND session_id = %s
                  AND lifecycle = 'DRAFT'
                FOR UPDATE
                """,
                (base_profile_version, session_id),
            )
        ).fetchone()
        if revision is None:
            raise RunLifecycleError("PROFILE_VERSION_CONFLICT")
        fields = dict(revision["fields"])
        fields.update(patch)
        await conn.execute(
            """
            UPDATE profile_revision
            SET lifecycle = 'SUPERSEDED'
            WHERE profile_version = %s
            """,
            (base_profile_version,),
        )
        return await self.CreateDraft(
            conn,
            session_id=session_id,
            run_id=run_id,
            fields=fields,
            based_on=base_profile_version,
        )

    async def Validate(self, conn, *, release_id, fields):
        return await validate_profile(conn, release_id=release_id, fields=fields)

    async def Confirm(
        self,
        conn,
        *,
        profile_version,
        actor,
        session_id,
    ):
        await conn.execute(
            """
            UPDATE profile_revision
            SET lifecycle = 'SUPERSEDED'
            WHERE session_id = %s
              AND lifecycle = 'CONFIRMED'
              AND profile_version <> %s
            """,
            (session_id, profile_version),
        )
        updated = await (
            await conn.execute(
                """
                UPDATE profile_revision
                SET lifecycle = 'CONFIRMED', confirmed_at = NOW()
                WHERE profile_version = %s AND lifecycle = 'DRAFT'
                RETURNING profile_version
                """,
                (profile_version,),
            )
        ).fetchone()
        if updated is None:
            raise RunLifecycleError("PROFILE_VERSION_CONFLICT")
        await conn.execute(
            """
            INSERT INTO profile_confirmation (
                confirmation_id, profile_version, confirmed_by
            )
            VALUES (%s, %s, %s)
            ON CONFLICT (profile_version) DO NOTHING
            """,
            (uuid4(), profile_version, actor),
        )

    def ToAlgoInput(self, *, profile_version, lifecycle, fields):
        if lifecycle != "CONFIRMED":
            raise RunLifecycleError("PROFILE_NOT_CONFIRMED")
        return {
            "profile_version": str(profile_version),
            "province": fields["province"]["value"],
            "year": str(fields["application_year"]["value"]),
            "category": fields["category"]["value"],
            "batch": fields["batch"]["value"],
            "score": fields["score"]["value"],
            "subjects": list(fields["subjects"]["value"]),
            "profile": {
                "risk_appetite": fields.get("preferences.risk_appetite", {}).get(
                    "value", "balanced"
                ),
                "priority_dims": fields.get(
                    "preferences.priority_dims", {}
                ).get("value", []),
                "interests": fields.get(
                    "preferences.major_interests", {}
                ).get("value", []),
                "preferred_cities": fields.get(
                    "preferences.preferred_cities", {}
                ).get("value", []),
                "preferred_tiers": fields.get(
                    "preferences.preferred_tiers", {}
                ).get("value", []),
            },
        }
