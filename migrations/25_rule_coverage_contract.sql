-- M4b: RuleCoverage 的规范 scope、来源完整性与发布后冻结。

CREATE OR REPLACE FUNCTION eligibility_scope_is_valid(scope JSONB)
RETURNS BOOLEAN
LANGUAGE SQL
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT
        jsonb_typeof(scope) = 'object'
        AND (scope - ARRAY[
            'province',
            'application_year',
            'category',
            'batch',
            'university_id',
            'major_group',
            'major_code'
        ]) = '{}'::jsonb
        AND (
            NOT scope ? 'province'
            OR (
                jsonb_typeof(scope -> 'province') = 'string'
                AND btrim(scope ->> 'province') <> ''
            )
        )
        AND (
            NOT scope ? 'application_year'
            OR (
                jsonb_typeof(scope -> 'application_year') = 'number'
                AND (scope ->> 'application_year') ~ '^[1-9][0-9]*$'
            )
        )
        AND (
            NOT scope ? 'category'
            OR (
                jsonb_typeof(scope -> 'category') = 'string'
                AND btrim(scope ->> 'category') <> ''
            )
        )
        AND (
            NOT scope ? 'batch'
            OR (
                jsonb_typeof(scope -> 'batch') = 'string'
                AND btrim(scope ->> 'batch') <> ''
            )
        )
        AND (
            NOT scope ? 'university_id'
            OR (
                jsonb_typeof(scope -> 'university_id') = 'number'
                AND (scope ->> 'university_id') ~ '^[1-9][0-9]*$'
            )
        )
        AND (
            NOT scope ? 'major_group'
            OR (
                jsonb_typeof(scope -> 'major_group') = 'string'
                AND btrim(scope ->> 'major_group') <> ''
            )
        )
        AND (
            NOT scope ? 'major_code'
            OR (
                jsonb_typeof(scope -> 'major_code') = 'string'
                AND btrim(scope ->> 'major_code') <> ''
            )
        );
$$;

CREATE OR REPLACE FUNCTION eligibility_scope_key(scope JSONB)
RETURNS TEXT
LANGUAGE SQL
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT jsonb_build_array(
        COALESCE(scope ->> 'province', ''),
        COALESCE(scope ->> 'application_year', ''),
        COALESCE(scope ->> 'category', ''),
        COALESCE(scope ->> 'batch', ''),
        COALESCE(scope ->> 'university_id', ''),
        COALESCE(scope ->> 'major_group', ''),
        COALESCE(scope ->> 'major_code', '')
    )::text;
$$;

CREATE OR REPLACE FUNCTION rule_coverage_sources_are_traceable(sources JSONB)
RETURNS BOOLEAN
LANGUAGE SQL
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT
        jsonb_typeof(sources) = 'array'
        AND NOT EXISTS (
            SELECT 1
            FROM jsonb_array_elements(sources) AS source
            WHERE jsonb_typeof(source) <> 'string'
               OR btrim(source #>> '{}') = ''
        );
$$;

ALTER TABLE rule_version
    DROP CONSTRAINT IF EXISTS rule_version_scope_valid;
ALTER TABLE rule_version
    ADD CONSTRAINT rule_version_scope_valid
    CHECK (eligibility_scope_is_valid(scope));

ALTER TABLE rule_coverage
    ADD COLUMN IF NOT EXISTS scope_key TEXT
    GENERATED ALWAYS AS (eligibility_scope_key(scope)) STORED;

ALTER TABLE rule_coverage
    DROP CONSTRAINT IF EXISTS rule_coverage_release_id_rule_type_scope_key;
ALTER TABLE rule_coverage
    DROP CONSTRAINT IF EXISTS rule_coverage_release_type_scope_key_key;
ALTER TABLE rule_coverage
    ADD CONSTRAINT rule_coverage_release_type_scope_key_key
    UNIQUE (release_id, rule_type, scope_key);

ALTER TABLE rule_coverage
    DROP CONSTRAINT IF EXISTS rule_coverage_scope_valid;
ALTER TABLE rule_coverage
    ADD CONSTRAINT rule_coverage_scope_valid
    CHECK (eligibility_scope_is_valid(scope));

ALTER TABLE rule_coverage
    DROP CONSTRAINT IF EXISTS rule_coverage_source_entries;
ALTER TABLE rule_coverage
    ADD CONSTRAINT rule_coverage_source_entries
    CHECK (rule_coverage_sources_are_traceable(covered_data_sources));

ALTER TABLE rule_coverage
    DROP CONSTRAINT IF EXISTS rule_coverage_check;
ALTER TABLE rule_coverage
    DROP CONSTRAINT IF EXISTS rule_coverage_check1;
ALTER TABLE rule_coverage
    DROP CONSTRAINT IF EXISTS rule_coverage_full_sources;
ALTER TABLE rule_coverage
    ADD CONSTRAINT rule_coverage_full_sources
    CHECK (
        status <> 'FULL'
        OR (
            jsonb_array_length(covered_data_sources) > 0
            AND btrim(notes) <> ''
        )
    );

ALTER TABLE rule_coverage
    DROP CONSTRAINT IF EXISTS rule_coverage_gap_notes;
ALTER TABLE rule_coverage
    ADD CONSTRAINT rule_coverage_gap_notes
    CHECK (status = 'FULL' OR btrim(notes) <> '');

CREATE OR REPLACE FUNCTION rule_coverage_guard_snapshot()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    target_release_id TEXT;
    release_state TEXT;
BEGIN
    target_release_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.release_id ELSE NEW.release_id END;

    IF TG_OP = 'UPDATE' AND NEW.release_id IS DISTINCT FROM OLD.release_id THEN
        RAISE EXCEPTION 'RuleCoverage cannot move between releases'
            USING ERRCODE = '23514';
    END IF;

    SELECT status INTO release_state
    FROM release
    WHERE release_id = target_release_id;

    IF release_state IS NOT NULL AND release_state <> 'STAGING' THEN
        RAISE EXCEPTION 'RuleCoverage is frozen after release publication'
            USING ERRCODE = '23514';
    END IF;

    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END
$$;

DROP TRIGGER IF EXISTS trg_rule_coverage_guard_insert ON rule_coverage;
CREATE TRIGGER trg_rule_coverage_guard_insert
    BEFORE INSERT ON rule_coverage
    FOR EACH ROW
    EXECUTE FUNCTION rule_coverage_guard_snapshot();

DROP TRIGGER IF EXISTS trg_rule_coverage_guard_update ON rule_coverage;
CREATE TRIGGER trg_rule_coverage_guard_update
    BEFORE UPDATE ON rule_coverage
    FOR EACH ROW
    EXECUTE FUNCTION rule_coverage_guard_snapshot();

DROP TRIGGER IF EXISTS trg_rule_coverage_guard_delete ON rule_coverage;
CREATE TRIGGER trg_rule_coverage_guard_delete
    BEFORE DELETE ON rule_coverage
    FOR EACH ROW
    EXECUTE FUNCTION rule_coverage_guard_snapshot();

CREATE INDEX IF NOT EXISTS idx_rule_coverage_release_scope_key
    ON rule_coverage(release_id, scope_key);

COMMENT ON COLUMN rule_coverage.scope_key IS
    '规范化适用域身份；禁止同 release/rule_type 下同范围重复声明';
COMMENT ON TABLE rule_coverage IS
    'release 内按规则类型/适用域声明的 FULL/PARTIAL/NONE 覆盖；发布后冻结，无规则不等于无限制';
