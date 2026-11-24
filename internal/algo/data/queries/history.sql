-- name: ListGroupHistory :many
-- 通过 release-bound lineage 批量读取目标组的历史录取参考；
-- 每个历史年份只取该映射组中最低录取分及其同条记录位次。
WITH mapped_history AS (
    SELECT
        lineage.target_university_code,
        lineage.target_major_group,
        lineage.history_year AS year,
        lineage.mapping_method,
        lineage.confidence AS mapping_confidence,
        admission.min_score,
        admission.min_rank,
        admission.major_code,
        admission.id,
        ROW_NUMBER() OVER (
            PARTITION BY
                lineage.target_university_code,
                lineage.target_major_group,
                lineage.history_year
            ORDER BY
                admission.min_score ASC,
                admission.min_rank DESC,
                admission.major_code ASC NULLS LAST,
                admission.id ASC
        ) AS history_rank
    FROM admission_group_lineage lineage
    JOIN major_admission admission
      ON admission.release_id = lineage.release_id
     AND admission.province = @province::text
     AND admission.category = lineage.target_category
     AND admission.batch = lineage.target_batch
     AND admission.year = lineage.history_year
     AND admission.university_code = lineage.history_university_code
     AND admission.major_group = lineage.history_major_group
    WHERE lineage.release_id = @release_id::text
      AND lineage.target_year = @report_year::int
      AND lineage.target_category = @category::text
      AND lineage.target_batch = @batch::text
      AND lineage.history_year >= @lower_year::int
      AND lineage.history_year <= @upper_year::int
      AND admission.min_rank IS NOT NULL
      AND admission.min_score IS NOT NULL
)
SELECT
    mapped.target_university_code AS university_code,
    mapped.target_major_group AS major_group,
    mapped.year::int AS year,
    mapped.min_score::int AS group_min_score,
    mapped.min_rank::int AS group_min_rank,
    mapped.mapping_method,
    mapped.mapping_confidence::float8 AS mapping_confidence,
    srm.total_count::int AS total_count
FROM mapped_history mapped
JOIN (
    SELECT year, MAX(cumulative_count) AS total_count
    FROM score_rank_map
    WHERE release_id = @release_id::text
      AND province = @province::text AND category = @category::text
    GROUP BY year
) srm ON mapped.year = srm.year
WHERE mapped.history_rank = 1
ORDER BY
    mapped.target_university_code,
    mapped.target_major_group,
    mapped.year DESC;

-- name: ListMajorHistory :many
-- 专业级概率只在 release 映射后的历史组内按“同专业代码 + 同专业名称”匹配；
-- 同代码不同名称、代码变化或其他断档保留为 UNKNOWN。
WITH current_majors AS (
    SELECT DISTINCT
        enrollment_plan.university_code AS target_university_code,
        enrollment_plan.major_group AS target_major_group,
        enrollment_plan.major_code AS target_major_code,
        BTRIM(enrollment_plan.major_name) AS target_major_name
    FROM enrollment_plan
    JOIN (
        SELECT DISTINCT
            lineage.target_university_code AS university_code,
            lineage.target_major_group AS major_group
        FROM admission_group_lineage lineage
        WHERE lineage.release_id = @release_id::text
          AND lineage.target_year = @report_year::int
          AND lineage.target_category = @category::text
          AND lineage.target_batch = @batch::text
          AND lineage.history_year = @upper_year::int
    ) mapped_groups
      ON mapped_groups.university_code = enrollment_plan.university_code
     AND mapped_groups.major_group = enrollment_plan.major_group
    WHERE enrollment_plan.release_id = @release_id::text
      AND enrollment_plan.province = @province::text
      AND enrollment_plan.category = @category::text
      AND enrollment_plan.batch = @batch::text
      AND enrollment_plan.year = @report_year::int
      AND NULLIF(BTRIM(enrollment_plan.university_code), '') IS NOT NULL
      AND NULLIF(BTRIM(enrollment_plan.major_group), '') IS NOT NULL
      AND NULLIF(BTRIM(enrollment_plan.major_code), '') IS NOT NULL
      AND NULLIF(BTRIM(enrollment_plan.major_name), '') IS NOT NULL
),
mapped_history AS (
    SELECT
        current_majors.target_university_code,
        current_majors.target_major_group,
        current_majors.target_major_code,
        current_majors.target_major_name,
        lineage.history_year AS year,
        admission.major_name,
        admission.min_score,
        admission.min_rank,
        admission.admit_count,
        admission.id,
        ROW_NUMBER() OVER (
            PARTITION BY
                current_majors.target_university_code,
                current_majors.target_major_group,
                current_majors.target_major_code,
                lineage.history_year
            ORDER BY
                (admission.major_code = current_majors.target_major_code) DESC,
                admission.id
        ) AS history_rank
    FROM current_majors
    JOIN admission_group_lineage lineage
      ON lineage.release_id = @release_id::text
     AND lineage.target_year = @report_year::int
     AND lineage.target_category = @category::text
     AND lineage.target_batch = @batch::text
     AND lineage.target_university_code =
         current_majors.target_university_code
     AND lineage.target_major_group =
         current_majors.target_major_group
     AND lineage.history_year >= @lower_year::int
     AND lineage.history_year <= @upper_year::int
    JOIN major_admission admission
      ON admission.release_id = lineage.release_id
     AND admission.province = @province::text
     AND admission.category = lineage.target_category
     AND admission.batch = lineage.target_batch
     AND admission.year = lineage.history_year
     AND admission.university_code = lineage.history_university_code
     AND admission.major_group = lineage.history_major_group
     AND BTRIM(admission.major_name) = current_majors.target_major_name
    WHERE admission.min_score IS NOT NULL
      AND admission.min_rank IS NOT NULL
)
SELECT
    COALESCE(mapped.target_university_code, '') AS university_code,
    COALESCE(mapped.target_major_group, '') AS major_group,
    COALESCE(mapped.target_major_code, '') AS major_code,
    COALESCE(BTRIM(mapped.major_name::text), '')::text AS major_name,
    mapped.year::int AS year,
    mapped.min_score::int AS min_score,
    mapped.min_rank::int AS min_rank,
    COALESCE(mapped.admit_count, 0)::int AS admit_count,
    srm.total_count::int AS total_count
FROM mapped_history mapped
JOIN (
    SELECT year, MAX(cumulative_count) AS total_count
    FROM score_rank_map
    WHERE release_id = @release_id::text
      AND province = @province::text
      AND category = @category::text
    GROUP BY year
) srm ON mapped.year = srm.year
WHERE mapped.history_rank = 1
ORDER BY
    mapped.target_university_code,
    mapped.target_major_group,
    mapped.target_major_code,
    mapped.year DESC;
