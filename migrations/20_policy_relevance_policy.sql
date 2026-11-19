-- M4a follow-up：release 级证据相关性资格策略。
-- top_k 是上限；低于该策略的候选不得为凑满结果而进入证据列表。

CREATE TABLE IF NOT EXISTS release_policy_relevance (
    release_id                 TEXT PRIMARY KEY
                               REFERENCES release(release_id) ON DELETE CASCADE,
    policy_version             TEXT NOT NULL
                               CHECK (btrim(policy_version) <> ''),
    min_vector_similarity      DOUBLE PRECISION NOT NULL
                               CHECK (
                                   min_vector_similarity >= 0
                                   AND min_vector_similarity <= 1
                               ),
    min_keyword_similarity     DOUBLE PRECISION NOT NULL
                               CHECK (
                                   min_keyword_similarity >= 0
                                   AND min_keyword_similarity <= 1
                               ),
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE release_policy_relevance IS
    'release 绑定的绝对相关性资格策略；top_k 只限制最多返回数量';
