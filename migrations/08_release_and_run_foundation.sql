-- M2 可靠任务底座：不可变 release、持久 run、命令回执与事件。

ALTER TABLE chat_session ADD COLUMN IF NOT EXISTS principal_id TEXT;
CREATE INDEX IF NOT EXISTS idx_chat_session_principal ON chat_session(principal_id);

CREATE TABLE release (
    release_id       TEXT PRIMARY KEY,
    status           TEXT NOT NULL
                     CHECK (status IN ('STAGING', 'PUBLISHED', 'REVOKED')),
    manifest         JSONB NOT NULL,
    manifest_sha256  TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at     TIMESTAMPTZ,
    revoked_at       TIMESTAMPTZ
);

CREATE INDEX idx_release_status_published
    ON release(status, published_at DESC);

CREATE TABLE run (
    run_id                     UUID PRIMARY KEY,
    session_id                 UUID NOT NULL REFERENCES chat_session(id) ON DELETE CASCADE,
    principal_id               TEXT NOT NULL,
    parent_result_version      UUID,
    task_kind                  TEXT NOT NULL
                               CHECK (task_kind IN (
                                   'GENERATE_PLAN',
                                   'POLICY_QA',
                                   'EXPLAIN_RESULT',
                                   'REVISE_PLAN'
                               )),
    task_input                 JSONB NOT NULL,
    state                      TEXT NOT NULL
                               CHECK (state IN (
                                   'QUEUED',
                                   'RUNNING',
                                   'WAITING_HITL',
                                   'EXPIRED',
                                   'SUCCEEDED',
                                   'FAILED',
                                   'CANCELLED'
                               )),
    stage                      TEXT NOT NULL,
    terminal_reason            TEXT,
    state_version              BIGINT NOT NULL DEFAULT 1,
    release_id                 TEXT NOT NULL REFERENCES release(release_id),
    working_profile_version    UUID,
    confirmed_profile_version  UUID,
    result_version             UUID,
    result_kind                TEXT,
    cancel_requested_at        TIMESTAMPTZ,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_run_principal_created
    ON run(principal_id, created_at DESC);
CREATE INDEX idx_run_session_created
    ON run(session_id, created_at DESC);

CREATE TABLE run_command_receipt (
    receipt_id       UUID PRIMARY KEY,
    principal_id     TEXT NOT NULL,
    operation        TEXT NOT NULL,
    scope_key        TEXT NOT NULL,
    idempotency_key  TEXT NOT NULL,
    request_hash     TEXT NOT NULL,
    response         JSONB NOT NULL,
    target_run_id    UUID REFERENCES run(run_id) ON DELETE CASCADE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at       TIMESTAMPTZ NOT NULL,
    UNIQUE(principal_id, operation, scope_key, idempotency_key)
);

CREATE INDEX idx_run_command_receipt_expiry
    ON run_command_receipt(expires_at);

CREATE TABLE run_event (
    event_id         BIGSERIAL PRIMARY KEY,
    run_id           UUID NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
    event_seq        BIGINT NOT NULL,
    event_type       TEXT NOT NULL
                     CHECK (event_type IN (
                         'STATE_CHANGED',
                         'PROGRESS',
                         'INTERRUPT',
                         'RESULT',
                         'ERROR'
                     )),
    stage            TEXT NOT NULL,
    state_version    BIGINT NOT NULL,
    occurred_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    payload_version  INTEGER NOT NULL DEFAULT 1,
    payload          JSONB NOT NULL,
    UNIQUE(run_id, event_seq)
);

CREATE INDEX idx_run_event_run_seq ON run_event(run_id, event_seq);
CREATE INDEX idx_run_event_occurred ON run_event(occurred_at);

