-- M5a-01：类型化 Chat 的 release-bound 路由、价格、预算与调用账本。
-- 只增对象；历史迁移和 llm_call_log 兼容投影保持不变。

CREATE TABLE llm_price_table (
    price_table_version TEXT PRIMARY KEY
                        CHECK (btrim(price_table_version) <> ''),
    currency_unit       TEXT NOT NULL DEFAULT 'USD_MICRO'
                        CHECK (currency_unit = 'USD_MICRO'),
    effective_at        TIMESTAMPTZ NOT NULL,
    exchange_rate_version TEXT,
    published_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (exchange_rate_version IS NULL OR btrim(exchange_rate_version) <> '')
);

CREATE INDEX idx_llm_price_table_effective
    ON llm_price_table(effective_at, published_at);

CREATE TABLE llm_price_entry (
    price_table_version TEXT NOT NULL
                        REFERENCES llm_price_table(price_table_version),
    model_id            TEXT NOT NULL CHECK (btrim(model_id) <> ''),
    input_micro_usd_per_million_tokens BIGINT NOT NULL
                                      CHECK (input_micro_usd_per_million_tokens >= 0),
    output_micro_usd_per_million_tokens BIGINT NOT NULL
                                       CHECK (output_micro_usd_per_million_tokens >= 0),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (price_table_version, model_id)
);

CREATE TABLE llm_model_allowlist (
    release_id   TEXT NOT NULL REFERENCES release(release_id),
    model_id     TEXT NOT NULL CHECK (btrim(model_id) <> ''),
    provider     TEXT NOT NULL CHECK (btrim(provider) <> ''),
    enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (release_id, model_id)
);

CREATE TABLE llm_role_route (
    release_id          TEXT NOT NULL REFERENCES release(release_id),
    role                TEXT NOT NULL CHECK (btrim(role) <> ''),
    route_version       TEXT NOT NULL CHECK (btrim(route_version) <> ''),
    primary_model_id    TEXT NOT NULL CHECK (btrim(primary_model_id) <> ''),
    fallback_model_ids  TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    max_output_tokens   INTEGER NOT NULL CHECK (max_output_tokens > 0),
    timeout_ms          INTEGER NOT NULL CHECK (timeout_ms > 0),
    max_attempts        INTEGER NOT NULL DEFAULT 2
                        CHECK (max_attempts BETWEEN 1 AND 4),
    price_table_version TEXT NOT NULL
                        REFERENCES llm_price_table(price_table_version),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (release_id, role),
    CHECK (array_position(fallback_model_ids, NULL) IS NULL)
);

CREATE TABLE llm_budget_scope (
    budget_scope_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    principal_id               TEXT NOT NULL CHECK (btrim(principal_id) <> ''),
    session_id                 UUID NOT NULL REFERENCES chat_session(id),
    currency_unit              TEXT NOT NULL DEFAULT 'USD_MICRO'
                               CHECK (currency_unit = 'USD_MICRO'),
    token_limit                BIGINT NOT NULL DEFAULT 50000 CHECK (token_limit > 0),
    cost_limit_micro_usd       BIGINT NOT NULL DEFAULT 150000
                               CHECK (cost_limit_micro_usd > 0),
    provider_tokens_settled    BIGINT NOT NULL DEFAULT 0
                               CHECK (provider_tokens_settled >= 0),
    provider_tokens_reserved   BIGINT NOT NULL DEFAULT 0
                               CHECK (provider_tokens_reserved >= 0),
    cost_settled_micro_usd     BIGINT NOT NULL DEFAULT 0
                               CHECK (cost_settled_micro_usd >= 0),
    cost_reserved_micro_usd    BIGINT NOT NULL DEFAULT 0
                               CHECK (cost_reserved_micro_usd >= 0),
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (principal_id, session_id),
    CHECK (provider_tokens_settled + provider_tokens_reserved <= token_limit),
    CHECK (cost_settled_micro_usd + cost_reserved_micro_usd <= cost_limit_micro_usd)
);

CREATE TABLE llm_logical_call (
    logical_call_pk            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ledger_entry_id            UUID NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    logical_call_id            TEXT NOT NULL CHECK (btrim(logical_call_id) <> ''),
    principal_id               TEXT NOT NULL CHECK (btrim(principal_id) <> ''),
    session_id                 UUID NOT NULL REFERENCES chat_session(id),
    run_id                     UUID NOT NULL REFERENCES run(run_id),
    role                       TEXT NOT NULL CHECK (btrim(role) <> ''),
    release_id                 TEXT NOT NULL REFERENCES release(release_id),
    route_version              TEXT NOT NULL CHECK (btrim(route_version) <> ''),
    price_table_version        TEXT NOT NULL
                               REFERENCES llm_price_table(price_table_version),
    model_id                   TEXT NOT NULL CHECK (btrim(model_id) <> ''),
    request_hash               CHAR(64) NOT NULL
                               CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    budget_scope_id            UUID NOT NULL
                               REFERENCES llm_budget_scope(budget_scope_id),
    status                     TEXT NOT NULL DEFAULT 'RESERVED'
                               CHECK (status IN (
                                   'RESERVED',
                                   'IN_PROGRESS',
                                   'SUCCEEDED',
                                   'FAILED',
                                   'PARTIAL_UNKNOWN'
                               )),
    reserved_input_tokens      BIGINT NOT NULL CHECK (reserved_input_tokens >= 0),
    reserved_output_tokens     BIGINT NOT NULL CHECK (reserved_output_tokens >= 0),
    reserved_cost_micro_usd    BIGINT NOT NULL CHECK (reserved_cost_micro_usd >= 0),
    actual_input_tokens        BIGINT CHECK (actual_input_tokens >= 0),
    actual_output_tokens       BIGINT CHECK (actual_output_tokens >= 0),
    actual_cost_micro_usd      BIGINT CHECK (actual_cost_micro_usd >= 0),
    response_payload           JSONB,
    error_reason               TEXT,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at               TIMESTAMPTZ,
    UNIQUE (principal_id, session_id, logical_call_id),
    UNIQUE (logical_call_pk, logical_call_id)
);

CREATE INDEX idx_llm_logical_call_run_created
    ON llm_logical_call(run_id, created_at DESC);
CREATE INDEX idx_llm_logical_call_budget_status
    ON llm_logical_call(budget_scope_id, status);

CREATE TABLE llm_provider_attempt (
    attempt_id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    logical_call_pk            UUID NOT NULL,
    logical_call_id            TEXT NOT NULL CHECK (btrim(logical_call_id) <> ''),
    attempt_no                 INTEGER NOT NULL DEFAULT 1 CHECK (attempt_no > 0),
    model_id                   TEXT NOT NULL CHECK (btrim(model_id) <> ''),
    provider                   TEXT NOT NULL CHECK (btrim(provider) <> ''),
    status                     TEXT NOT NULL DEFAULT 'RESERVED'
                               CHECK (status IN (
                                   'RESERVED',
                                   'IN_PROGRESS',
                                   'SUCCEEDED',
                                   'FAILED_NO_CHARGE',
                                   'PENDING_RECONCILIATION'
                               )),
    reserved_tokens            BIGINT NOT NULL CHECK (reserved_tokens >= 0),
    reserved_cost_micro_usd    BIGINT NOT NULL CHECK (reserved_cost_micro_usd >= 0),
    input_tokens               BIGINT CHECK (input_tokens >= 0),
    output_tokens              BIGINT CHECK (output_tokens >= 0),
    cost_micro_usd             BIGINT CHECK (cost_micro_usd >= 0),
    provider_request_id        TEXT,
    error_code                 TEXT,
    started_at                 TIMESTAMPTZ,
    finished_at                TIMESTAMPTZ,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (logical_call_pk, attempt_no),
    FOREIGN KEY (logical_call_pk, logical_call_id)
        REFERENCES llm_logical_call(logical_call_pk, logical_call_id)
);

CREATE INDEX idx_llm_provider_attempt_reconciliation
    ON llm_provider_attempt(status, created_at)
    WHERE status IN ('RESERVED', 'IN_PROGRESS', 'PENDING_RECONCILIATION');
CREATE INDEX idx_llm_provider_attempt_logical
    ON llm_provider_attempt(logical_call_id, attempt_no);

ALTER TABLE llm_call_log
    ADD COLUMN IF NOT EXISTS logical_call_id TEXT,
    ADD COLUMN IF NOT EXISTS ledger_entry_id UUID,
    ADD COLUMN IF NOT EXISTS attempt_id UUID,
    ADD COLUMN IF NOT EXISTS route_version TEXT,
    ADD COLUMN IF NOT EXISTS price_table_version TEXT,
    ADD COLUMN IF NOT EXISTS cost_micro_usd BIGINT,
    ADD COLUMN IF NOT EXISTS settlement_status TEXT;

CREATE INDEX idx_llm_call_log_logical_call
    ON llm_call_log(logical_call_id);

-- 价格与条目只允许在首次发布前补齐；发布后只能发布新版本。
CREATE OR REPLACE FUNCTION reject_llm_published_price_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    target_version TEXT;
    target_published_at TIMESTAMPTZ;
BEGIN
    IF TG_TABLE_NAME = 'llm_price_table' THEN
        IF TG_OP = 'DELETE' AND OLD.published_at IS NOT NULL THEN
            RAISE EXCEPTION 'published LLM price table is immutable'
                USING ERRCODE = '23514';
        END IF;
        IF TG_OP = 'UPDATE' AND OLD.published_at IS NOT NULL THEN
            RAISE EXCEPTION 'published LLM price table is immutable'
                USING ERRCODE = '23514';
        END IF;
        RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
    END IF;

    target_version := CASE
        WHEN TG_OP = 'DELETE' THEN OLD.price_table_version
        ELSE NEW.price_table_version
    END;
    SELECT published_at
      INTO target_published_at
      FROM llm_price_table
     WHERE price_table_version = target_version;
    IF NOT FOUND OR target_published_at IS NOT NULL THEN
        RAISE EXCEPTION 'published LLM price entries are immutable'
            USING ERRCODE = '23514';
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END
$$;

CREATE TRIGGER trg_llm_price_table_immutable
    BEFORE UPDATE OR DELETE ON llm_price_table
    FOR EACH ROW
    EXECUTE FUNCTION reject_llm_published_price_mutation();

CREATE TRIGGER trg_llm_price_entry_immutable
    BEFORE INSERT OR UPDATE OR DELETE ON llm_price_entry
    FOR EACH ROW
    EXECUTE FUNCTION reject_llm_published_price_mutation();

COMMENT ON TABLE llm_budget_scope IS
    '权威预算作用域：principal + session 唯一，跨 run 持续累计';
COMMENT ON TABLE llm_logical_call IS
    '一次语义调用；logical_call_id 按 principal + session 幂等';
COMMENT ON TABLE llm_provider_attempt IS
    '每次真实供应商请求；通过 logical_call_pk 与逻辑调用关联';
COMMENT ON TABLE llm_price_table IS
    '不可变价格版本；published_at 非空后价格和条目只读';
