-- M4b: 资格规则可读取的画像字段目录与类型契约。

CREATE TABLE eligibility_profile_field (
    field_key      TEXT PRIMARY KEY CHECK (btrim(field_key) <> ''),
    value_type     TEXT NOT NULL CHECK (value_type IN (
        'STRING',
        'INT',
        'STRING_LIST'
    )),
    allowed_values TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[]
                   CHECK (array_position(allowed_values, '') IS NULL),
    description    TEXT NOT NULL CHECK (btrim(description) <> ''),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (
        value_type = 'STRING'
        OR cardinality(allowed_values) = 0
    )
);

INSERT INTO eligibility_profile_field (
    field_key, value_type, allowed_values, description
) VALUES
    ('province', 'STRING', ARRAY['湖南']::TEXT[], 'V1 考生省份'),
    ('application_year', 'INT', ARRAY[]::TEXT[], '报考年'),
    ('category', 'STRING', ARRAY['物理类', '历史类']::TEXT[], '科类'),
    ('batch', 'STRING', ARRAY['本科批']::TEXT[], '录取批次'),
    ('score', 'INT', ARRAY[]::TEXT[], '高考总分'),
    ('subjects', 'STRING_LIST', ARRAY[]::TEXT[], '湖南 3+1+2 选科组合'),
    ('rank', 'INT', ARRAY[]::TEXT[], '算法推导位次'),
    ('physical_exam.color_vision', 'STRING',
        ARRAY['NORMAL', 'COLOR_WEAK', 'COLOR_BLIND']::TEXT[], '色觉结论'),
    ('foreign_language', 'STRING', ARRAY[]::TEXT[], '外语语种'),
    ('gender', 'STRING', ARRAY['MALE', 'FEMALE']::TEXT[], '性别'),
    ('graduate_status', 'STRING', ARRAY['FRESH', 'PREVIOUS']::TEXT[], '应届或往届'),
    ('household_province', 'STRING', ARRAY[]::TEXT[], '户籍省份'),
    ('school_registration_province', 'STRING', ARRAY[]::TEXT[], '学籍省份'),
    ('single_subject_scores.语文', 'INT', ARRAY[]::TEXT[], '语文单科成绩'),
    ('single_subject_scores.数学', 'INT', ARRAY[]::TEXT[], '数学单科成绩'),
    ('single_subject_scores.英语', 'INT', ARRAY[]::TEXT[], '英语单科成绩'),
    ('single_subject_scores.物理', 'INT', ARRAY[]::TEXT[], '物理单科成绩'),
    ('single_subject_scores.历史', 'INT', ARRAY[]::TEXT[], '历史单科成绩'),
    ('single_subject_scores.化学', 'INT', ARRAY[]::TEXT[], '化学单科成绩'),
    ('single_subject_scores.生物', 'INT', ARRAY[]::TEXT[], '生物单科成绩'),
    ('single_subject_scores.思想政治', 'INT', ARRAY[]::TEXT[], '思想政治单科成绩'),
    ('single_subject_scores.地理', 'INT', ARRAY[]::TEXT[], '地理单科成绩');

COMMENT ON TABLE eligibility_profile_field IS
    '结构化资格规则 allowlist；未知字段不得进入规则，缺失/UNKNOWN/DECLINED 不按满足处理';
