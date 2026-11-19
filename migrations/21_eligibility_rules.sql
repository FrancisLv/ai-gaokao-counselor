-- M4b: 结构化资格规则的不可变版本与 release-scoped 覆盖声明。

CREATE TABLE rule_version (
    rule_id                 TEXT NOT NULL,
    version                 INTEGER NOT NULL CHECK (version > 0),
    rule_key                TEXT NOT NULL CHECK (btrim(rule_key) <> ''),
    rule_type               TEXT NOT NULL CHECK (rule_type IN (
        'SUBJECT_REQUIREMENT',
        'SINGLE_SUBJECT_MIN',
        'PHYSICAL_RESTRICTION',
        'GENDER_RESTRICTION',
        'BATCH_RESTRICTION',
        'TUITION_LIMIT',
        'OTHER_HARD'
    )),
    scope                   JSONB NOT NULL DEFAULT '{}'::jsonb
                            CHECK (jsonb_typeof(scope) = 'object'),
    required_profile_fields JSONB NOT NULL DEFAULT '[]'::jsonb
                            CHECK (jsonb_typeof(required_profile_fields) = 'array'),
    expression              JSONB NOT NULL
                            CHECK (jsonb_typeof(expression) = 'object'),
    outcome_when_true       TEXT NOT NULL DEFAULT 'FAIL'
                            CHECK (outcome_when_true = 'FAIL'),
    reason_code             TEXT NOT NULL CHECK (btrim(reason_code) <> ''),
    review_status           TEXT NOT NULL CHECK (review_status IN (
        'DRAFT',
        'REVIEWED',
        'PUBLISHED',
        'REVOKED'
    )),
    valid_from              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    valid_to                TIMESTAMPTZ,
    release_id              TEXT NOT NULL REFERENCES release(release_id) ON DELETE CASCADE,
    source_evidence         JSONB NOT NULL
                            CHECK (
                                jsonb_typeof(source_evidence) = 'array'
                                AND jsonb_array_length(source_evidence) > 0
                            ),
    reviewed_by             TEXT,
    reviewed_at             TIMESTAMPTZ,
    revoked_by              TEXT,
    revoked_at              TIMESTAMPTZ,
    revoke_reason            TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (rule_id, version),
    CHECK (valid_to IS NULL OR valid_to > valid_from),
    CHECK (
        review_status <> 'REVIEWED'
        OR (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)
    ),
    CHECK (
        review_status <> 'REVOKED'
        OR (revoked_by IS NOT NULL AND revoked_at IS NOT NULL AND btrim(revoke_reason) <> '')
    )
);

CREATE INDEX idx_rule_version_release_published
    ON rule_version(release_id, review_status, rule_key);
CREATE INDEX idx_rule_version_scope
    ON rule_version USING GIN(scope);
CREATE INDEX idx_rule_version_required_fields
    ON rule_version USING GIN(required_profile_fields);

COMMENT ON TABLE rule_version IS
    '不可变规则内容版本；修改逻辑必须新增 rule_id/version，仅生命周期元数据可变';

CREATE TABLE rule_coverage (
    release_id            TEXT NOT NULL REFERENCES release(release_id) ON DELETE CASCADE,
    rule_type             TEXT NOT NULL CHECK (rule_type IN (
        'SUBJECT_REQUIREMENT',
        'SINGLE_SUBJECT_MIN',
        'PHYSICAL_RESTRICTION',
        'GENDER_RESTRICTION',
        'BATCH_RESTRICTION',
        'TUITION_LIMIT',
        'OTHER_HARD'
    )),
    scope                 JSONB NOT NULL DEFAULT '{}'::jsonb
                          CHECK (jsonb_typeof(scope) = 'object'),
    status                TEXT NOT NULL CHECK (status IN ('FULL', 'PARTIAL', 'NONE')),
    covered_data_sources  JSONB NOT NULL DEFAULT '[]'::jsonb
                          CHECK (jsonb_typeof(covered_data_sources) = 'array'),
    notes                 TEXT NOT NULL DEFAULT '',
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (release_id, rule_type, scope),
    CHECK (
        status <> 'FULL'
        OR (
            jsonb_array_length(covered_data_sources) > 0
            AND btrim(notes) <> ''
        )
    ),
    CHECK (
        status = 'FULL'
        OR btrim(notes) <> ''
    )
);

CREATE INDEX idx_rule_coverage_release_type
    ON rule_coverage(release_id, rule_type);
CREATE INDEX idx_rule_coverage_scope
    ON rule_coverage USING GIN(scope);

COMMENT ON TABLE rule_coverage IS
    'release 内按规则类型/适用域声明的 FULL/PARTIAL/NONE 覆盖；无规则不等于无限制';
