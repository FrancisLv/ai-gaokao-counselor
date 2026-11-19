-- M4a-01：policy 文档适用域、chunk set 身份与 release 只读映射。
-- 历史 policy 数据保留到修复为止；在线 RetrievePolicy 只读取元数据完整的映射行。

ALTER TABLE policy_doc
    ADD COLUMN IF NOT EXISTS document_type TEXT,
    ADD COLUMN IF NOT EXISTS issuer_type TEXT,
    ADD COLUMN IF NOT EXISTS issuer_id TEXT,
    ADD COLUMN IF NOT EXISTS school_id TEXT,
    ADD COLUMN IF NOT EXISTS fetched_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS content_sha256 TEXT,
    ADD COLUMN IF NOT EXISTS raw_storage_uri TEXT;

ALTER TABLE policy_chunk
    ADD COLUMN IF NOT EXISTS text_sha256 TEXT,
    ADD COLUMN IF NOT EXISTS char_start INTEGER,
    ADD COLUMN IF NOT EXISTS char_end INTEGER,
    ADD COLUMN IF NOT EXISTS embedding_model TEXT,
    ADD COLUMN IF NOT EXISTS embedding_dim INTEGER,
    ADD COLUMN IF NOT EXISTS index_version TEXT,
    ADD COLUMN IF NOT EXISTS chunk_set_version TEXT;

ALTER TABLE policy_doc
    ADD CONSTRAINT policy_doc_document_type_check
        CHECK (
            document_type IS NULL
            OR document_type IN (
                'charter', 'policy', 'physical_exam', 'university_notice'
            )
        ) NOT VALID,
    ADD CONSTRAINT policy_doc_issuer_type_check
        CHECK (
            issuer_type IS NULL
            OR issuer_type IN ('national', 'provincial', 'university')
        ) NOT VALID,
    ADD CONSTRAINT policy_doc_content_sha256_check
        CHECK (
            content_sha256 IS NULL
            OR content_sha256 ~ '^[0-9a-f]{64}$'
        ) NOT VALID;

ALTER TABLE policy_chunk
    ADD CONSTRAINT policy_chunk_text_sha256_check
        CHECK (
            text_sha256 IS NULL
            OR text_sha256 ~ '^[0-9a-f]{64}$'
        ) NOT VALID,
    ADD CONSTRAINT policy_chunk_locator_pair_check
        CHECK (
            (char_start IS NULL) = (char_end IS NULL)
            AND (
                char_start IS NULL
                OR (
                    char_start >= 0
                    AND char_end >= char_start
                )
            )
        ) NOT VALID,
    ADD CONSTRAINT policy_chunk_embedding_dim_check
        CHECK (embedding_dim IS NULL OR embedding_dim > 0) NOT VALID;

CREATE TABLE policy_chunk_set (
    chunk_set_version  TEXT PRIMARY KEY,
    doc_id             TEXT NOT NULL
                       REFERENCES policy_doc(id) ON DELETE CASCADE,
    content_sha256     TEXT NOT NULL
                       CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    embedding_model    TEXT NOT NULL,
    embedding_dim      INTEGER NOT NULL CHECK (embedding_dim > 0),
    index_version      TEXT NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (chunk_set_version, doc_id)
);

CREATE INDEX idx_policy_chunk_set_doc
    ON policy_chunk_set(doc_id);

ALTER TABLE policy_chunk
    ADD CONSTRAINT policy_chunk_chunk_set_version_doc_fk
        FOREIGN KEY (chunk_set_version, doc_id)
        REFERENCES policy_chunk_set(chunk_set_version, doc_id)
        ON DELETE CASCADE
        NOT VALID;

CREATE INDEX idx_policy_chunk_set_identity
    ON policy_chunk(chunk_set_version, id)
    WHERE chunk_set_version IS NOT NULL;

CREATE TABLE release_policy_doc (
    release_id         TEXT NOT NULL
                       REFERENCES release(release_id) ON DELETE CASCADE,
    doc_id             TEXT NOT NULL,
    chunk_set_version  TEXT NOT NULL,
    PRIMARY KEY (release_id, doc_id),
    FOREIGN KEY (chunk_set_version, doc_id)
        REFERENCES policy_chunk_set(chunk_set_version, doc_id)
        ON DELETE CASCADE
);

CREATE INDEX idx_release_policy_doc_chunk_set
    ON release_policy_doc(chunk_set_version, doc_id);

CREATE TABLE policy_applicability (
    id                 BIGSERIAL PRIMARY KEY,
    doc_id             TEXT NOT NULL
                       REFERENCES policy_doc(id) ON DELETE CASCADE,
    regions            TEXT[] NOT NULL,
    categories         TEXT[] NOT NULL,
    batches            TEXT[] NOT NULL,
    valid_from         DATE NOT NULL,
    valid_to           DATE,
    CHECK (cardinality(regions) > 0),
    CHECK (cardinality(categories) > 0),
    CHECK (cardinality(batches) > 0),
    CHECK (array_position(regions, NULL) IS NULL),
    CHECK (array_position(categories, NULL) IS NULL),
    CHECK (array_position(batches, NULL) IS NULL),
    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);

CREATE INDEX idx_policy_applicability_doc_valid
    ON policy_applicability(doc_id, valid_from, valid_to);

COMMENT ON TABLE policy_chunk_set IS
    '不可变 chunk 集合身份；同一 release 只能读取 release_policy_doc 指定的集合';
COMMENT ON TABLE release_policy_doc IS
    'release 到 policy 文档及唯一 chunk set 的只读映射';
COMMENT ON TABLE policy_applicability IS
    '文档适用域；必须先在相似度计算前完成硬过滤';
COMMENT ON TABLE charter_doc IS
    'DEPRECATED: use policy_doc (ADR-0006)';
COMMENT ON TABLE charter_chunk IS
    'DEPRECATED: use policy_chunk (ADR-0006)';
