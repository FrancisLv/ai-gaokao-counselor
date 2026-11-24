-- name: ListCandidateGroups :many
-- 以报考年计划组为候选身份，经 release-bound lineage 映射到上一年录取组。
-- 历史专业明细不决定当年组成员。
WITH target_groups AS (
    SELECT
        MIN(university_code)::text AS university_code,
        MIN(major_group)::text AS major_group,
        MIN(university_id)::bigint AS university_id,
        MIN(university_name)::text AS university_name
    FROM enrollment_plan
    WHERE release_id = @release_id::text
      AND province = @province::text AND category = @category::text
      AND batch = @batch::text AND year = @report_year::int
      AND NULLIF(BTRIM(major_code), '') IS NOT NULL
      AND NULLIF(BTRIM(university_code), '') IS NOT NULL
      AND NULLIF(BTRIM(major_group), '') IS NOT NULL
    GROUP BY university_code, major_group
    HAVING COUNT(DISTINCT university_id) = 1 AND COUNT(DISTINCT university_name) = 1
),
mapped_history AS (
    SELECT
        lineage.target_university_code,
        lineage.target_major_group,
        admission.min_score,
        admission.min_rank,
        ROW_NUMBER() OVER (
            PARTITION BY
                lineage.target_university_code,
                lineage.target_major_group
            ORDER BY
                admission.min_score ASC,
                admission.min_rank DESC,
                admission.major_code ASC NULLS LAST,
                admission.id ASC
        ) AS major_rank
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
      AND lineage.history_year = @history_year::int
      AND admission.min_score IS NOT NULL
      AND admission.min_rank IS NOT NULL
)
SELECT
    target.university_id,
    target.university_code,
    target.university_name,
    target.major_group,
    history.min_rank::int AS group_min_rank,
    COALESCE(u.is_985, false) AS is_985,
    COALESCE(u.is_211, false) AS is_211,
    COALESCE(u.is_dual_class, false) AS is_dual_class,
    COALESCE(u.city, '') AS city,
    COALESCE(u.province, '') AS province,
    COALESCE(u.num_master, 0)::int AS num_master,
    COALESCE(u.num_doctor, 0)::int AS num_doctor,
    COALESCE(u.ruanke_rank, 0)::int AS ruanke_rank
FROM target_groups target
JOIN mapped_history history
  ON history.target_university_code = target.university_code
 AND history.target_major_group = target.major_group
 AND history.major_rank = 1
JOIN university u ON target.university_id = u.id
ORDER BY target.university_code, target.major_group;

-- name: ListCandidateMajors :many
-- 读取报考年计划中的专业明细。历史录取数据只用于回填同代码专业参考，
-- 不能决定当年组成员，否则专业代码调整会把仍招生的整组错误剔除。
SELECT
    COALESCE(ep.university_code, '') AS university_code,
    COALESCE(ep.major_group, '') AS major_group,
    COALESCE(ep.major_code, '') AS major_code,
    COALESCE(ep.major_name, '') AS major_name,
    COALESCE(ep.subject_req, '') AS subject_req,
    COALESCE(m.major_note, '') AS major_note,
    COALESCE(history.min_score, 0)::int AS min_score,
    COALESCE(history.min_rank, 0)::int AS min_rank,
    COALESCE(history.admit_count, 0)::int AS admit_count,
    COALESCE(NULLIF(ep.duration, ''), m.duration, '') AS duration,
    COALESCE(ep.tuition, m.tuition, 0)::float8 AS tuition,
    ep.plan_count::int AS plan_count,
    COALESCE(CASE
        WHEN history.id IS NOT NULL THEN @history_year::int
        ELSE NULL
    END, 0)::int AS reference_year,
    TRUE AS in_current_plan
FROM enrollment_plan ep
JOIN (
    SELECT DISTINCT
        lineage.target_university_code AS university_code,
        lineage.target_major_group AS major_group
    FROM admission_group_lineage lineage
    WHERE lineage.release_id = @release_id::text
      AND lineage.target_year = @report_year::int
      AND lineage.target_category = @category::text
      AND lineage.target_batch = @batch::text
      AND lineage.history_year = @history_year::int
) mapped_groups
  ON mapped_groups.university_code = ep.university_code
 AND mapped_groups.major_group = ep.major_group
LEFT JOIN major m ON ep.major_id = m.id
LEFT JOIN LATERAL (
    SELECT
        admission.id,
        admission.min_score,
        admission.min_rank,
        admission.admit_count
    FROM major_admission admission
    WHERE admission.release_id = @release_id::text
      AND admission.province = @province::text
      AND admission.category = @category::text
      AND admission.batch = @batch::text
      AND admission.year = @history_year::int
      AND admission.university_code = ep.university_code
      AND admission.major_group = ep.major_group
      AND admission.major_code = ep.major_code
    ORDER BY admission.id
    LIMIT 1
) history ON TRUE
WHERE ep.release_id = @release_id::text
  AND ep.province = @province::text
  AND ep.category = @category::text
  AND ep.batch = @batch::text
  AND ep.year = @report_year::int
  AND NULLIF(BTRIM(ep.major_code), '') IS NOT NULL
ORDER BY ep.university_code, ep.major_group, ep.major_code, ep.id;
