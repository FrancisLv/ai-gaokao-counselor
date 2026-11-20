-- M5a-07：release-bound Chat 结果缓存。
-- 只缓存通过治理与 schema 校验的最终内容；缓存清理不影响权威调用账本。

ALTER TABLE llm_role_route
    ADD COLUMN allow_result_cache BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE llm_logical_call
    ADD COLUMN cache_hit BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN cache_key_hash CHAR(64)
        CHECK (cache_key_hash IS NULL OR cache_key_hash ~ '^[0-9a-f]{64}$');

ALTER TABLE llm_logical_call
    ADD CONSTRAINT llm_logical_call_cache_hit_identity
    CHECK (cache_hit = FALSE OR cache_key_hash IS NOT NULL);

ALTER TABLE llm_call_log
    ADD COLUMN IF NOT EXISTS cache_hit BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS cache_key_hash CHAR(64);

CREATE TABLE llm_result_cache (
    cache_key_hash       CHAR(64) PRIMARY KEY
                         CHECK (cache_key_hash ~ '^[0-9a-f]{64}$'),
    cache_schema_version TEXT NOT NULL
                         CHECK (btrim(cache_schema_version) <> ''),
    release_id           TEXT NOT NULL REFERENCES release(release_id),
    role                 TEXT NOT NULL CHECK (btrim(role) <> ''),
    route_version        TEXT NOT NULL CHECK (btrim(route_version) <> ''),
    model_id             TEXT NOT NULL CHECK (btrim(model_id) <> ''),
    prompt_template_key  TEXT NOT NULL CHECK (btrim(prompt_template_key) <> ''),
    prompt_template_version TEXT NOT NULL
                         CHECK (btrim(prompt_template_version) <> ''),
    prompt_template_sha256 CHAR(64) NOT NULL
                         CHECK (prompt_template_sha256 ~ '^[0-9a-f]{64}$'),
    response_schema_key  TEXT,
    response_schema_version TEXT,
    response_schema_sha256 CHAR(64),
    data_classification  TEXT NOT NULL
                         CHECK (data_classification = 'PUBLIC_DATA'),
    max_output_tokens    INTEGER NOT NULL CHECK (max_output_tokens > 0),
    temperature          DOUBLE PRECISION NOT NULL CHECK (temperature = 0),
    usage_kind           TEXT NOT NULL DEFAULT 'ZERO_PROVIDER_USAGE_ON_HIT'
                         CHECK (usage_kind = 'ZERO_PROVIDER_USAGE_ON_HIT'),
    request_key_manifest JSONB NOT NULL
                         CHECK (jsonb_typeof(request_key_manifest) = 'object'),
    status               TEXT NOT NULL DEFAULT 'IN_PROGRESS'
                         CHECK (status IN (
                             'IN_PROGRESS',
                             'READY',
                             'FAILED',
                             'INVALIDATED'
                         )),
    claim_token          UUID NOT NULL,
    claim_started_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    claim_expires_at     TIMESTAMPTZ NOT NULL,
    result_payload       JSONB,
    invalidation_reason  TEXT,
    invalidated_at       TIMESTAMPTZ,
    completed_at         TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (
        (response_schema_key IS NULL
         AND response_schema_version IS NULL
         AND response_schema_sha256 IS NULL)
        OR
        (response_schema_key IS NOT NULL
         AND btrim(response_schema_key) <> ''
         AND response_schema_version IS NOT NULL
         AND btrim(response_schema_version) <> ''
         AND response_schema_sha256 ~ '^[0-9a-f]{64}$')
    ),
    CHECK (
        (status = 'READY' AND result_payload IS NOT NULL AND completed_at IS NOT NULL)
        OR
        (status <> 'READY' AND result_payload IS NULL)
    ),
    CHECK (
        (status = 'INVALIDATED'
         AND invalidation_reason IS NOT NULL
         AND invalidated_at IS NOT NULL)
        OR
        (status <> 'INVALIDATED'
         AND invalidation_reason IS NULL
         AND invalidated_at IS NULL)
    )
);

CREATE INDEX idx_llm_result_cache_release_status
    ON llm_result_cache(release_id, status);
CREATE INDEX idx_llm_result_cache_claim_expiry
    ON llm_result_cache(status, claim_expires_at);

CREATE FUNCTION invalidate_llm_result_cache_on_release_revoke()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.status = 'REVOKED'
       AND OLD.status IS DISTINCT FROM NEW.status THEN
        UPDATE llm_result_cache
        SET status = 'INVALIDATED',
            result_payload = NULL,
            invalidation_reason = 'RELEASE_REVOKED',
            invalidated_at = COALESCE(NEW.revoked_at, NOW()),
            completed_at = NULL,
            updated_at = COALESCE(NEW.revoked_at, NOW())
        WHERE release_id = NEW.release_id
          AND status IN ('IN_PROGRESS', 'READY', 'FAILED');
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_llm_result_cache_release_revoke
    AFTER UPDATE OF status ON release
    FOR EACH ROW
    EXECUTE FUNCTION invalidate_llm_result_cache_on_release_revoke();

COMMENT ON TABLE llm_result_cache IS
    '共享缓存仅面向 temperature=0、PUBLIC_DATA 且 route 明确允许的最终结果；清理不关联权威账本';
COMMENT ON COLUMN llm_result_cache.request_key_manifest IS
    '缓存键治理维度审计摘要；messages 仅保存规范化后的 SHA-256，不保存原文明文';
COMMENT ON COLUMN llm_result_cache.usage_kind IS
    '缓存命中统一返回零 provider token/cost，不复用首次调用的供应商 usage';
