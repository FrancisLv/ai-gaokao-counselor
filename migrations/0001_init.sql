-- 初始 schema（对应架构文档第 9 章「数据存储设计」）
-- 三合一：结构化关系表 + pgvector（向量）+ tsvector（全文倒排）

CREATE EXTENSION IF NOT EXISTS vector;
-- 中文全文检索：默认分词对中文很差，生产请安装 zhparser / pg_jieba（见架构文档 7.5）
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------- 结构化数据 ----------
CREATE TABLE IF NOT EXISTS college (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    province    TEXT,
    level       TEXT,
    tags        TEXT[],
    updated_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS major (
    id          TEXT PRIMARY KEY,
    college_id  TEXT REFERENCES college (id),
    code        TEXT,
    name        TEXT,
    category    TEXT,
    subject_req JSONB                 -- 选科要求
);

CREATE TABLE IF NOT EXISTS admission_score (
    id          BIGSERIAL PRIMARY KEY,
    college_id  TEXT REFERENCES college (id),
    major_id    TEXT,
    province    TEXT,
    year        INT,
    track       TEXT,                 -- 物理类 / 历史类 / 理科 / 文科 / 综合
    batch       TEXT,
    min_score   INT,
    min_rank    INT,
    plan_count  INT,
    source_uri  TEXT
);
CREATE INDEX IF NOT EXISTS idx_adm_lookup
    ON admission_score (province, year, track, college_id);

-- 一分一段表
CREATE TABLE IF NOT EXISTS score_rank_map (
    province TEXT,
    year     INT,
    track    TEXT,
    score    INT,
    rank     INT,
    PRIMARY KEY (province, year, track, score)
);

-- ---------- 非结构化（政策 / 章程）----------
CREATE TABLE IF NOT EXISTS policy_doc (
    id             TEXT PRIMARY KEY,
    title          TEXT,
    issuer         TEXT,
    effective_year INT,
    region         TEXT,
    source_uri     TEXT,
    raw_path       TEXT
);

CREATE TABLE IF NOT EXISTS policy_chunk (
    id           TEXT PRIMARY KEY,
    doc_id       TEXT REFERENCES policy_doc (id),
    section_path TEXT,                -- 父级标题路径，如「第三章 录取规则 > 3.2 平行志愿」
    text         TEXT,
    meta         JSONB,
    embedding    vector(1024),        -- bge-large-zh-v1.5
    tsv          tsvector
);
CREATE INDEX IF NOT EXISTS idx_chunk_embedding ON policy_chunk USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_chunk_tsv       ON policy_chunk USING gin (tsv);

-- ---------- 编排检查点 / 成本 ----------
CREATE TABLE IF NOT EXISTS plan_checkpoint (
    session_id TEXT,
    node       TEXT,
    state      JSONB,
    seq        BIGINT,
    created_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (session_id, seq)
);

CREATE TABLE IF NOT EXISTS llm_call_log (
    trace_id    TEXT,
    node        TEXT,
    role        TEXT,
    model       TEXT,
    prompt_hash TEXT,
    in_tokens   INT,
    out_tokens  INT,
    cost        NUMERIC(12, 6),
    latency_ms  INT,
    created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_llm_log_trace ON llm_call_log (trace_id);
