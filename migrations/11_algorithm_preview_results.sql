-- M3：不可变 ALGORITHM_PREVIEW 结果。
-- wire 契约使用 typed proto；数据库仅保存版本化 payload 与门禁摘要。

CREATE TABLE run_result (
    result_version            UUID PRIMARY KEY,
    run_id                    UUID NOT NULL UNIQUE
                              REFERENCES run(run_id) ON DELETE CASCADE,
    release_id                TEXT NOT NULL REFERENCES release(release_id),
    confirmed_profile_version UUID NOT NULL
                              REFERENCES profile_revision(profile_version),
    result_kind               TEXT NOT NULL
                              CHECK (result_kind = 'ALGORITHM_PREVIEW'),
    payload                   JSONB NOT NULL,
    verification_summary      JSONB NOT NULL,
    unverified_conditions     JSONB NOT NULL,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_run_result_release_created
    ON run_result(release_id, created_at DESC);
