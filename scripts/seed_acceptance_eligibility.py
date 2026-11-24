"""Seed a clearly synthetic, release-bound eligibility fixture for local acceptance."""
from __future__ import annotations

import argparse
import json

import psycopg
from psycopg.types.json import Jsonb


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--release-id", default="rel_legacy_v1")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    with psycopg.connect(args.dsn, autocommit=False) as conn:
        database = conn.execute("SELECT current_database()").fetchone()[0]
        if not any(
            marker in database.lower()
            for marker in ("test", "acceptance")
        ):
            raise SystemExit(f"拒绝写入非测试/验收数据库: {database}")

        release = conn.execute(
            "SELECT status FROM release WHERE release_id = %s FOR UPDATE",
            (args.release_id,),
        ).fetchone()
        if release is None:
            raise SystemExit(f"release 不存在: {args.release_id}")

        conn.execute(
            """
            UPDATE release
            SET status = 'STAGING', published_at = NULL
            WHERE release_id = %s
            """,
            (args.release_id,),
        )
        conn.execute(
            """
            INSERT INTO eligibility_profile_field (
                release_id, field_key, value_type, allowed_values,
                description, created_at
            )
            SELECT %s, field_key, value_type, allowed_values,
                   description, created_at
            FROM eligibility_profile_field_template
            ON CONFLICT (release_id, field_key) DO NOTHING
            """,
            (args.release_id,),
        )
        conn.execute(
            """
            INSERT INTO rule_coverage (
                release_id, rule_type, scope, status,
                covered_data_sources, notes
            )
            VALUES (
                %s, 'SUBJECT_REQUIREMENT', '{}'::jsonb, 'FULL',
                '["major_admission", "enrollment_plan"]'::jsonb,
                '技术验收合成夹具：候选选科已由 EstimateProb 硬过滤；'
                '本规则仅让已过滤候选进入正式资格确认，不代表完整业务规则。'
            )
            ON CONFLICT DO NOTHING
            """,
            (args.release_id,),
        )

        for category in ("物理类", "历史类"):
            rule_id = f"acceptance-category-{category}"
            rule_key = "synthetic-category-subject-coverage"
            existing = conn.execute(
                """
                SELECT review_status
                FROM rule_version
                WHERE rule_id = %s AND version = 1
                """,
                (rule_id,),
            ).fetchone()
            if existing is None:
                expression = {
                    "op": "NOT",
                    "child": {
                        "op": "EQ",
                        "field": "category",
                        "value": category,
                    },
                }
                conn.execute(
                    """
                    INSERT INTO rule_version (
                        rule_id, version, rule_key, rule_type, scope,
                        required_profile_fields, expression,
                        outcome_when_true, reason_code, review_status,
                        release_id, source_evidence, created_by
                    )
                    VALUES (
                        %s, 1, %s, 'SUBJECT_REQUIREMENT', %s,
                        '["category"]'::jsonb, %s, 'FAIL',
                        'SYNTHETIC_CATEGORY_MISMATCH', 'DRAFT',
                        NULL, %s, 'acceptance-seed'
                    )
                    """,
                    (
                        rule_id,
                        rule_key,
                        Jsonb({"category": category}),
                        Jsonb(expression),
                        Jsonb(
                            [
                                {
                                    "evidence_id": (
                                        f"synthetic:{args.release_id}:{category}"
                                    ),
                                    "evidence_type": "DOCUMENT",
                                    "release_id": args.release_id,
                                    "content_sha256": (
                                        f"synthetic-{category}-v1"
                                    ),
                                }
                            ]
                        ),
                    ),
                )
                conn.execute(
                    """
                    UPDATE rule_version
                    SET review_status = 'REVIEWED',
                        reviewed_by = 'acceptance-reviewer',
                        reviewed_at = NOW()
                    WHERE rule_id = %s AND version = 1
                    """,
                    (rule_id,),
                )
                conn.execute(
                    """
                    UPDATE rule_version
                    SET review_status = 'PUBLISHED',
                        release_id = %s,
                        published_by = 'acceptance-publisher',
                        published_at = NOW()
                    WHERE rule_id = %s AND version = 1
                    """,
                    (args.release_id, rule_id),
                )
            elif existing[0] != "PUBLISHED":
                raise SystemExit(
                    f"已有规则不是 PUBLISHED 状态: {rule_id} {existing[0]}"
                )

        conn.execute(
            """
            UPDATE release
            SET status = 'PUBLISHED', published_at = NOW()
            WHERE release_id = %s
            """,
            (args.release_id,),
        )
        conn.commit()
        print(
            json.dumps(
                {
                    "release_id": args.release_id,
                    "coverage": "FULL",
                    "rules": [
                        "acceptance-category-物理类",
                        "acceptance-category-历史类",
                    ],
                    "synthetic": True,
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
