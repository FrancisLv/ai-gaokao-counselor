-- 迁移 53 使用“历史上没有 STALE”作为排除条件，漏掉了曾被迁移 52 恢复为 ACTIVE 的旧报告。
-- 本迁移按最新状态判断，使所有仍有效且包含专业结构的旧结果重新失效。
WITH latest_state AS (
    SELECT DISTINCT ON (state.result_version)
           state.result_version,
           state.status
    FROM run_result_state_event AS state
    ORDER BY state.result_version, state.event_id DESC
)
INSERT INTO run_result_state_event (result_version, status, reason)
SELECT result.result_version, 'STALE', 'MAJOR_RENUMBERING_IDENTITY_CHANGED'
FROM run_result result
JOIN latest_state ON latest_state.result_version = result.result_version
WHERE result.result_kind = 'V1_FORMAL'
  AND latest_state.status = 'ACTIVE'
  AND result.payload @? '$.report.sections[*].majors[*]';
