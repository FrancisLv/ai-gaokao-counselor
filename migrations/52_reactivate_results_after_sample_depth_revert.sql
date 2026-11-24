-- ADR-0028 撤销“单年历史强制降档”。恢复仅被 51 号错误迁移标记且之后没有新状态的结果。
WITH latest_state AS (
    SELECT DISTINCT ON (state.result_version)
           state.result_version,
           state.status,
           state.reason
    FROM run_result_state_event AS state
    ORDER BY state.result_version, state.event_id DESC
)
INSERT INTO run_result_state_event (result_version, status, reason)
SELECT result_version, 'ACTIVE', NULL
FROM latest_state
WHERE status = 'STALE'
  AND reason = 'PROBABILITY_SAMPLE_DEPTH_CHANGED';
