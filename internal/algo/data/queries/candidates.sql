-- name: ListCandidateGroups :many
-- 用上一年的录取数据建立专业组骨架，仅保留报考年仍有招生计划的组。
-- 同一组的院校 ID/名称必须一致，且组键非空；不任取冲突记录。
WITH reliable_groups AS (
    SELECT university_code, major_group
    FROM major_admission
    WHERE province = @province::text AND category = @category::text
      AND batch = @batch::text AND year = @history_year::int
      AND NULLIF(BTRIM(university_code), '') IS NOT NULL
      AND NULLIF(BTRIM(major_group), '') IS NOT NULL
    GROUP BY university_code, major_group
    HAVING COUNT(DISTINCT university_id) = 1 AND COUNT(DISTINCT university_name) = 1
       AND COUNT(university_id) = COUNT(*)
)
SELECT
    ma.university_id,
    COALESCE(ma.university_code, '') AS university_code,
    COALESCE(ma.university_name, '') AS university_name,
    COALESCE(ma.major_group, '') AS major_group,
    MIN(ma.min_rank)::int AS group_min_rank,
    COALESCE(u.is_985, false) AS is_985,
    COALESCE(u.is_211, false) AS is_211,
    COALESCE(u.is_dual_class, false) AS is_dual_class,
    COALESCE(u.city, '') AS city,
    COALESCE(u.province, '') AS province,
    COALESCE(u.num_master, 0)::int AS num_master,
    COALESCE(u.num_doctor, 0)::int AS num_doctor,
    COALESCE(u.ruanke_rank, 0)::int AS ruanke_rank
FROM major_admission ma
JOIN reliable_groups rg USING (university_code, major_group)
JOIN university u ON ma.university_id = u.id
WHERE ma.province = @province::text AND ma.category = @category::text AND ma.batch = @batch::text
  AND ma.year = @history_year::int
  AND ma.min_rank IS NOT NULL
  AND EXISTS (
      SELECT 1 FROM enrollment_plan ep
      WHERE ep.university_code = ma.university_code AND ep.major_group = ma.major_group
        AND ep.province = @province::text AND ep.category = @category::text
        AND ep.batch = @batch::text AND ep.year = @report_year::int
  )
GROUP BY ma.university_id, ma.university_code, ma.university_name, ma.major_group,
         u.is_985, u.is_211, u.is_dual_class, u.city, u.province,
         u.num_master, u.num_doctor, u.ruanke_rank
ORDER BY ma.university_code, ma.major_group;

-- name: ListCandidateMajors :many
-- 读取组内专业明细。LEFT JOIN 保留未关联专业基础表的记录，缺失展示字段取零值。
-- 最后按专业代码、主键打破同位次并列，保持专业及选科列表顺序稳定。
SELECT
    COALESCE(ma.university_code, '') AS university_code,
    COALESCE(ma.major_group, '') AS major_group,
    COALESCE(ma.major_code, '') AS major_code,
    COALESCE(ma.major_name, '') AS major_name,
    COALESCE(ma.subject_req, '') AS subject_req,
    COALESCE(ma.major_note, '') AS major_note,
    COALESCE(ma.min_score, 0)::int AS min_score,
    COALESCE(ma.min_rank, 0)::int AS min_rank,
    COALESCE(ma.admit_count, 0)::int AS admit_count,
    COALESCE(m.duration, '') AS duration,
    COALESCE(m.tuition, 0)::float8 AS tuition
FROM major_admission ma
LEFT JOIN major m ON ma.major_id = m.id
WHERE ma.province = @province::text AND ma.category = @category::text AND ma.batch = @batch::text
  AND ma.year = @history_year::int
  AND EXISTS (
      SELECT 1 FROM enrollment_plan ep
      WHERE ep.university_code = ma.university_code AND ep.major_group = ma.major_group
        AND ep.province = @province::text AND ep.category = @category::text
        AND ep.batch = @batch::text AND ep.year = @report_year::int
  )
ORDER BY ma.university_code, ma.major_group, ma.min_rank, ma.major_code, ma.id;
