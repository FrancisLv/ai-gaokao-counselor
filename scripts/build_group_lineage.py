"""Build release-bound cross-year admission group lineage mappings."""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import dataclass

import psycopg
from psycopg.rows import dict_row


LINEAGE_VERSION = "group-lineage-v2"
DEFAULT_THRESHOLD = 0.6


@dataclass(frozen=True)
class GroupProfile:
    university_code: str
    major_group: str
    subjects: frozenset[str]
    names: frozenset[str]


@dataclass(frozen=True)
class LineageMatch:
    historical: GroupProfile
    method: str
    confidence: float
    matched: tuple[str, ...]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--release-id", default="rel_legacy_v1")
    parser.add_argument("--target-year", type=int, default=2025)
    parser.add_argument("--province", default="湖南")
    parser.add_argument("--category", default="物理类")
    parser.add_argument("--batch", default="本科批")
    parser.add_argument("--window-years", type=int, default=3)
    parser.add_argument(
        "--name-threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
    )
    parser.add_argument("--replace", action="store_true")
    return parser.parse_args()


def normalize_name(value: str) -> str:
    return "".join(value.split())


def primary_subject(value: str) -> str:
    return value.split("，", 1)[0].strip()


def load_groups(
    conn,
    *,
    table: str,
    release_id: str,
    province: str,
    category: str,
    batch: str,
    year: int,
) -> dict[tuple[str, str], GroupProfile]:
    rows = conn.execute(
        f"""
        SELECT university_code, major_group,
               array_agg(DISTINCT subject_req) AS subjects,
               array_agg(DISTINCT major_name) AS names
        FROM {table}
        WHERE release_id = %s
          AND province = %s
          AND category = %s
          AND batch = %s
          AND year = %s
          AND NULLIF(BTRIM(major_code), '') IS NOT NULL
        GROUP BY university_code, major_group
        HAVING COUNT(DISTINCT university_id) = 1
           AND COUNT(DISTINCT university_name) = 1
        ORDER BY university_code, major_group
        """,
        (release_id, province, category, batch, year),
    ).fetchall()
    result = {}
    for row in rows:
        names = frozenset(
            normalized
            for value in row["names"] or ()
            if (normalized := normalize_name(value))
        )
        subjects = frozenset(
            normalized
            for value in row["subjects"] or ()
            if (normalized := primary_subject(value))
        )
        result[(row["university_code"], row["major_group"])] = GroupProfile(
            university_code=row["university_code"],
            major_group=row["major_group"],
            subjects=subjects,
            names=names,
        )
    return result


def jaccard(left: frozenset[str], right: frozenset[str]) -> tuple[float, tuple[str, ...]]:
    if not left or not right:
        return 0.0, ()
    matched = tuple(sorted(left & right))
    union = left | right
    return len(matched) / len(union), matched


def select_lineage_match(
    target: GroupProfile,
    candidates: list[GroupProfile],
    name_threshold: float,
) -> LineageMatch | None:
    """Select one history group by composition, using the code only as a tie-break."""
    unique = {
        (candidate.university_code, candidate.major_group): candidate
        for candidate in candidates
        if target.subjects & candidate.subjects
    }
    scored = []
    for historical in unique.values():
        score, matched = jaccard(target.names, historical.names)
        if score >= name_threshold:
            scored.append((score, historical, matched))
    if not scored:
        return None
    scored.sort(key=lambda item: (-item[0], item[1].major_group))
    top_score = scored[0][0]
    top = [
        item
        for item in scored
        if abs(item[0] - top_score) < 1e-9
    ]
    exact = [
        item
        for item in top
        if item[1].major_group == target.major_group
    ]
    if exact:
        _, historical, matched = exact[0]
        return LineageMatch(
            historical=historical,
            method="EXACT_CODE",
            confidence=1.0,
            matched=matched,
        )
    if len(top) > 1:
        return None
    score, historical, matched = top[0]
    return LineageMatch(
        historical=historical,
        method="MAJOR_NAME_JACCARD",
        confidence=score,
        matched=matched,
    )


def main() -> None:
    args = parse_args()
    history_years = tuple(
        range(
            args.target_year - args.window_years,
            args.target_year,
        )
    )
    with psycopg.connect(
        args.dsn,
        autocommit=False,
        row_factory=dict_row,
    ) as conn:
        database = conn.execute(
            "SELECT current_database()"
        ).fetchone()["current_database"]
        if args.replace and not any(
            marker in database.lower()
            for marker in ("test", "acceptance")
        ):
            raise SystemExit(f"拒绝重建非测试/验收数据库: {database}")
        release = conn.execute(
            "SELECT status FROM release WHERE release_id = %s",
            (args.release_id,),
        ).fetchone()
        if release is None:
            raise SystemExit(f"release 不存在: {args.release_id}")

        targets = load_groups(
            conn,
            table="enrollment_plan",
            release_id=args.release_id,
            province=args.province,
            category=args.category,
            batch=args.batch,
            year=args.target_year,
        )
        history = {
            year: load_groups(
                conn,
                table="major_admission",
                release_id=args.release_id,
                province=args.province,
                category=args.category,
                batch=args.batch,
                year=year,
            )
            for year in history_years
        }
        by_school_and_subject: dict[
            tuple[str, str],
            list[tuple[int, GroupProfile]],
        ] = defaultdict(list)
        for year, groups in history.items():
            for group in groups.values():
                for subject in group.subjects:
                    by_school_and_subject[
                        (group.university_code, subject)
                    ].append((year, group))

        mappings = []
        for target_key, target in targets.items():
            for year in history_years:
                candidates = []
                seen = set()
                for subject in sorted(target.subjects):
                    for candidate_year, historical in by_school_and_subject.get(
                        (target.university_code, subject),
                        (),
                    ):
                        if candidate_year != year:
                            continue
                        key = (
                            historical.university_code,
                            historical.major_group,
                        )
                        if key in seen:
                            continue
                        seen.add(key)
                        candidates.append(historical)
                match = select_lineage_match(
                    target,
                    candidates,
                    args.name_threshold,
                )
                if match is None:
                    continue
                mappings.append(
                    mapping_row(
                        args,
                        target=target,
                        historical=match.historical,
                        history_year=year,
                        method=match.method,
                        confidence=match.confidence,
                        matched=match.matched,
                    )
                )

        if args.replace:
            conn.execute(
                """
                DELETE FROM admission_group_lineage
                WHERE release_id = %s
                  AND target_year = %s
                  AND target_category = %s
                  AND target_batch = %s
                """,
                (
                    args.release_id,
                    args.target_year,
                    args.category,
                    args.batch,
                ),
            )
        with conn.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO admission_group_lineage (
                    release_id, target_year, target_category, target_batch,
                    target_university_code, target_major_group,
                    history_year, history_university_code,
                    history_major_group, mapping_method, confidence,
                    target_primary_subject, history_primary_subject,
                    target_major_names, history_major_names,
                    matched_major_names, target_major_count,
                    history_major_count, matched_major_count,
                    mapping_version
                )
                VALUES (
                    %(release_id)s, %(target_year)s, %(target_category)s,
                    %(target_batch)s, %(target_university_code)s,
                    %(target_major_group)s, %(history_year)s,
                    %(history_university_code)s, %(history_major_group)s,
                    %(mapping_method)s, %(confidence)s,
                    %(target_primary_subject)s, %(history_primary_subject)s,
                    %(target_major_names)s, %(history_major_names)s,
                    %(matched_major_names)s, %(target_major_count)s,
                    %(history_major_count)s, %(matched_major_count)s,
                    %(mapping_version)s
                )
                ON CONFLICT (
                    release_id, target_year, target_category, target_batch,
                    target_university_code, target_major_group, history_year
                ) DO UPDATE SET
                    history_university_code = EXCLUDED.history_university_code,
                    history_major_group = EXCLUDED.history_major_group,
                    mapping_method = EXCLUDED.mapping_method,
                    confidence = EXCLUDED.confidence,
                    target_primary_subject = EXCLUDED.target_primary_subject,
                    history_primary_subject = EXCLUDED.history_primary_subject,
                    target_major_names = EXCLUDED.target_major_names,
                    history_major_names = EXCLUDED.history_major_names,
                    matched_major_names = EXCLUDED.matched_major_names,
                    target_major_count = EXCLUDED.target_major_count,
                    history_major_count = EXCLUDED.history_major_count,
                    matched_major_count = EXCLUDED.matched_major_count,
                    mapping_version = EXCLUDED.mapping_version
                """,
                mappings,
            )
        conn.commit()

        mapped_years: dict[tuple[str, str], set[int]] = defaultdict(set)
        by_method = defaultdict(int)
        by_year = defaultdict(int)
        for row in mappings:
            mapped_years[
                (row["target_university_code"], row["target_major_group"])
            ].add(row["history_year"])
            by_method[row["mapping_method"]] += 1
            by_year[row["history_year"]] += 1
        full_three = sum(
            len(years) == len(history_years)
            for years in mapped_years.values()
        )
        print(
            json.dumps(
                {
                    "release_id": args.release_id,
                    "target_groups": len(targets),
                    "mapped_target_groups": len(mapped_years),
                    "full_window_groups": full_three,
                    "mappings": len(mappings),
                    "by_method": dict(sorted(by_method.items())),
                    "by_year": dict(sorted(by_year.items())),
                    "threshold": args.name_threshold,
                    "mapping_version": LINEAGE_VERSION,
                },
                ensure_ascii=False,
                indent=2,
            )
        )


def mapping_row(
    args: argparse.Namespace,
    *,
    target: GroupProfile,
    historical: GroupProfile,
    history_year: int,
    method: str,
    confidence: float,
    matched: tuple[str, ...],
) -> dict:
    common_subjects = sorted(target.subjects & historical.subjects)
    return {
        "release_id": args.release_id,
        "target_year": args.target_year,
        "target_category": args.category,
        "target_batch": args.batch,
        "target_university_code": target.university_code,
        "target_major_group": target.major_group,
        "history_year": history_year,
        "history_university_code": historical.university_code,
        "history_major_group": historical.major_group,
        "mapping_method": method,
        "confidence": round(confidence, 4),
        "target_primary_subject": " / ".join(sorted(target.subjects)),
        "history_primary_subject": " / ".join(sorted(historical.subjects)),
        "target_major_names": sorted(target.names),
        "history_major_names": sorted(historical.names),
        "matched_major_names": list(matched),
        "target_major_count": len(target.names),
        "history_major_count": len(historical.names),
        "matched_major_count": len(matched),
        "mapping_version": LINEAGE_VERSION,
    }


if __name__ == "__main__":
    main()
