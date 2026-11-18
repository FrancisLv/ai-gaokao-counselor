-- name: ListGroupHistory :many
-- 批量读取窗口内组投档线；上下界由仓储传入，不能包含报考年。
-- 总人数来自完整一分一段表，子查询不能按本科批过滤。
SELECT
    COALESCE(ma.university_code, '') AS university_code,
    COALESCE(ma.major_group, '') AS major_group,
    ma.year::int AS year,
    MIN(ma.min_rank)::int AS group_min_rank,
    srm.total_count::int AS total_count
FROM major_admission ma
JOIN (
    SELECT year, MAX(cumulative_count) AS total_count
    FROM score_rank_map
    WHERE province = @province::text AND category = @category::text
    GROUP BY year
) srm ON ma.year = srm.year
WHERE ma.province = @province::text AND ma.category = @category::text AND ma.batch = @batch::text
  AND ma.year >= @lower_year::int AND ma.year <= @upper_year::int
  AND ma.min_rank IS NOT NULL
GROUP BY ma.university_code, ma.major_group, ma.year, srm.total_count
ORDER BY ma.university_code, ma.major_group, ma.year DESC;
