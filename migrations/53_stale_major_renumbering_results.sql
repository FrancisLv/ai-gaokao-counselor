-- 专业历史匹配由“代码与名称同时一致”调整为“映射组内精确名称一致，代码作 tie-break”。
-- 旧正式结果正文保持不可变，通过追加 STALE 状态要求重新生成。
INSERT INTO run_result_state_event (result_version, status, reason)
SELECT result.result_version, 'STALE', 'MAJOR_RENUMBERING_IDENTITY_CHANGED'
FROM run_result result
WHERE result.result_kind = 'V1_FORMAL'
  AND result.payload @? '$.report.sections[*].majors[*]'
  AND NOT EXISTS (
      SELECT 1
      FROM run_result_state_event state
      WHERE state.result_version = result.result_version
        AND state.status = 'STALE'
  );
