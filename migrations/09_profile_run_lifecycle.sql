-- M2：不可变画像 revision、确认记录与 interrupt。
-- 官方 LangGraph checkpoint 表由 langgraph-checkpoint-postgres 的 setup() 创建。

CREATE TABLE release_scope (
    release_id        TEXT NOT NULL REFERENCES release(release_id) ON DELETE CASCADE,
    province          TEXT NOT NULL,
    application_year  INTEGER NOT NULL,
    category          TEXT NOT NULL,
    batch             TEXT NOT NULL,
    PRIMARY KEY (release_id, province, application_year, category, batch)
);

CREATE INDEX idx_release_scope_lookup
    ON release_scope(province, application_year, category, batch, release_id);

CREATE TABLE profile_revision (
    profile_version   UUID PRIMARY KEY,
    session_id        UUID NOT NULL REFERENCES chat_session(id) ON DELETE CASCADE,
    run_id            UUID NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
    revision_no       BIGINT NOT NULL,
    based_on          UUID REFERENCES profile_revision(profile_version),
    lifecycle         TEXT NOT NULL
                      CHECK (lifecycle IN ('DRAFT', 'CONFIRMED', 'SUPERSEDED')),
    fields            JSONB NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    confirmed_at      TIMESTAMPTZ,
    UNIQUE(session_id, revision_no)
);

CREATE INDEX idx_profile_revision_run
    ON profile_revision(run_id, revision_no DESC);

CREATE TABLE profile_confirmation (
    confirmation_id   UUID PRIMARY KEY,
    profile_version   UUID NOT NULL REFERENCES profile_revision(profile_version),
    confirmed_by      TEXT NOT NULL,
    confirmed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(profile_version)
);

CREATE TABLE run_interrupt (
    interrupt_id          UUID PRIMARY KEY,
    run_id                UUID NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
    kind                  TEXT NOT NULL
                          CHECK (kind IN ('INTAKE_ASK', 'PROFILE_CONFIRM', 'BACKFILL_ASK')),
    state                 TEXT NOT NULL
                          CHECK (state IN ('OPEN', 'CONSUMED', 'EXPIRED', 'CANCELLED')),
    question_key          TEXT NOT NULL,
    payload_version       INTEGER NOT NULL DEFAULT 1,
    payload               JSONB NOT NULL,
    answer_schema_version INTEGER NOT NULL DEFAULT 1,
    checkpoint_id         TEXT NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at            TIMESTAMPTZ NOT NULL,
    consumed_by_command_id UUID,
    answer_payload        JSONB,
    applied_at            TIMESTAMPTZ
);

CREATE UNIQUE INDEX uq_run_interrupt_one_open
    ON run_interrupt(run_id)
    WHERE state = 'OPEN';

CREATE INDEX idx_run_interrupt_run_created
    ON run_interrupt(run_id, created_at DESC);
