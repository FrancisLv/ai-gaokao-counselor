-- M5a-02：预算超限后的持久封锁状态。
-- 已预占的在途调用允许完成结算；新调用必须由 ACTIVE 状态与条件预占共同拒绝。

ALTER TABLE llm_budget_scope
    ADD COLUMN budget_status TEXT NOT NULL DEFAULT 'ACTIVE'
        CHECK (budget_status IN ('ACTIVE', 'BUDGET_OVERRUN')),
    ADD COLUMN overrun_at TIMESTAMPTZ,
    ADD COLUMN overrun_reason TEXT,
    ADD CONSTRAINT llm_budget_scope_overrun_consistency
        CHECK (
            (budget_status = 'ACTIVE'
             AND overrun_at IS NULL
             AND overrun_reason IS NULL)
            OR
            (budget_status = 'BUDGET_OVERRUN'
             AND overrun_at IS NOT NULL
             AND btrim(overrun_reason) <> '')
        );

ALTER TABLE llm_logical_call
    DROP CONSTRAINT IF EXISTS llm_logical_call_status_check;

ALTER TABLE llm_logical_call
    ADD CONSTRAINT llm_logical_call_status_check
    CHECK (status IN (
        'RESERVED',
        'IN_PROGRESS',
        'SUCCEEDED',
        'FAILED',
        'PARTIAL_UNKNOWN',
        'BUDGET_OVERRUN'
    ));

COMMENT ON COLUMN llm_budget_scope.budget_status IS
    'ACTIVE 或 BUDGET_OVERRUN；超限后仅允许既有预占完成/对账，新调用不得预占';
COMMENT ON COLUMN llm_budget_scope.overrun_at IS
    '首次发现实际用量超过预占的时间；不得通过更新账本清除';
COMMENT ON COLUMN llm_logical_call.status IS
    'BUDGET_OVERRUN 表示供应商已返回但实际用量超过预留，保持预占待人工对账';
