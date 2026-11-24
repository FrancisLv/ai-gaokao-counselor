-- 概率模型由“计划数值修正 + 二元命中”切换为“原始百分位 + 临界半权重”。
-- 旧正式结果正文保持不可变，通过追加 STALE 状态阻止继续作为有效报告使用。
INSERT INTO run_result_state_event (result_version, status, reason)
SELECT result.result_version, 'STALE', 'PROBABILITY_BOUNDARY_MODEL_CHANGED'
FROM run_result result
WHERE result.result_kind = 'V1_FORMAL'
  AND result.payload @? '$.report.sections[*] ? (@.probability != null)'
  AND NOT EXISTS (
      SELECT 1
      FROM run_result_state_event state
      WHERE state.result_version = result.result_version
        AND state.status = 'STALE'
  );
