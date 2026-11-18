-- name: ListScoreRanks :many
-- 读取报考年完整一分一段表，不能按批次过滤，否则全省人数分母会偏小。
-- 本科线由 Go 从本科批记录中提取；分数/位次区间仍按原规则解析。
SELECT
    score::int AS score,
    COALESCE(score_range, '') AS score_range,
    segment_count,
    cumulative_count,
    COALESCE(rank_range, '') AS rank_range,
    batch,
    COALESCE(control_score, 0)::int AS control_score
FROM score_rank_map
WHERE province = @province::text AND category = @category::text AND year = @report_year::int
ORDER BY score DESC;
