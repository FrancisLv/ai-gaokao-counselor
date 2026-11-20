-- M5b：敏感资格数据使用前必须存在按 principal、目的和数据类别绑定的同意记录。

CREATE TABLE consent_record (
    consent_id             UUID PRIMARY KEY,
    principal_id           TEXT NOT NULL CHECK (btrim(principal_id) <> ''),
    purpose                TEXT NOT NULL CHECK (purpose IN (
        'PLAN_GENERATION',
        'SENSITIVE_ELIGIBILITY_EVALUATION'
    )),
    data_classes           TEXT[] NOT NULL
                           CHECK (cardinality(data_classes) > 0)
                           CHECK (array_position(data_classes, NULL) IS NULL)
                           CHECK (array_position(data_classes, '') IS NULL),
    policy_version         TEXT NOT NULL CHECK (btrim(policy_version) <> ''),
    granted_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at             TIMESTAMPTZ,
    guardian_ack_recorded  BOOLEAN NOT NULL DEFAULT FALSE,
    CHECK (revoked_at IS NULL OR revoked_at >= granted_at)
);

CREATE INDEX idx_consent_record_principal_purpose
    ON consent_record(principal_id, purpose, granted_at DESC);

COMMENT ON TABLE consent_record IS
    'principal 同意记录；敏感资格数据只可由当前 policy 版本和匹配目的/数据类别的有效记录放行';
