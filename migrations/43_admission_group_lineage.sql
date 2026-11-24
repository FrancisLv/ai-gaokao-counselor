-- 跨年院校专业组身份映射：release 绑定的当年组 → 历史录取组。

CREATE TABLE admission_group_lineage (
    release_id                 TEXT NOT NULL
                               REFERENCES release(release_id) ON DELETE CASCADE,
    target_year                INTEGER NOT NULL CHECK (target_year > 0),
    target_category            TEXT NOT NULL CHECK (btrim(target_category) <> ''),
    target_batch               TEXT NOT NULL CHECK (btrim(target_batch) <> ''),
    target_university_code     TEXT NOT NULL
                               CHECK (btrim(target_university_code) <> ''),
    target_major_group         TEXT NOT NULL
                               CHECK (btrim(target_major_group) <> ''),
    history_year               INTEGER NOT NULL CHECK (history_year > 0),
    history_university_code    TEXT NOT NULL
                               CHECK (btrim(history_university_code) <> ''),
    history_major_group        TEXT NOT NULL
                               CHECK (btrim(history_major_group) <> ''),
    mapping_method             TEXT NOT NULL
                               CHECK (mapping_method IN (
                                   'EXACT_CODE',
                                   'MAJOR_NAME_JACCARD'
                               )),
    confidence                 NUMERIC(5,4) NOT NULL
                               CHECK (confidence >= 0 AND confidence <= 1),
    target_primary_subject     TEXT NOT NULL DEFAULT '',
    history_primary_subject    TEXT NOT NULL DEFAULT '',
    target_major_names         TEXT[] NOT NULL,
    history_major_names        TEXT[] NOT NULL,
    matched_major_names        TEXT[] NOT NULL,
    target_major_count         INTEGER NOT NULL CHECK (target_major_count > 0),
    history_major_count        INTEGER NOT NULL CHECK (history_major_count > 0),
    matched_major_count        INTEGER NOT NULL CHECK (matched_major_count >= 0),
    mapping_version            TEXT NOT NULL CHECK (btrim(mapping_version) <> ''),
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (
        release_id,
        target_year,
        target_category,
        target_batch,
        target_university_code,
        target_major_group,
        history_year
    ),
    CHECK (history_year < target_year),
    CHECK (array_position(target_major_names, NULL) IS NULL),
    CHECK (array_position(history_major_names, NULL) IS NULL),
    CHECK (array_position(matched_major_names, NULL) IS NULL),
    CHECK (mapping_method <> 'EXACT_CODE' OR confidence = 1),
    CHECK (
        mapping_method <> 'MAJOR_NAME_JACCARD'
        OR matched_major_count = cardinality(matched_major_names)
    )
);

CREATE INDEX idx_admission_group_lineage_target
    ON admission_group_lineage(
        release_id,
        target_year,
        target_category,
        target_batch,
        target_university_code,
        target_major_group
    );

CREATE INDEX idx_admission_group_lineage_method
    ON admission_group_lineage(release_id, mapping_method, confidence DESC);

COMMENT ON TABLE admission_group_lineage IS
    'release 绑定的跨年专业组身份映射；精确代码优先，名称重合映射须保留置信度和匹配专业证据';
