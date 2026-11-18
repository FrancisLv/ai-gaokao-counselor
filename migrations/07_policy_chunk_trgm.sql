-- ============================================================
-- policy_doc / policy_chunk（招生章程 RAG 检索表）版本化 + pg_trgm 关键词索引
--
-- 背景：ingest_charters.py / retrieve_test.py / qa_check.py 一直实际操作
-- policy_doc / policy_chunk 两表，但其 DDL 此前只存在于活库中、不在任何
-- migration 里（00_schema.sql 定义的是同用途的 charter_doc / charter_chunk，
-- 实际为空表；表名收口另行处理）。本文件按活库 pg_dump 原样补录 DDL，
-- 使 migrations 对新环境可完整重放。
--
-- 新增：text 列的 pg_trgm GIN 索引，支撑「向量 ‖ 关键词」双路混合检索的
-- 关键词一路（ILIKE 子串匹配加速，见 services/orchestrator/rag/retrieve.py）。
-- 扩展 pg_trgm 已由 00_schema.sql 启用；中文分词（zhparser/tsvector）按
-- 分阶段策略推迟，待 golden set 评测后再决策。
-- ============================================================

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------- policy_doc（章程文档级） ----------
CREATE TABLE IF NOT EXISTS policy_doc (
    id              TEXT PRIMARY KEY,          -- 稳定 doc_id（ingest 侧按元数据 hash 生成）
    title           TEXT,                      -- 文档标题
    issuer          TEXT,                      -- 发布院校
    effective_year  INTEGER,                   -- 生效年份
    region          TEXT,                      -- 省份/地区
    source_uri      TEXT,                      -- 原始 URL（溯源）
    raw_path        TEXT                       -- 原始文件路径（溯源）
);

-- ---------- policy_chunk（章程分块级，检索单元） ----------
CREATE TABLE IF NOT EXISTS policy_chunk (
    id              TEXT PRIMARY KEY,
    doc_id          TEXT REFERENCES policy_doc(id),
    section_path    TEXT,                      -- 章节路径（如「第三章 > 第十条」）
    text            TEXT,                      -- 块文本
    meta            JSONB,                     -- 冗余过滤字段：school_name / year / url
    embedding       vector(1024),              -- bge-large-zh-v1.5
    tsv             tsvector                   -- 预留（当前 'simple' 分词对中文不可用）
);

-- 存量索引（活库已有，IF NOT EXISTS 幂等补录）
CREATE INDEX IF NOT EXISTS idx_chunk_embedding ON policy_chunk USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_chunk_meta      ON policy_chunk USING gin (meta);
CREATE INDEX IF NOT EXISTS idx_chunk_tsv       ON policy_chunk USING gin (tsv);

-- 新增：关键词一路的 trgm 索引（加速 text ILIKE '%term%'）
CREATE INDEX IF NOT EXISTS idx_chunk_text_trgm ON policy_chunk USING gin (text gin_trgm_ops);
