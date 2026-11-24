-- 用户拟填专业草稿：独立于不可变正式结果，按版本保存以便恢复与冲突检测。

CREATE TABLE wishlist_draft_version (
    draft_version_id  UUID PRIMARY KEY,
    principal_id      TEXT NOT NULL CHECK (btrim(principal_id) <> ''),
    session_id        UUID NOT NULL REFERENCES chat_session(id) ON DELETE CASCADE,
    run_id            UUID NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
    result_version    UUID NOT NULL REFERENCES run_result(result_version) ON DELETE CASCADE,
    release_id        TEXT NOT NULL REFERENCES release(release_id),
    draft_version     BIGINT NOT NULL CHECK (draft_version > 0),
    selections        JSONB NOT NULL
                      CHECK (jsonb_typeof(selections) = 'array'),
    idempotency_key   TEXT NOT NULL CHECK (btrim(idempotency_key) <> ''),
    request_sha256    TEXT NOT NULL CHECK (length(request_sha256) = 64),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (principal_id, result_version, draft_version),
    UNIQUE (principal_id, result_version, idempotency_key)
);

CREATE INDEX idx_wishlist_draft_version_latest
    ON wishlist_draft_version(principal_id, result_version, draft_version DESC);

CREATE OR REPLACE FUNCTION reject_wishlist_draft_version_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'wishlist_draft_version is immutable; insert a new version';
END;
$$;

CREATE TRIGGER trg_wishlist_draft_version_immutable_update
BEFORE UPDATE ON wishlist_draft_version
FOR EACH ROW
EXECUTE FUNCTION reject_wishlist_draft_version_mutation();

COMMENT ON TABLE wishlist_draft_version IS
    '基于正式结果的不可变志愿草稿版本；不改变 run_result、概率或证据包';
