-- ============================================================
-- GaokaoCompass 高考志愿智能决策 Agent 系统 - 数据库初始化脚本
-- 数据库: ai_gaokao_counselor
-- PostgreSQL 16+ (with pgvector)
-- ============================================================

-- 启用扩展
CREATE EXTENSION IF NOT EXISTS vector;      -- pgvector 向量检索
CREATE EXTENSION IF NOT EXISTS pg_trgm;     -- 模糊搜索

-- ============================================================
-- 1. university（院校主表）
-- 来源: 掌上高考 CDN
-- 行数: ~2991 所
-- ============================================================
CREATE TABLE university (
    -- 主键
    id              BIGSERIAL PRIMARY KEY,
    -- 标识字段
    school_id       VARCHAR(20) UNIQUE,              -- 掌上高考院校ID（如 "31"=北京大学）
    enrollment_code VARCHAR(20),                     -- 教育部招生代码（如 "10001"），关联 CSV 中的 university_code
    name            VARCHAR(200) NOT NULL,           -- 院校全称（如 "北京大学"）
    short_name      VARCHAR(50),                     -- 院校简称（如 "北大"）
    -- 地理信息
    province        VARCHAR(20) NOT NULL,            -- 所在省份（如 "北京"）
    city            VARCHAR(50),                     -- 所在城市（如 "北京市"）
    address         TEXT,                            -- 校区地址
    -- 分类信息
    type            VARCHAR(20),                     -- 院校类型：综合/理工/师范/医药/农林/财经/政法/语言/体育/艺术/民族/军事
    nature          VARCHAR(20),                     -- 办学性质：公办/民办/中外合作办学/独立学院
    level           VARCHAR(20),                     -- 办学层次：普通本科/专科（高职）
    -- 标签标记
    is_985          BOOLEAN DEFAULT FALSE,           -- 是否985
    is_211          BOOLEAN DEFAULT FALSE,           -- 是否211
    is_dual_class   BOOLEAN DEFAULT FALSE,           -- 是否双一流
    -- 详细信息
    create_date     VARCHAR(10),                     -- 建校年份
    belong          VARCHAR(100),                    -- 主管部门（如 "教育部"）
    website         VARCHAR(500),                    -- 学校官网 URL
    phone           VARCHAR(100),                    -- 招生咨询电话
    -- 学术数据
    num_master      INTEGER,                         -- 一级学科硕士点数量
    num_doctor      INTEGER,                         -- 一级学科博士点数量
    ruanke_rank     INTEGER,                         -- 软科排名
    -- 特色标签
    labels          JSONB DEFAULT '[]',              -- 特色标签数组
    -- 元数据
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_university_province ON university(province);
CREATE INDEX idx_university_type ON university(type);
CREATE INDEX idx_university_enrollment_code ON university(enrollment_code);
CREATE INDEX idx_university_labels ON university USING GIN(labels);

-- ============================================================
-- 2. major（专业表）
-- 来源: enrollment-plan.csv + major-admission.csv
-- ============================================================
CREATE TABLE major (
    -- 主键
    id              BIGSERIAL PRIMARY KEY,
    -- 关联字段
    university_id   BIGINT REFERENCES university(id),
    -- 标识字段
    major_code      VARCHAR(20),                     -- 专业代码（如 "080901"）
    major_name      VARCHAR(200) NOT NULL,           -- 专业名称
    major_group     VARCHAR(100),                    -- 专业组代码
    -- 选科要求
    subject_req     JSONB,                           -- 选科要求 JSONB: {"track":"物理类","subjects":["物理"],"reselect":"不限"}
    -- 其他信息
    major_note      TEXT,                            -- 专业备注
    duration        VARCHAR(20),                     -- 学制（如 "四年"）
    tuition         DECIMAL(10,2),                   -- 学费（元/学年）
    -- 元数据
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    -- 唯一约束
    UNIQUE(university_id, major_group, major_code, major_name)
);

CREATE INDEX idx_major_university ON major(university_id);
CREATE INDEX idx_major_name ON major(major_name);
CREATE INDEX idx_major_subject_req ON major USING GIN(subject_req);

-- ============================================================
-- 3. enrollment_plan（招生计划表）
-- 来源: enrollment-plan.csv
-- ============================================================
CREATE TABLE enrollment_plan (
    -- 主键
    id              BIGSERIAL PRIMARY KEY,
    -- 关联字段
    university_id   BIGINT REFERENCES university(id),
    major_id        BIGINT REFERENCES major(id),
    -- 维度字段
    province        VARCHAR(20) NOT NULL,            -- 录取省份（固定 "湖南"）
    year            SMALLINT NOT NULL,               -- 招生年份
    category        VARCHAR(20) NOT NULL,            -- 科类：物理类/历史类/艺术类等
    batch           VARCHAR(50) NOT NULL,            -- 批次：本科批/专科批等
    -- 冗余标识
    university_code VARCHAR(20),                     -- 招生代码（冗余）
    university_name VARCHAR(200),                    -- 院校名称（冗余）
    major_group     VARCHAR(100),                    -- 专业组（冗余）
    major_code      VARCHAR(20),                     -- 专业代码（冗余）
    major_name      VARCHAR(200),                    -- 专业名称（冗余）
    -- 核心数据
    plan_count      INTEGER NOT NULL,                -- 招生计划数
    -- 其他信息
    subject_req     VARCHAR(500),                    -- 选科要求（原始文本）
    duration        VARCHAR(20),                     -- 学制
    tuition         DECIMAL(10,2),                   -- 学费
    -- 元数据
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    -- 唯一约束
    UNIQUE(university_code, major_group, major_code, category, batch, year)
);

CREATE INDEX idx_enrollment_plan_query ON enrollment_plan(province, year, category, batch);
CREATE INDEX idx_enrollment_plan_university ON enrollment_plan(university_id);
CREATE INDEX idx_enrollment_plan_code ON enrollment_plan(university_code, major_code);

-- ============================================================
-- 4. major_admission（专业录取分数表）
-- 来源: major-admission.csv
-- ============================================================
CREATE TABLE major_admission (
    -- 主键
    id              BIGSERIAL PRIMARY KEY,
    -- 关联字段
    university_id   BIGINT REFERENCES university(id),
    major_id        BIGINT REFERENCES major(id),
    -- 维度字段
    province        VARCHAR(20) NOT NULL,            -- 录取省份
    year            SMALLINT NOT NULL,               -- 录取年份
    category        VARCHAR(20) NOT NULL,            -- 科类
    batch           VARCHAR(50) NOT NULL,            -- 批次
    -- 冗余标识
    university_code VARCHAR(20),
    university_name VARCHAR(200),
    major_group     VARCHAR(100),
    major_code      VARCHAR(20),
    major_name      VARCHAR(200),
    -- 核心分数数据
    min_score       SMALLINT,                        -- 最低录取分
    max_score       SMALLINT,                        -- 最高录取分
    avg_score       DECIMAL(6,2),                    -- 平均录取分
    min_rank        INTEGER,                         -- 最低分位次
    -- 其他数据
    admit_count     INTEGER,                         -- 实际录取人数
    subject_req     VARCHAR(500),                    -- 选科要求
    major_note      TEXT,                            -- 专业备注
    -- 院校属性（冗余）
    school_province VARCHAR(20),                     -- 院校所在省份
    school_nature   VARCHAR(20),                     -- 办学性质
    is_985          BOOLEAN DEFAULT FALSE,
    is_211          BOOLEAN DEFAULT FALSE,
    -- 元数据
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    -- 唯一约束
    UNIQUE(university_code, major_group, major_code, category, batch, year)
);

CREATE INDEX idx_major_admission_query ON major_admission(province, year, category, batch);
CREATE INDEX idx_major_admission_university ON major_admission(university_id);
CREATE INDEX idx_major_admission_score ON major_admission(min_score);
CREATE INDEX idx_major_admission_rank ON major_admission(min_rank);
CREATE INDEX idx_major_admission_code ON major_admission(university_code, major_code);

-- ============================================================
-- 5. school_admission（院校投档线表）
-- 来源: school-admission.csv
-- ============================================================
CREATE TABLE school_admission (
    -- 主键
    id              BIGSERIAL PRIMARY KEY,
    -- 关联字段
    university_id   BIGINT REFERENCES university(id),
    -- 维度字段
    province        VARCHAR(20) NOT NULL,
    year            SMALLINT NOT NULL,
    category        VARCHAR(20) NOT NULL,
    batch           VARCHAR(50) NOT NULL,
    -- 冗余标识
    university_code VARCHAR(20),
    university_name VARCHAR(200),
    major_group     VARCHAR(100),
    -- 核心数据
    min_score       SMALLINT,                        -- 投档最低分
    min_rank        INTEGER,                         -- 最低分位次
    control_score   SMALLINT,                        -- 批次控制线
    score_diff      SMALLINT,                        -- 线差
    -- 其他
    subject_req     VARCHAR(500),
    -- 院校属性
    school_province VARCHAR(20),
    school_nature   VARCHAR(20),
    is_985          BOOLEAN DEFAULT FALSE,
    is_211          BOOLEAN DEFAULT FALSE,
    -- 元数据
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    -- 唯一约束
    UNIQUE(university_code, major_group, category, batch, year)
);

CREATE INDEX idx_school_admission_query ON school_admission(province, year, category, batch);
CREATE INDEX idx_school_admission_university ON school_admission(university_id);
CREATE INDEX idx_school_admission_rank ON school_admission(min_rank);

-- ============================================================
-- 6. score_rank_map（一分一段表）
-- 来源: score-range.csv
-- ============================================================
CREATE TABLE score_rank_map (
    -- 主键
    id              BIGSERIAL PRIMARY KEY,
    -- 维度字段
    province        VARCHAR(20) NOT NULL,
    year            SMALLINT NOT NULL,
    category        VARCHAR(20) NOT NULL,
    batch           VARCHAR(50) NOT NULL,
    -- 核心数据
    control_score   SMALLINT,                        -- 批次控制线
    score           SMALLINT NOT NULL,               -- 分数
    score_range     VARCHAR(20),                     -- 分数段
    segment_count   INTEGER NOT NULL,                -- 本段人数
    cumulative_count INTEGER NOT NULL,               -- 累计人数（位次）
    rank_range      VARCHAR(30),                     -- 位次范围
    -- 唯一约束
    UNIQUE(province, year, category, batch, score)
);

CREATE INDEX idx_score_rank_map_query ON score_rank_map(province, year, category, batch);
CREATE INDEX idx_score_rank_map_score ON score_rank_map(score);
CREATE INDEX idx_score_rank_map_rank ON score_rank_map(cumulative_count);

-- ============================================================
-- 7. charter_doc（招生章程文档表）
-- 来源: data/charters/2025/*.txt + *.json
-- ============================================================
CREATE TABLE charter_doc (
    -- 主键
    id              BIGSERIAL PRIMARY KEY,
    -- 关联字段
    university_id   BIGINT REFERENCES university(id),
    -- 文档信息
    title           VARCHAR(500) NOT NULL,           -- 文档标题
    university_name VARCHAR(200) NOT NULL,           -- 院校名称
    year            SMALLINT NOT NULL,               -- 生效年份
    -- 来源信息
    source_url      TEXT,                            -- 原始 URL
    raw_path        TEXT,                            -- 原始文件路径
    -- 全文内容
    full_text       TEXT NOT NULL,                   -- 完整章程文本
    -- 处理状态
    chunk_count     INTEGER DEFAULT 0,               -- 已切分块数
    indexed_at      TIMESTAMPTZ,                     -- 索引完成时间
    -- 元数据
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    -- 唯一约束
    UNIQUE(university_name, year)
);

CREATE INDEX idx_charter_doc_university ON charter_doc(university_id);
CREATE INDEX idx_charter_doc_year ON charter_doc(year);

-- ============================================================
-- 8. charter_chunk（招生章程分块表）
-- RAG 检索核心，向量 + 全文双路检索
-- ============================================================
CREATE TABLE charter_chunk (
    -- 主键
    id              BIGSERIAL PRIMARY KEY,
    -- 关联字段
    doc_id          BIGINT NOT NULL REFERENCES charter_doc(id) ON DELETE CASCADE,
    -- 定位信息
    chunk_index     INTEGER NOT NULL,                -- 块序号
    section_path    VARCHAR(500),                    -- 章节路径
    -- 内容
    text            TEXT NOT NULL,                   -- 块文本内容
    char_count      INTEGER,                         -- 字符数
    -- 向量 & 全文检索
    embedding       vector(1024),                    -- pgvector 向量（bge-large-zh-v1.5, 1024维）
    tsv             tsvector,                        -- 全文检索向量
    -- 元数据（冗余）
    university_name VARCHAR(200),                    -- 院校名称
    year            SMALLINT,                        -- 生效年份
    source_uri      VARCHAR(500),                    -- 溯源 URI
    -- 扩展元数据
    meta            JSONB DEFAULT '{}',
    -- 唯一约束
    UNIQUE(doc_id, chunk_index)
);

CREATE INDEX idx_charter_chunk_doc ON charter_chunk(doc_id);
CREATE INDEX idx_charter_chunk_embedding ON charter_chunk USING hnsw(embedding vector_cosine_ops);
CREATE INDEX idx_charter_chunk_tsv ON charter_chunk USING GIN(tsv);
CREATE INDEX idx_charter_chunk_university ON charter_chunk(university_name);
CREATE INDEX idx_charter_chunk_year ON charter_chunk(year);
CREATE INDEX idx_charter_chunk_meta ON charter_chunk USING GIN(meta);

-- ============================================================
-- 9. chat_session（会话表）
-- ============================================================
CREATE TABLE chat_session (
    -- 主键
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- 用户信息
    user_id         VARCHAR(100),
    -- 用户画像
    profile         JSONB,                           -- {"score":580,"rank":15000,"province":"湖南","category":"物理类","preferences":{...}}
    -- 状态
    status          VARCHAR(20) DEFAULT 'active',    -- active/completed/expired
    current_node    VARCHAR(50),                     -- 当前编排图节点
    -- 预算控制
    token_budget    INTEGER DEFAULT 50000,
    token_used      INTEGER DEFAULT 0,
    call_budget     INTEGER DEFAULT 30,
    call_used       INTEGER DEFAULT 0,
    -- 元数据
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    expired_at      TIMESTAMPTZ
);

CREATE INDEX idx_chat_session_user ON chat_session(user_id);
CREATE INDEX idx_chat_session_status ON chat_session(status);

-- ============================================================
-- 10. plan_checkpoint（编排检查点表）
-- ============================================================
CREATE TABLE plan_checkpoint (
    -- 主键
    id              BIGSERIAL PRIMARY KEY,
    -- 关联字段
    session_id      UUID NOT NULL REFERENCES chat_session(id),
    -- 检查点数据
    node            VARCHAR(50) NOT NULL,            -- 编排图节点名
    state           JSONB NOT NULL,                  -- 完整状态快照
    seq             INTEGER NOT NULL,                -- 序列号
    -- 元数据
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    -- 唯一约束
    UNIQUE(session_id, seq)
);

CREATE INDEX idx_checkpoint_session ON plan_checkpoint(session_id);

-- ============================================================
-- 11. llm_call_log（LLM 调用日志表）
-- ============================================================
CREATE TABLE llm_call_log (
    -- 主键
    id              BIGSERIAL PRIMARY KEY,
    -- 追踪信息
    trace_id        VARCHAR(64),
    session_id      UUID,
    node            VARCHAR(50),
    -- 调用信息
    role            VARCHAR(20),                     -- profile/strategy/critic/report/verify
    model           VARCHAR(100),
    prompt_hash     VARCHAR(64),
    -- Token 统计
    in_tokens       INTEGER,
    out_tokens      INTEGER,
    -- 成本与性能
    cost            DECIMAL(10,6),
    latency_ms      INTEGER,
    -- 状态
    status          VARCHAR(20) DEFAULT 'success',   -- success/retry/fail
    error_message   TEXT,
    -- 元数据
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_llm_call_trace ON llm_call_log(trace_id);
CREATE INDEX idx_llm_call_session ON llm_call_log(session_id);
CREATE INDEX idx_llm_call_created ON llm_call_log(created_at);
CREATE INDEX idx_llm_call_model ON llm_call_log(model);

-- ============================================================
-- 12. eval_run（评测回归记录表）
-- ============================================================
CREATE TABLE eval_run (
    -- 主键
    id              BIGSERIAL PRIMARY KEY,
    -- 版本信息
    git_sha         VARCHAR(40),
    data_version    INTEGER,
    -- 评测指标
    metrics         JSONB NOT NULL,
    passed          BOOLEAN NOT NULL,
    -- 详细统计
    total_queries   INTEGER,
    passed_queries  INTEGER,
    failed_queries  INTEGER,
    details         JSONB,
    -- 元数据
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_eval_run_passed ON eval_run(passed);
CREATE INDEX idx_eval_run_created ON eval_run(created_at);

-- ============================================================
-- 13. data_version（数据版本表）
-- ============================================================
CREATE TABLE data_version (
    -- 主键
    id              SERIAL PRIMARY KEY,
    -- 版本信息
    version         INTEGER NOT NULL,
    description     VARCHAR(500),
    -- 元数据
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- 初始化第一个版本
INSERT INTO data_version (version, description) VALUES (1, '初始数据导入：湖南2022-2025录取数据+掌上高考院校信息+2025招生章程');
