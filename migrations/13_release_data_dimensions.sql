-- M3 review fix：公开数据按不可变 release 选择，禁止同一 run 混读版本。

INSERT INTO release (
    release_id, status, manifest, manifest_sha256, published_at
)
VALUES (
    'rel_legacy_v1',
    'PUBLISHED',
    '{"legacy": true}'::jsonb,
    'legacy-v1',
    NOW()
)
ON CONFLICT (release_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS release_component (
    release_id       TEXT NOT NULL REFERENCES release(release_id),
    component_type   TEXT NOT NULL,
    dataset_key      TEXT NOT NULL,
    component_version TEXT NOT NULL,
    content_sha256   TEXT NOT NULL,
    PRIMARY KEY (release_id, component_type, dataset_key)
);

ALTER TABLE university ADD COLUMN IF NOT EXISTS release_id TEXT;
ALTER TABLE major ADD COLUMN IF NOT EXISTS release_id TEXT;
ALTER TABLE enrollment_plan ADD COLUMN IF NOT EXISTS release_id TEXT;
ALTER TABLE major_admission ADD COLUMN IF NOT EXISTS release_id TEXT;
ALTER TABLE school_admission ADD COLUMN IF NOT EXISTS release_id TEXT;
ALTER TABLE score_rank_map ADD COLUMN IF NOT EXISTS release_id TEXT;

UPDATE university SET release_id = 'rel_legacy_v1' WHERE release_id IS NULL;
UPDATE major SET release_id = 'rel_legacy_v1' WHERE release_id IS NULL;
UPDATE enrollment_plan SET release_id = 'rel_legacy_v1' WHERE release_id IS NULL;
UPDATE major_admission SET release_id = 'rel_legacy_v1' WHERE release_id IS NULL;
UPDATE school_admission SET release_id = 'rel_legacy_v1' WHERE release_id IS NULL;
UPDATE score_rank_map SET release_id = 'rel_legacy_v1' WHERE release_id IS NULL;

ALTER TABLE university ALTER COLUMN release_id SET NOT NULL;
ALTER TABLE major ALTER COLUMN release_id SET NOT NULL;
ALTER TABLE enrollment_plan ALTER COLUMN release_id SET NOT NULL;
ALTER TABLE major_admission ALTER COLUMN release_id SET NOT NULL;
ALTER TABLE school_admission ALTER COLUMN release_id SET NOT NULL;
ALTER TABLE score_rank_map ALTER COLUMN release_id SET NOT NULL;

ALTER TABLE university ALTER COLUMN release_id SET DEFAULT 'rel_legacy_v1';
ALTER TABLE major ALTER COLUMN release_id SET DEFAULT 'rel_legacy_v1';
ALTER TABLE enrollment_plan ALTER COLUMN release_id SET DEFAULT 'rel_legacy_v1';
ALTER TABLE major_admission ALTER COLUMN release_id SET DEFAULT 'rel_legacy_v1';
ALTER TABLE school_admission ALTER COLUMN release_id SET DEFAULT 'rel_legacy_v1';
ALTER TABLE score_rank_map ALTER COLUMN release_id SET DEFAULT 'rel_legacy_v1';

ALTER TABLE university DROP CONSTRAINT IF EXISTS university_school_id_key;
ALTER TABLE major DROP CONSTRAINT IF EXISTS major_university_id_major_group_major_code_major_name_key;
ALTER TABLE enrollment_plan DROP CONSTRAINT IF EXISTS enrollment_plan_university_code_major_group_major_code_category_batch_year_key;
ALTER TABLE major_admission DROP CONSTRAINT IF EXISTS major_admission_university_code_major_group_major_code_category_batch_year_key;
ALTER TABLE school_admission DROP CONSTRAINT IF EXISTS school_admission_university_code_major_group_category_batch_year_key;
ALTER TABLE score_rank_map DROP CONSTRAINT IF EXISTS score_rank_map_province_year_category_batch_score_key;

ALTER TABLE university
    ADD CONSTRAINT university_release_school_key
    UNIQUE (release_id, school_id);
ALTER TABLE major
    ADD CONSTRAINT major_release_identity_key
    UNIQUE (release_id, university_id, major_group, major_code, major_name);
ALTER TABLE enrollment_plan
    ADD CONSTRAINT enrollment_plan_release_identity_key
    UNIQUE (release_id, province, university_code, major_group, major_code, category, batch, year);
ALTER TABLE major_admission
    ADD CONSTRAINT major_admission_release_identity_key
    UNIQUE (release_id, province, university_code, major_group, major_code, category, batch, year);
ALTER TABLE school_admission
    ADD CONSTRAINT school_admission_release_identity_key
    UNIQUE (release_id, province, university_code, major_group, category, batch, year);
ALTER TABLE score_rank_map
    ADD CONSTRAINT score_rank_map_release_identity_key
    UNIQUE (release_id, province, year, category, batch, score);

CREATE INDEX IF NOT EXISTS idx_enrollment_plan_release_scope
    ON enrollment_plan(release_id, province, year, category, batch);
CREATE INDEX IF NOT EXISTS idx_major_admission_release_scope
    ON major_admission(release_id, province, year, category, batch);
CREATE INDEX IF NOT EXISTS idx_school_admission_release_scope
    ON school_admission(release_id, province, year, category, batch);
CREATE INDEX IF NOT EXISTS idx_score_rank_map_release_scope
    ON score_rank_map(release_id, province, year, category);
