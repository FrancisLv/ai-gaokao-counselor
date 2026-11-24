-- 专业级概率历史匹配由“仅同代码”修正为“同代码 + 严格同名称”。
-- 旧正式结果正文保持不可变，通过追加 STALE 状态阻止继续作为有效报告使用。
INSERT INTO run_result_state_event (result_version, status, reason)
SELECT result.result_version, 'STALE', 'MAJOR_IDENTITY_CORRECTION'
FROM run_result result
WHERE result.result_kind = 'V1_FORMAL'
  AND result.payload @? '$.report.sections[*].majors[*] ? (@.admission_probability != null)'
  AND NOT EXISTS (
      SELECT 1
      FROM run_result_state_event state
      WHERE state.result_version = result.result_version
        AND state.status = 'STALE'
  );
