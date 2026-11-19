-- M4a-02：release 绑定的 policy 向量索引身份。
-- chunk set 必须与 release 声明的 embedding 模型、维度和索引版本完全一致。

CREATE TABLE release_policy_index (
    release_id       TEXT PRIMARY KEY
                     REFERENCES release(release_id) ON DELETE CASCADE,
    embedding_model  TEXT NOT NULL
                     CHECK (btrim(embedding_model) <> ''),
    embedding_dim    INTEGER NOT NULL
                     CHECK (embedding_dim > 0),
    index_version    TEXT NOT NULL
                     CHECK (btrim(index_version) <> ''),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE release_policy_index IS
    'release 的 policy 向量索引身份；查询模型、维度与索引版本必须完全一致';
