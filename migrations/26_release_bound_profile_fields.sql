-- M4b: eligibility profile field allowlist is frozen into each release.

CREATE TABLE eligibility_profile_field_template (
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
    CHECK (value_type = 'STRING' OR cardinality(allowed_values) = 0)
);

INSERT INTO eligibility_profile_field_template (
    field_key, value_type, allowed_values, description, created_at
)
SELECT field_key, value_type, allowed_values, description, created_at
FROM eligibility_profile_field;

ALTER TABLE eligibility_profile_field
    DROP CONSTRAINT eligibility_profile_field_pkey,
    ADD COLUMN release_id TEXT;

INSERT INTO eligibility_profile_field (
    release_id, field_key, value_type, allowed_values, description, created_at
)
SELECT
    release.release_id,
    field.field_key,
    field.value_type,
    field.allowed_values,
    field.description,
    field.created_at
FROM release
CROSS JOIN eligibility_profile_field_template field;

DELETE FROM eligibility_profile_field WHERE release_id IS NULL;

ALTER TABLE eligibility_profile_field
    ALTER COLUMN release_id SET NOT NULL,
    ADD CONSTRAINT eligibility_profile_field_release_fk
        FOREIGN KEY (release_id) REFERENCES release(release_id) ON DELETE CASCADE,
    ADD PRIMARY KEY (release_id, field_key);

CREATE INDEX eligibility_profile_field_release_idx
    ON eligibility_profile_field(release_id, field_key);

CREATE OR REPLACE FUNCTION eligibility_profile_field_guard_snapshot()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    target_release_id TEXT;
    release_state TEXT;
BEGIN
    target_release_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.release_id ELSE NEW.release_id END;

    IF current_setting('app.eligibility_profile_field_seed_release', true)
       = target_release_id THEN
        RETURN NEW;
    END IF;

    IF TG_OP = 'UPDATE' AND NEW.release_id IS DISTINCT FROM OLD.release_id THEN
        RAISE EXCEPTION 'eligibility profile field cannot move between releases'
            USING ERRCODE = '23514';
    END IF;

    SELECT status INTO release_state
    FROM release
    WHERE release_id = target_release_id;

    IF release_state IS NOT NULL AND release_state <> 'STAGING' THEN
        RAISE EXCEPTION 'eligibility profile field catalog is frozen after release publication'
            USING ERRCODE = '23514';
    END IF;

    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END
$$;

CREATE TRIGGER trg_eligibility_profile_field_guard_insert
    BEFORE INSERT ON eligibility_profile_field
    FOR EACH ROW
    EXECUTE FUNCTION eligibility_profile_field_guard_snapshot();

CREATE TRIGGER trg_eligibility_profile_field_guard_update
    BEFORE UPDATE ON eligibility_profile_field
    FOR EACH ROW
    EXECUTE FUNCTION eligibility_profile_field_guard_snapshot();

CREATE TRIGGER trg_eligibility_profile_field_guard_delete
    BEFORE DELETE ON eligibility_profile_field
    FOR EACH ROW
    EXECUTE FUNCTION eligibility_profile_field_guard_snapshot();

CREATE OR REPLACE FUNCTION seed_eligibility_profile_fields()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM set_config(
        'app.eligibility_profile_field_seed_release',
        NEW.release_id,
        true
    );
    INSERT INTO eligibility_profile_field (
        release_id, field_key, value_type, allowed_values, description
    )
    SELECT
        NEW.release_id,
        field_key,
        value_type,
        allowed_values,
        description
    FROM eligibility_profile_field_template;
    PERFORM set_config('app.eligibility_profile_field_seed_release', '', true);
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_seed_eligibility_profile_fields
    AFTER INSERT ON release
    FOR EACH ROW
    EXECUTE FUNCTION seed_eligibility_profile_fields();

COMMENT ON TABLE eligibility_profile_field_template IS
    '新 release 的默认资格字段目录模板；发布后实际读取只使用 release 内快照';
COMMENT ON TABLE eligibility_profile_field IS
    'release-scoped 资格字段 allowlist 快照；发布后冻结';
