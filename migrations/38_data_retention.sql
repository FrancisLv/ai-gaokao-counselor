-- M5b-07：版本化数据保留策略、清理审计与受控终态清理。
-- 只新增对象；历史迁移保持只读。

CREATE TABLE data_retention_policy (
    policy_version  TEXT NOT NULL CHECK (btrim(policy_version) <> ''),
    resource_kind   TEXT NOT NULL CHECK (btrim(resource_kind) <> ''),
    retention_days  INTEGER NOT NULL CHECK (retention_days >= 0),
    action          TEXT NOT NULL
                    CHECK (action IN ('DELETE', 'DEIDENTIFY')),
    effective_at    TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (policy_version, resource_kind)
);

CREATE TABLE data_retention_sweep (
    sweep_id        UUID PRIMARY KEY,
    policy_version  TEXT NOT NULL,
    status          TEXT NOT NULL
                    CHECK (status IN (
                        'RUNNING',
                        'COMPLETED',
                        'RETENTION_FAILED_RETRYABLE'
                    )),
    started_at      TIMESTAMPTZ NOT NULL,
    completed_at    TIMESTAMPTZ,
    error_code      TEXT,
    CHECK (
        (status = 'RUNNING' AND completed_at IS NULL AND error_code IS NULL)
        OR
        (status = 'COMPLETED' AND completed_at IS NOT NULL AND error_code IS NULL)
        OR
        (status = 'RETENTION_FAILED_RETRYABLE'
         AND completed_at IS NOT NULL
         AND btrim(error_code) <> '')
    )
);

CREATE TABLE data_retention_ledger (
    ledger_id       BIGSERIAL PRIMARY KEY,
    sweep_id        UUID NOT NULL REFERENCES data_retention_sweep(sweep_id),
    resource_kind   TEXT NOT NULL,
    action          TEXT NOT NULL CHECK (action IN ('DELETE', 'DEIDENTIFY')),
    result          TEXT NOT NULL CHECK (result IN ('SUCCEEDED', 'FAILED')),
    affected_rows   BIGINT NOT NULL DEFAULT 0 CHECK (affected_rows >= 0),
    error_code      TEXT,
    occurred_at     TIMESTAMPTZ NOT NULL,
    CHECK (
        (result = 'SUCCEEDED' AND error_code IS NULL)
        OR
        (result = 'FAILED' AND btrim(error_code) <> '')
    )
);

CREATE INDEX idx_data_retention_ledger_sweep
    ON data_retention_ledger(sweep_id, ledger_id);

INSERT INTO data_retention_policy (
    policy_version, resource_kind, retention_days, action, effective_at
)
VALUES
    ('m5b-retention-v1', 'FINAL_RESULTS', 30, 'DELETE', NOW() - INTERVAL '1 day'),
    ('m5b-retention-v1', 'SUCCESSFUL_CHECKPOINTS', 30, 'DELETE', NOW() - INTERVAL '1 day'),
    ('m5b-retention-v1', 'PROFILE', 30, 'DELETE', NOW() - INTERVAL '1 day'),
    ('m5b-retention-v1', 'RUN_METADATA', 30, 'DEIDENTIFY', NOW() - INTERVAL '1 day'),
    ('m5b-retention-v1', 'RUN_EVENTS', 7, 'DELETE', NOW() - INTERVAL '1 day'),
    ('m5b-retention-v1', 'FAILED_CHECKPOINTS', 7, 'DELETE', NOW() - INTERVAL '1 day'),
    ('m5b-retention-v1', 'COMMAND_RECEIPTS', 7, 'DELETE', NOW() - INTERVAL '1 day'),
    ('m5b-retention-v1', 'PROVIDER_LEDGER', 180, 'DEIDENTIFY', NOW() - INTERVAL '1 day'),
    ('m5b-retention-v1', 'SECURITY_AUDIT', 180, 'DEIDENTIFY', NOW() - INTERVAL '1 day');

-- 终态数据只能由明确的数据生命周期事务清理；普通业务写入仍不可改写正式结果。
CREATE OR REPLACE FUNCTION reject_run_result_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF current_setting('app.principal_deletion', true) = 'on'
       OR current_setting('app.retention_cleanup', true) = 'on' THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        IF OLD.result_kind = 'V1_FORMAL' THEN
            RAISE EXCEPTION 'run_result is immutable';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.result_kind = 'V1_FORMAL'
       OR NEW.result_kind = 'V1_FORMAL' THEN
        RAISE EXCEPTION 'run_result is immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION reject_immutable_formal_result()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE'
       AND (
           current_setting('app.principal_deletion', true) = 'on'
           OR current_setting('app.retention_cleanup', true) = 'on'
       ) THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'formal evidence result is immutable';
END;
$$;

COMMENT ON TABLE data_retention_policy IS
    '版本化保留策略；清理必须引用同一 policy_version，缺失策略 fail closed';
COMMENT ON TABLE data_retention_sweep IS
    '一次保留清理的非敏感审计身份、状态与错误码';
COMMENT ON TABLE data_retention_ledger IS
    '逐资源清理结果与行数；不保存被清理内容或敏感字段';
