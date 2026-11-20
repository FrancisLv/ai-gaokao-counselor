-- M5b-06：principal 删除请求、即时访问封锁、tombstone 与资源 ledger。
-- 只新增对象；已发布 schema 与业务数据不改写。

CREATE TABLE principal_data_state (
    principal_id          TEXT PRIMARY KEY CHECK (btrim(principal_id) <> ''),
    status                TEXT NOT NULL
                          CHECK (status IN (
                              'ACTIVE',
                              'DELETION_PENDING',
                              'DELETED'
                          )),
    deletion_request_id   TEXT,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (
        (status = 'ACTIVE' AND deletion_request_id IS NULL)
        OR
        (status <> 'ACTIVE' AND btrim(deletion_request_id) <> '')
    )
);

CREATE TABLE principal_deletion_request (
    principal_ref   CHAR(64) NOT NULL
                    CHECK (principal_ref ~ '^[0-9a-f]{64}$'),
    request_id      TEXT NOT NULL CHECK (btrim(request_id) <> ''),
    request_hash    CHAR(64) NOT NULL
                    CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    status          TEXT NOT NULL
                    CHECK (status IN (
                        'DELETION_PENDING',
                        'DELETION_COMPLETED',
                        'DELETION_FAILED_RETRYABLE'
                    )),
    receipt         JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    PRIMARY KEY (principal_ref, request_id)
);

CREATE TABLE principal_deletion_ledger (
    ledger_id       BIGSERIAL PRIMARY KEY,
    principal_ref   CHAR(64) NOT NULL
                    CHECK (principal_ref ~ '^[0-9a-f]{64}$'),
    request_id      TEXT NOT NULL,
    resource_kind   TEXT NOT NULL CHECK (btrim(resource_kind) <> ''),
    result          TEXT NOT NULL
                    CHECK (result IN ('SUCCEEDED', 'FAILED')),
    affected_rows   BIGINT NOT NULL DEFAULT 0 CHECK (affected_rows >= 0),
    error_code      TEXT,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (
        (result = 'SUCCEEDED' AND error_code IS NULL)
        OR
        (result = 'FAILED' AND btrim(error_code) <> '')
    )
);

CREATE INDEX idx_principal_deletion_ledger_request
    ON principal_deletion_ledger(principal_ref, request_id, ledger_id);

CREATE TABLE principal_deletion_tombstone (
    tombstone_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    principal_ref   CHAR(64) NOT NULL UNIQUE
                    CHECK (principal_ref ~ '^[0-9a-f]{64}$'),
    request_id      TEXT NOT NULL,
    reason_code     TEXT NOT NULL CHECK (btrim(reason_code) <> ''),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ
);

-- 正式结果正文默认不可变；仅 principal 删除事务可显式开启删除通道。
CREATE OR REPLACE FUNCTION reject_run_result_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF current_setting('app.principal_deletion', true) = 'on' THEN
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
    IF current_setting('app.principal_deletion', true) = 'on'
       AND TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'formal evidence result is immutable';
END;
$$;

COMMENT ON TABLE principal_data_state IS
    'principal 删除状态；DELETION_PENDING/DELETED 必须立即封锁私有读取';
COMMENT ON TABLE principal_deletion_tombstone IS
    '最小删除 tombstone，只保留主体不可逆引用、请求、原因与时间';
COMMENT ON TABLE principal_deletion_ledger IS
    '删除资源处理 ledger，不保存被删内容、prompt 或敏感字段';
