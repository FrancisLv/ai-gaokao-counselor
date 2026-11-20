-- M5b-08：备份恢复期间关闭业务，并在开放前重放 principal 删除。
-- 只新增恢复闸门与重放审计，不改写已发布 schema。

CREATE TABLE data_lifecycle_restore_state (
    restore_key       TEXT PRIMARY KEY CHECK (restore_key = 'SERVICE'),
    status            TEXT NOT NULL
                      CHECK (status IN ('RESTORING', 'OPEN')),
    restore_id        TEXT,
    error_code        TEXT,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (
        (status = 'OPEN')
        OR
        (status = 'RESTORING' AND btrim(restore_id) <> '')
    )
);

CREATE TABLE data_lifecycle_restore_replay (
    restore_id      TEXT NOT NULL CHECK (btrim(restore_id) <> ''),
    principal_ref   CHAR(64) NOT NULL
                    CHECK (principal_ref ~ '^[0-9a-f]{64}$'),
    request_id      TEXT NOT NULL CHECK (btrim(request_id) <> ''),
    result          TEXT NOT NULL CHECK (result IN ('SUCCEEDED', 'FAILED')),
    error_code      TEXT,
    replayed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (restore_id, principal_ref),
    CHECK (
        (result = 'SUCCEEDED' AND error_code IS NULL)
        OR
        (result = 'FAILED' AND btrim(error_code) <> '')
    )
);

COMMENT ON TABLE data_lifecycle_restore_state IS
    '备份恢复全局闸门；RESTORING 时所有私有业务读取必须关闭';
COMMENT ON TABLE data_lifecycle_restore_replay IS
    '删除 tombstone/ledger 恢复重放审计，不保存被删内容或敏感原文';
