-- name: HasEnrollmentPlan :one
-- 区分“整批招生计划未导入”与“有计划但没有可用候选”，只检查存在性。
SELECT EXISTS (
    SELECT 1 FROM enrollment_plan
    WHERE province = @province::text AND category = @category::text
      AND batch = @batch::text AND year = @report_year::int
);

-- name: ListGroupPlans :many
-- 一次查两年计划，在数据库按专业组与年份聚合，避免逐组查询。
-- 保持现有契约：此查询不按 batch 过滤。
SELECT
    COALESCE(university_code, '') AS university_code,
    COALESCE(major_group, '') AS major_group,
    year::int AS year,
    SUM(plan_count)::int AS plan_count
FROM enrollment_plan
WHERE province = @province::text AND category = @category::text
  AND year IN (@report_year::int, @previous_year::int)
GROUP BY university_code, major_group, year;
