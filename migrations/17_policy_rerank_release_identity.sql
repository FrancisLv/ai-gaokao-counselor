-- M4a-04：release 绑定的可选 policy rerank 配置身份。
-- 无配置行或 enabled=false 时不调用 rerank；启用时模型、配置版本与
-- 超时共同决定查询行为，不得由客户端逐次覆盖。

CREATE TABLE release_policy_rerank (
    release_id       TEXT PRIMARY KEY
                     REFERENCES release(release_id) ON DELETE CASCADE,
    enabled          BOOLEAN NOT NULL,
    model_name       TEXT
                     CHECK (model_name IS NULL OR btrim(model_name) <> ''),
    config_version   TEXT
                     CHECK (config_version IS NULL OR btrim(config_version) <> ''),
    timeout_ms       INTEGER
                     CHECK (timeout_ms IS NULL OR timeout_ms > 0),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (
        NOT enabled
        OR (
            model_name IS NOT NULL
            AND config_version IS NOT NULL
            AND timeout_ms IS NOT NULL
        )
    )
);

COMMENT ON TABLE release_policy_rerank IS
    'release 的可选 policy rerank 模型、配置版本与超时；客户端不可覆盖';
