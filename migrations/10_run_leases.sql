-- Ticket #4：worker 执行租约、fencing epoch 与主动执行时间。

ALTER TABLE run
    ADD COLUMN IF NOT EXISTS lease_owner TEXT,
    ADD COLUMN IF NOT EXISTS lease_epoch BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS active_execution_remaining_ms BIGINT NOT NULL DEFAULT 600000,
    ADD COLUMN IF NOT EXISTS active_execution_started_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_run_execution_lease
    ON run(state, lease_expires_at, created_at)
    WHERE state IN ('QUEUED', 'RUNNING');

CREATE INDEX IF NOT EXISTS idx_run_hitl_expiry
    ON run(state, stage, updated_at)
    WHERE state = 'WAITING_HITL';
