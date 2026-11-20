-- M5a-03：release-bound、不可变的 prompt template 与 response schema 版本。
-- 新 Chat 路径只能引用这里注册的版本；原始 messages/json_schema 不构成授权。

CREATE TABLE llm_prompt_template (
    release_id              TEXT NOT NULL REFERENCES release(release_id),
    prompt_template_key     TEXT NOT NULL CHECK (btrim(prompt_template_key) <> ''),
    prompt_template_version TEXT NOT NULL CHECK (btrim(prompt_template_version) <> ''),
    system_template         TEXT NOT NULL CHECK (btrim(system_template) <> ''),
    user_template           TEXT NOT NULL DEFAULT '',
    template_sha256         CHAR(64) NOT NULL
                            CHECK (template_sha256 ~ '^[0-9a-f]{64}$'),
    status                  TEXT NOT NULL DEFAULT 'ACTIVE'
                            CHECK (status IN ('ACTIVE', 'REVOKED')),
    published_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at              TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (release_id, prompt_template_key, prompt_template_version),
    CHECK (
        (status = 'ACTIVE' AND revoked_at IS NULL)
        OR
        (status = 'REVOKED' AND revoked_at IS NOT NULL)
    )
);

CREATE TABLE llm_response_schema (
    release_id            TEXT NOT NULL REFERENCES release(release_id),
    response_schema_key   TEXT NOT NULL CHECK (btrim(response_schema_key) <> ''),
    response_schema_version TEXT NOT NULL
                            CHECK (btrim(response_schema_version) <> ''),
    schema_json           JSONB NOT NULL CHECK (jsonb_typeof(schema_json) = 'object'),
    schema_sha256         CHAR(64) NOT NULL
                          CHECK (schema_sha256 ~ '^[0-9a-f]{64}$'),
    status                TEXT NOT NULL DEFAULT 'ACTIVE'
                          CHECK (status IN ('ACTIVE', 'REVOKED')),
    published_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at            TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (release_id, response_schema_key, response_schema_version),
    CHECK (
        (status = 'ACTIVE' AND revoked_at IS NULL)
        OR
        (status = 'REVOKED' AND revoked_at IS NOT NULL)
    )
);

ALTER TABLE llm_role_route
    ADD COLUMN requires_response_schema BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE llm_logical_call
    ADD COLUMN prompt_template_key TEXT,
    ADD COLUMN prompt_template_version TEXT,
    ADD COLUMN prompt_template_sha256 CHAR(64),
    ADD COLUMN response_schema_key TEXT,
    ADD COLUMN response_schema_version TEXT,
    ADD COLUMN response_schema_sha256 CHAR(64);

ALTER TABLE llm_logical_call
    ADD CONSTRAINT llm_logical_call_prompt_identity_consistent
        CHECK (
            (prompt_template_key IS NULL
             AND prompt_template_version IS NULL
             AND prompt_template_sha256 IS NULL)
            OR
            (prompt_template_key IS NOT NULL
             AND btrim(prompt_template_key) <> ''
             AND prompt_template_version IS NOT NULL
             AND btrim(prompt_template_version) <> ''
             AND prompt_template_sha256 ~ '^[0-9a-f]{64}$')
        ),
    ADD CONSTRAINT llm_logical_call_schema_identity_consistent
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
    ADD CONSTRAINT llm_logical_call_prompt_registered
        FOREIGN KEY (
            release_id, prompt_template_key, prompt_template_version
        ) REFERENCES llm_prompt_template (
            release_id, prompt_template_key, prompt_template_version
        ),
    ADD CONSTRAINT llm_logical_call_schema_registered
        FOREIGN KEY (
            release_id, response_schema_key, response_schema_version
        ) REFERENCES llm_response_schema (
            release_id, response_schema_key, response_schema_version
        );

CREATE FUNCTION reject_llm_prompt_template_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' OR OLD.status = 'REVOKED' THEN
        RAISE EXCEPTION 'registered LLM prompt template is immutable'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.release_id IS DISTINCT FROM OLD.release_id
       OR NEW.prompt_template_key IS DISTINCT FROM OLD.prompt_template_key
       OR NEW.prompt_template_version IS DISTINCT FROM OLD.prompt_template_version
       OR NEW.system_template IS DISTINCT FROM OLD.system_template
       OR NEW.user_template IS DISTINCT FROM OLD.user_template
       OR NEW.template_sha256 IS DISTINCT FROM OLD.template_sha256
       OR NEW.published_at IS DISTINCT FROM OLD.published_at
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.status <> 'REVOKED' THEN
        RAISE EXCEPTION 'registered LLM prompt template content is immutable'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_llm_prompt_template_immutable
    BEFORE UPDATE OR DELETE ON llm_prompt_template
    FOR EACH ROW
    EXECUTE FUNCTION reject_llm_prompt_template_mutation();

CREATE FUNCTION reject_llm_response_schema_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' OR OLD.status = 'REVOKED' THEN
        RAISE EXCEPTION 'registered LLM response schema is immutable'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.release_id IS DISTINCT FROM OLD.release_id
       OR NEW.response_schema_key IS DISTINCT FROM OLD.response_schema_key
       OR NEW.response_schema_version IS DISTINCT FROM OLD.response_schema_version
       OR NEW.schema_json IS DISTINCT FROM OLD.schema_json
       OR NEW.schema_sha256 IS DISTINCT FROM OLD.schema_sha256
       OR NEW.published_at IS DISTINCT FROM OLD.published_at
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.status <> 'REVOKED' THEN
        RAISE EXCEPTION 'registered LLM response schema content is immutable'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_llm_response_schema_immutable
    BEFORE UPDATE OR DELETE ON llm_response_schema
    FOR EACH ROW
    EXECUTE FUNCTION reject_llm_response_schema_mutation();

COMMENT ON TABLE llm_prompt_template IS
    '服务端注册的 prompt 模板；system/user 内容按 release + key + version 不可变';
COMMENT ON TABLE llm_response_schema IS
    '服务端注册的 JSON Schema；调用方原始 json_schema 不能覆盖或授权';
