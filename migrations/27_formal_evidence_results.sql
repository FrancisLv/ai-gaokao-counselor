-- M4c-06：V1_FORMAL 结果、不可变证据包/验证报告与结果状态。

ALTER TABLE run_result
    ADD COLUMN IF NOT EXISTS source_package_sha256 TEXT,
    ADD COLUMN IF NOT EXISTS evidence_package_sha256 TEXT,
    ADD COLUMN IF NOT EXISTS verification_report_sha256 TEXT;

ALTER TABLE run_result
    DROP CONSTRAINT IF EXISTS run_result_result_kind_check;
ALTER TABLE run_result
    ADD CONSTRAINT run_result_result_kind_check
    CHECK (
        result_kind IN (
            'ALGORITHM_PREVIEW',
            'POLICY_EVIDENCE',
            'V1_FORMAL'
        )
    );

ALTER TABLE run_result
    DROP CONSTRAINT IF EXISTS run_result_kind_profile_check;
ALTER TABLE run_result
    ADD CONSTRAINT run_result_kind_profile_check
    CHECK (
        (
            result_kind IN ('ALGORITHM_PREVIEW', 'V1_FORMAL')
            AND confirmed_profile_version IS NOT NULL
        )
        OR (
            result_kind = 'POLICY_EVIDENCE'
            AND confirmed_profile_version IS NULL
        )
    );

ALTER TABLE run_result
    ADD CONSTRAINT run_result_formal_identity_check
    CHECK (
        result_kind <> 'V1_FORMAL'
        OR (
            source_package_sha256 IS NOT NULL
            AND evidence_package_sha256 IS NOT NULL
            AND verification_report_sha256 IS NOT NULL
        )
    );

CREATE TABLE IF NOT EXISTS run_evidence_package (
    result_version             UUID PRIMARY KEY
                               REFERENCES run_result(result_version),
    run_id                     UUID NOT NULL REFERENCES run(run_id),
    release_id                 TEXT NOT NULL REFERENCES release(release_id),
    confirmed_profile_version  UUID NOT NULL
                               REFERENCES profile_revision(profile_version),
    result_kind                TEXT NOT NULL
                               CHECK (result_kind = 'V1_FORMAL'),
    source_package_sha256      TEXT NOT NULL,
    package_sha256             TEXT NOT NULL,
    package_json               JSONB NOT NULL,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS run_verification_report (
    result_version       UUID PRIMARY KEY
                         REFERENCES run_result(result_version),
    run_id               UUID NOT NULL REFERENCES run(run_id),
    status               TEXT NOT NULL
                         CHECK (status IN ('PASS', 'PARTIAL')),
    report_sha256        TEXT NOT NULL,
    report_json          JSONB NOT NULL,
    stripped_claim_ids   JSONB NOT NULL
                         CHECK (jsonb_typeof(stripped_claim_ids) = 'array'),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS run_result_state_event (
    event_id         BIGSERIAL PRIMARY KEY,
    result_version   UUID NOT NULL REFERENCES run_result(result_version),
    status           TEXT NOT NULL CHECK (status IN ('ACTIVE', 'STALE')),
    reason           TEXT,
    occurred_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_run_result_state_latest
    ON run_result_state_event(result_version, event_id DESC);

CREATE OR REPLACE FUNCTION validate_formal_evidence_binding()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    parent_result run_result%ROWTYPE;
BEGIN
    SELECT *
    INTO parent_result
    FROM run_result
    WHERE result_version = NEW.result_version;
    IF NOT FOUND
       OR parent_result.result_kind <> 'V1_FORMAL'
       OR parent_result.run_id <> NEW.run_id THEN
        RAISE EXCEPTION 'formal evidence must bind its parent result';
    END IF;
    IF TG_TABLE_NAME = 'run_evidence_package' THEN
        IF parent_result.release_id <> NEW.release_id
           OR parent_result.confirmed_profile_version
              <> NEW.confirmed_profile_version
           OR parent_result.evidence_package_sha256
              <> NEW.package_sha256 THEN
            RAISE EXCEPTION 'evidence package identity mismatch';
        END IF;
    ELSIF TG_TABLE_NAME = 'run_verification_report' THEN
        IF parent_result.verification_report_sha256
           <> NEW.report_sha256 THEN
            RAISE EXCEPTION 'verification report identity mismatch';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_validate_run_evidence_package
    ON run_evidence_package;
CREATE TRIGGER trg_validate_run_evidence_package
    BEFORE INSERT ON run_evidence_package
    FOR EACH ROW
    EXECUTE FUNCTION validate_formal_evidence_binding();

DROP TRIGGER IF EXISTS trg_validate_run_verification_report
    ON run_verification_report;
CREATE TRIGGER trg_validate_run_verification_report
    BEFORE INSERT ON run_verification_report
    FOR EACH ROW
    EXECUTE FUNCTION validate_formal_evidence_binding();

CREATE OR REPLACE FUNCTION reject_run_result_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    target_kind TEXT;
BEGIN
    IF TG_OP = 'DELETE' THEN
        target_kind := OLD.result_kind;
    ELSE
        target_kind := NEW.result_kind;
    END IF;
    IF target_kind <> 'V1_FORMAL' THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'run_result is immutable';
END;
$$;

DROP TRIGGER IF EXISTS trg_run_result_immutable_update ON run_result;
CREATE TRIGGER trg_run_result_immutable_update
    BEFORE UPDATE ON run_result
    FOR EACH ROW
    EXECUTE FUNCTION reject_run_result_mutation();

DROP TRIGGER IF EXISTS trg_run_result_immutable_delete ON run_result;
CREATE TRIGGER trg_run_result_immutable_delete
    BEFORE DELETE ON run_result
    FOR EACH ROW
    EXECUTE FUNCTION reject_run_result_mutation();

CREATE OR REPLACE FUNCTION reject_immutable_formal_result()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'formal evidence result is immutable';
END;
$$;

DROP TRIGGER IF EXISTS trg_run_evidence_package_immutable_update
    ON run_evidence_package;
CREATE TRIGGER trg_run_evidence_package_immutable_update
    BEFORE UPDATE ON run_evidence_package
    FOR EACH ROW
    EXECUTE FUNCTION reject_immutable_formal_result();

DROP TRIGGER IF EXISTS trg_run_evidence_package_immutable_delete
    ON run_evidence_package;
CREATE TRIGGER trg_run_evidence_package_immutable_delete
    BEFORE DELETE ON run_evidence_package
    FOR EACH ROW
    EXECUTE FUNCTION reject_immutable_formal_result();

DROP TRIGGER IF EXISTS trg_run_verification_report_immutable_update
    ON run_verification_report;
CREATE TRIGGER trg_run_verification_report_immutable_update
    BEFORE UPDATE ON run_verification_report
    FOR EACH ROW
    EXECUTE FUNCTION reject_immutable_formal_result();

DROP TRIGGER IF EXISTS trg_run_verification_report_immutable_delete
    ON run_verification_report;
CREATE TRIGGER trg_run_verification_report_immutable_delete
    BEFORE DELETE ON run_verification_report
    FOR EACH ROW
    EXECUTE FUNCTION reject_immutable_formal_result();

DROP TRIGGER IF EXISTS trg_run_result_state_event_immutable_update
    ON run_result_state_event;
CREATE TRIGGER trg_run_result_state_event_immutable_update
    BEFORE UPDATE ON run_result_state_event
    FOR EACH ROW
    EXECUTE FUNCTION reject_immutable_formal_result();

DROP TRIGGER IF EXISTS trg_run_result_state_event_immutable_delete
    ON run_result_state_event;
CREATE TRIGGER trg_run_result_state_event_immutable_delete
    BEFORE DELETE ON run_result_state_event
    FOR EACH ROW
    EXECUTE FUNCTION reject_immutable_formal_result();

CREATE OR REPLACE FUNCTION mark_formal_results_stale_on_release_revoke()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF (
        NEW.status = 'REVOKED'
        AND OLD.status IS DISTINCT FROM 'REVOKED'
    ) OR (
        NEW.revoked_at IS NOT NULL
        AND OLD.revoked_at IS NULL
    ) THEN
        INSERT INTO run_result_state_event (
            result_version, status, reason
        )
        SELECT result.result_version, 'STALE', 'RELEASE_REVOKED'
        FROM run_result result
        WHERE result.release_id = NEW.release_id
          AND result.result_kind = 'V1_FORMAL'
          AND NOT EXISTS (
              SELECT 1
              FROM run_result_state_event state
              WHERE state.result_version = result.result_version
                AND state.status = 'STALE'
          );
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_mark_formal_results_stale_on_release_revoke
    ON release;
CREATE TRIGGER trg_mark_formal_results_stale_on_release_revoke
    AFTER UPDATE OF status, revoked_at ON release
    FOR EACH ROW
    EXECUTE FUNCTION mark_formal_results_stale_on_release_revoke();

COMMENT ON TABLE run_evidence_package IS
    'V1_FORMAL 发布的不可变、已剥离证据包正文';
COMMENT ON TABLE run_verification_report IS
    'V1_FORMAL 发布的不可变 VerifyPackage 报告';
COMMENT ON TABLE run_result_state_event IS
    '结果 ACTIVE/STALE 追加式状态；release 撤销不修改原结果正文';
