-- 受控邀请账号、登录会话、恢复码与用户自助账户数据。

CREATE TABLE user_account (
    account_id              UUID PRIMARY KEY,
    username                TEXT NOT NULL CHECK (btrim(username) <> ''),
    username_normalized     TEXT NOT NULL UNIQUE CHECK (btrim(username_normalized) <> ''),
    nickname                TEXT NOT NULL CHECK (btrim(nickname) <> ''),
    password_hash           TEXT NOT NULL CHECK (btrim(password_hash) <> ''),
    status                  TEXT NOT NULL DEFAULT 'ACTIVE'
                            CHECK (status IN ('ACTIVE', 'DELETION_PENDING', 'DELETED')),
    privacy_policy_version  TEXT NOT NULL CHECK (btrim(privacy_policy_version) <> ''),
    guardian_ack_recorded   BOOLEAN NOT NULL,
    privacy_accepted_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login_at           TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_user_account_last_login
    ON user_account(status, last_login_at);

CREATE TABLE account_consent (
    consent_id             UUID PRIMARY KEY,
    account_id             UUID NOT NULL REFERENCES user_account(account_id) ON DELETE CASCADE,
    purpose                TEXT NOT NULL CHECK (purpose IN ('GENERAL_PRIVACY')),
    policy_version         TEXT NOT NULL CHECK (btrim(policy_version) <> ''),
    guardian_ack_recorded  BOOLEAN NOT NULL,
    granted_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(account_id, purpose, policy_version)
);

CREATE TABLE invite_code (
    code_hash              CHAR(64) PRIMARY KEY CHECK (code_hash ~ '^[0-9a-f]{64}$'),
    status                 TEXT NOT NULL DEFAULT 'AVAILABLE'
                           CHECK (status IN ('AVAILABLE', 'CONSUMED', 'REVOKED')),
    expires_at             TIMESTAMPTZ NOT NULL,
    consumed_by_account_id UUID REFERENCES user_account(account_id),
    consumed_at            TIMESTAMPTZ,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (
        (status = 'AVAILABLE' AND consumed_by_account_id IS NULL AND consumed_at IS NULL)
        OR
        (status = 'CONSUMED' AND consumed_by_account_id IS NOT NULL AND consumed_at IS NOT NULL)
        OR
        (status = 'REVOKED')
    )
);

CREATE INDEX idx_invite_code_expiry
    ON invite_code(status, expires_at);

CREATE TABLE recovery_code (
    recovery_code_id UUID PRIMARY KEY,
    account_id       UUID NOT NULL REFERENCES user_account(account_id) ON DELETE CASCADE,
    code_hash        CHAR(64) NOT NULL CHECK (code_hash ~ '^[0-9a-f]{64}$'),
    consumed_at      TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(account_id, code_hash)
);

CREATE INDEX idx_recovery_code_active
    ON recovery_code(account_id, consumed_at);

CREATE TABLE login_session (
    login_session_id UUID PRIMARY KEY,
    account_id       UUID NOT NULL REFERENCES user_account(account_id) ON DELETE CASCADE,
    token_hash       CHAR(64) NOT NULL UNIQUE CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    user_agent_summary TEXT NOT NULL DEFAULT '',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    idle_expires_at  TIMESTAMPTZ NOT NULL,
    absolute_expires_at TIMESTAMPTZ NOT NULL,
    revoked_at       TIMESTAMPTZ,
    CHECK (absolute_expires_at > created_at),
    CHECK (idle_expires_at <= absolute_expires_at)
);

CREATE INDEX idx_login_session_account_active
    ON login_session(account_id, revoked_at, absolute_expires_at);

CREATE TABLE reserved_username (
    username_hash CHAR(64) PRIMARY KEY CHECK (username_hash ~ '^[0-9a-f]{64}$'),
    reserved_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE user_account IS
    '受控邀请用户账号；不含手机号、邮箱、真实姓名等直接联系方式';
COMMENT ON TABLE login_session IS
    '服务端可撤销登录会话；只保存 token 哈希，不持久化来源 IP';
COMMENT ON TABLE reserved_username IS
    '账号删除后永久保留的规范化用户名摘要，禁止复用';
