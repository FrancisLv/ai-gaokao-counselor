-- M4b: RuleVersion 内容不可变、生命周期审计与发布门禁。

ALTER TABLE rule_version
    ALTER COLUMN release_id DROP NOT NULL,
    ADD COLUMN IF NOT EXISTS created_by TEXT,
    ADD COLUMN IF NOT EXISTS published_by TEXT,
    ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ;

ALTER TABLE rule_version
    DROP CONSTRAINT IF EXISTS rule_version_published_metadata;
ALTER TABLE rule_version
    ADD CONSTRAINT rule_version_published_metadata
    CHECK (
        review_status <> 'PUBLISHED'
        OR (
            reviewed_by IS NOT NULL
            AND reviewed_at IS NOT NULL
            AND published_by IS NOT NULL
            AND published_at IS NOT NULL
            AND release_id IS NOT NULL
        )
    ) NOT VALID;

CREATE UNIQUE INDEX IF NOT EXISTS uq_rule_version_published_exact_scope
    ON rule_version(release_id, rule_key, scope)
    WHERE review_status = 'PUBLISHED';

CREATE TABLE IF NOT EXISTS rule_lifecycle_event (
    event_id       BIGSERIAL PRIMARY KEY,
    rule_id        TEXT NOT NULL,
    version        INTEGER NOT NULL,
    from_state     TEXT NOT NULL,
    to_state       TEXT NOT NULL,
    actor          TEXT NOT NULL CHECK (btrim(actor) <> ''),
    reason         TEXT NOT NULL DEFAULT '',
    occurred_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (rule_id, version)
        REFERENCES rule_version(rule_id, version)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_rule_lifecycle_event_rule
    ON rule_lifecycle_event(rule_id, version, event_id);

CREATE OR REPLACE FUNCTION eligibility_scope_overlaps(left_scope JSONB, right_scope JSONB)
RETURNS BOOLEAN
LANGUAGE SQL
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT NOT (
        (
            COALESCE(left_scope ->> 'province', '') <> ''
            AND COALESCE(right_scope ->> 'province', '') <> ''
            AND left_scope ->> 'province' <> right_scope ->> 'province'
        )
        OR (
            COALESCE(left_scope ->> 'application_year', '') <> ''
            AND COALESCE(right_scope ->> 'application_year', '') <> ''
            AND left_scope ->> 'application_year' <> right_scope ->> 'application_year'
        )
        OR (
            COALESCE(left_scope ->> 'category', '') <> ''
            AND COALESCE(right_scope ->> 'category', '') <> ''
            AND left_scope ->> 'category' <> right_scope ->> 'category'
        )
        OR (
            COALESCE(left_scope ->> 'batch', '') <> ''
            AND COALESCE(right_scope ->> 'batch', '') <> ''
            AND left_scope ->> 'batch' <> right_scope ->> 'batch'
        )
        OR (
            COALESCE(left_scope ->> 'university_id', '') <> ''
            AND COALESCE(right_scope ->> 'university_id', '') <> ''
            AND left_scope ->> 'university_id' <> right_scope ->> 'university_id'
        )
        OR (
            COALESCE(left_scope ->> 'major_group', '') <> ''
            AND COALESCE(right_scope ->> 'major_group', '') <> ''
            AND left_scope ->> 'major_group' <> right_scope ->> 'major_group'
        )
        OR (
            COALESCE(left_scope ->> 'major_code', '') <> ''
            AND COALESCE(right_scope ->> 'major_code', '') <> ''
            AND left_scope ->> 'major_code' <> right_scope ->> 'major_code'
        )
    );
$$;

CREATE OR REPLACE FUNCTION eligibility_scope_covers(container_scope JSONB, target_scope JSONB)
RETURNS BOOLEAN
LANGUAGE SQL
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT
        (COALESCE(container_scope ->> 'province', '') = '' OR container_scope ->> 'province' = target_scope ->> 'province')
        AND (COALESCE(container_scope ->> 'application_year', '') = '' OR container_scope ->> 'application_year' = target_scope ->> 'application_year')
        AND (COALESCE(container_scope ->> 'category', '') = '' OR container_scope ->> 'category' = target_scope ->> 'category')
        AND (COALESCE(container_scope ->> 'batch', '') = '' OR container_scope ->> 'batch' = target_scope ->> 'batch')
        AND (COALESCE(container_scope ->> 'university_id', '') = '' OR container_scope ->> 'university_id' = target_scope ->> 'university_id')
        AND (COALESCE(container_scope ->> 'major_group', '') = '' OR container_scope ->> 'major_group' = target_scope ->> 'major_group')
        AND (COALESCE(container_scope ->> 'major_code', '') = '' OR container_scope ->> 'major_code' = target_scope ->> 'major_code');
$$;

CREATE OR REPLACE FUNCTION rule_version_before_insert()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.review_status <> 'DRAFT' THEN
        RAISE EXCEPTION 'rule_version must start as DRAFT'
            USING ERRCODE = '23514';
    END IF;
    IF btrim(COALESCE(NEW.created_by, '')) = '' THEN
        RAISE EXCEPTION 'DRAFT rule requires created_by'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.reviewed_by IS NOT NULL
       OR NEW.reviewed_at IS NOT NULL
       OR NEW.published_by IS NOT NULL
       OR NEW.published_at IS NOT NULL
       OR NEW.revoked_by IS NOT NULL
       OR NEW.revoked_at IS NOT NULL
       OR btrim(COALESCE(NEW.revoke_reason, '')) <> '' THEN
        RAISE EXCEPTION 'DRAFT rule cannot carry review, publish or revoke metadata'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS trg_rule_version_before_insert ON rule_version;
CREATE TRIGGER trg_rule_version_before_insert
    BEFORE INSERT ON rule_version
    FOR EACH ROW
    EXECUTE FUNCTION rule_version_before_insert();

CREATE OR REPLACE FUNCTION rule_version_after_insert()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO rule_lifecycle_event (
        rule_id, version, from_state, to_state, actor, occurred_at
    ) VALUES (
        NEW.rule_id, NEW.version, 'NONE', NEW.review_status, NEW.created_by, clock_timestamp()
    );
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS trg_rule_version_after_insert ON rule_version;
CREATE TRIGGER trg_rule_version_after_insert
    AFTER INSERT ON rule_version
    FOR EACH ROW
    EXECUTE FUNCTION rule_version_after_insert();

CREATE OR REPLACE FUNCTION rule_version_before_update()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    transition_actor TEXT;
    conflict_exists BOOLEAN;
    coverage_exists BOOLEAN;
BEGIN
    IF NEW.rule_id IS DISTINCT FROM OLD.rule_id
       OR NEW.version IS DISTINCT FROM OLD.version
       OR NEW.rule_key IS DISTINCT FROM OLD.rule_key
       OR NEW.rule_type IS DISTINCT FROM OLD.rule_type
       OR NEW.scope IS DISTINCT FROM OLD.scope
       OR NEW.required_profile_fields IS DISTINCT FROM OLD.required_profile_fields
       OR NEW.expression IS DISTINCT FROM OLD.expression
       OR NEW.outcome_when_true IS DISTINCT FROM OLD.outcome_when_true
       OR NEW.reason_code IS DISTINCT FROM OLD.reason_code
       OR NEW.valid_from IS DISTINCT FROM OLD.valid_from
       OR NEW.valid_to IS DISTINCT FROM OLD.valid_to
       OR NEW.source_evidence IS DISTINCT FROM OLD.source_evidence
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.created_by IS DISTINCT FROM OLD.created_by THEN
        RAISE EXCEPTION 'rule_version content is immutable'
            USING ERRCODE = '23514';
    END IF;

    IF NEW.review_status = OLD.review_status THEN
        RAISE EXCEPTION 'rule lifecycle update must change review_status'
            USING ERRCODE = '23514';
    END IF;

    IF OLD.review_status = 'DRAFT' AND NEW.review_status = 'REVIEWED' THEN
        IF NEW.release_id IS DISTINCT FROM OLD.release_id THEN
            RAISE EXCEPTION 'REVIEWED cannot bind or change release_id'
                USING ERRCODE = '23514';
        END IF;
        IF btrim(COALESCE(NEW.reviewed_by, '')) = '' OR NEW.reviewed_at IS NULL THEN
            RAISE EXCEPTION 'REVIEWED requires reviewer and reviewed_at'
                USING ERRCODE = '23514';
        END IF;
        IF NEW.published_by IS NOT NULL
           OR NEW.published_at IS NOT NULL
           OR NEW.revoked_by IS NOT NULL
           OR NEW.revoked_at IS NOT NULL
           OR btrim(COALESCE(NEW.revoke_reason, '')) <> '' THEN
            RAISE EXCEPTION 'REVIEWED cannot carry publish or revoke metadata'
                USING ERRCODE = '23514';
        END IF;
        transition_actor := NEW.reviewed_by;

    ELSIF OLD.review_status = 'REVIEWED' AND NEW.review_status = 'PUBLISHED' THEN
        IF NEW.release_id IS NULL OR (OLD.release_id IS NOT NULL AND NEW.release_id <> OLD.release_id) THEN
            RAISE EXCEPTION 'PUBLISHED must atomically bind release_id'
                USING ERRCODE = '23514';
        END IF;
        IF jsonb_array_length(NEW.source_evidence) = 0
           OR EXISTS (
               SELECT 1
               FROM jsonb_array_elements(NEW.source_evidence) AS evidence
               WHERE COALESCE(evidence ->> 'evidence_id', '') = ''
                  OR evidence ->> 'release_id' IS DISTINCT FROM NEW.release_id
           ) THEN
            RAISE EXCEPTION 'published evidence must bind the same release_id'
                USING ERRCODE = '23514';
        END IF;
        IF NEW.reviewed_by IS DISTINCT FROM OLD.reviewed_by
           OR NEW.reviewed_at IS DISTINCT FROM OLD.reviewed_at THEN
            RAISE EXCEPTION 'publish cannot rewrite prior review metadata'
                USING ERRCODE = '23514';
        END IF;
        IF btrim(COALESCE(NEW.published_by, '')) = '' OR NEW.published_at IS NULL THEN
            RAISE EXCEPTION 'PUBLISHED requires publisher and published_at'
                USING ERRCODE = '23514';
        END IF;
        IF NEW.revoked_by IS NOT NULL
           OR NEW.revoked_at IS NOT NULL
           OR btrim(COALESCE(NEW.revoke_reason, '')) <> '' THEN
            RAISE EXCEPTION 'PUBLISHED cannot carry revoke metadata'
                USING ERRCODE = '23514';
        END IF;

        SELECT EXISTS (
            SELECT 1
            FROM rule_coverage coverage
            WHERE coverage.release_id = NEW.release_id
              AND coverage.rule_type = NEW.rule_type
              AND coverage.status IN ('FULL', 'PARTIAL')
              AND eligibility_scope_covers(coverage.scope, NEW.scope)
        ) INTO coverage_exists;
        IF NOT coverage_exists THEN
            RAISE EXCEPTION 'PUBLISHED requires applicable RuleCoverage'
                USING ERRCODE = '23514';
        END IF;

        SELECT EXISTS (
            SELECT 1
            FROM rule_version existing
            WHERE existing.release_id = NEW.release_id
              AND existing.rule_key = NEW.rule_key
              AND existing.review_status = 'PUBLISHED'
              AND (existing.rule_id <> NEW.rule_id OR existing.version <> NEW.version)
              AND eligibility_scope_overlaps(existing.scope, NEW.scope)
        ) INTO conflict_exists;
        IF conflict_exists THEN
            RAISE EXCEPTION 'RULE_CONFLICT'
                USING ERRCODE = '23505';
        END IF;
        transition_actor := NEW.published_by;

    ELSIF OLD.review_status = 'PUBLISHED' AND NEW.review_status = 'REVOKED' THEN
        IF NEW.release_id IS DISTINCT FROM OLD.release_id THEN
            RAISE EXCEPTION 'revoke cannot change release_id'
                USING ERRCODE = '23514';
        END IF;
        IF NEW.reviewed_by IS DISTINCT FROM OLD.reviewed_by
           OR NEW.reviewed_at IS DISTINCT FROM OLD.reviewed_at
           OR NEW.published_by IS DISTINCT FROM OLD.published_by
           OR NEW.published_at IS DISTINCT FROM OLD.published_at THEN
            RAISE EXCEPTION 'revoke cannot rewrite review or publish metadata'
                USING ERRCODE = '23514';
        END IF;
        IF btrim(COALESCE(NEW.revoked_by, '')) = ''
           OR NEW.revoked_at IS NULL
           OR btrim(COALESCE(NEW.revoke_reason, '')) = '' THEN
            RAISE EXCEPTION 'REVOKED requires actor, timestamp and reason'
                USING ERRCODE = '23514';
        END IF;
        transition_actor := NEW.revoked_by;

    ELSE
        RAISE EXCEPTION 'illegal rule lifecycle transition % -> %', OLD.review_status, NEW.review_status
            USING ERRCODE = '23514';
    END IF;

    INSERT INTO rule_lifecycle_event (
        rule_id, version, from_state, to_state, actor, reason, occurred_at
    ) VALUES (
        NEW.rule_id, NEW.version, OLD.review_status, NEW.review_status,
        transition_actor, COALESCE(NEW.revoke_reason, ''), clock_timestamp()
    );
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS trg_rule_version_before_update ON rule_version;
CREATE TRIGGER trg_rule_version_before_update
    BEFORE UPDATE ON rule_version
    FOR EACH ROW
    EXECUTE FUNCTION rule_version_before_update();

COMMENT ON TABLE rule_lifecycle_event IS
    'RuleVersion 状态变化的追加式审计记录；内容修改仍必须创建新版本';
