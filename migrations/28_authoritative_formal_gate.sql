-- M4c review fix：正式门禁绑定本 run 已生成的权威资格与配置 artifact。

CREATE TABLE IF NOT EXISTS run_formal_gate_artifact (
    run_id                     UUID PRIMARY KEY REFERENCES run(run_id),
    release_id                 TEXT NOT NULL REFERENCES release(release_id),
    confirmed_profile_version  UUID NOT NULL
                               REFERENCES profile_revision(profile_version),
    verifier_version           TEXT NOT NULL,
    applicable_as_of           DATE NOT NULL,
    algorithm_config_version   TEXT NOT NULL,
    algorithm_config_sha256    TEXT NOT NULL,
    artifact_sha256            TEXT NOT NULL,
    artifact_json              JSONB NOT NULL,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (verifier_version <> ''),
    CHECK (algorithm_config_version <> ''),
    CHECK (algorithm_config_sha256 ~ '^[0-9a-f]{64}$'),
    CHECK (artifact_sha256 ~ '^[0-9a-f]{64}$')
);

CREATE OR REPLACE FUNCTION validate_formal_gate_artifact()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    bound_run run%ROWTYPE;
BEGIN
    SELECT *
    INTO bound_run
    FROM run
    WHERE run_id = NEW.run_id;
    IF NOT FOUND
       OR bound_run.release_id <> NEW.release_id
       OR bound_run.confirmed_profile_version
          IS DISTINCT FROM NEW.confirmed_profile_version THEN
        RAISE EXCEPTION 'formal gate artifact identity mismatch';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_validate_run_formal_gate_artifact
    ON run_formal_gate_artifact;
CREATE TRIGGER trg_validate_run_formal_gate_artifact
    BEFORE INSERT ON run_formal_gate_artifact
    FOR EACH ROW
    EXECUTE FUNCTION validate_formal_gate_artifact();

DROP TRIGGER IF EXISTS trg_run_formal_gate_artifact_immutable_update
    ON run_formal_gate_artifact;
CREATE TRIGGER trg_run_formal_gate_artifact_immutable_update
    BEFORE UPDATE ON run_formal_gate_artifact
    FOR EACH ROW
    EXECUTE FUNCTION reject_immutable_formal_result();

DROP TRIGGER IF EXISTS trg_run_formal_gate_artifact_immutable_delete
    ON run_formal_gate_artifact;
CREATE TRIGGER trg_run_formal_gate_artifact_immutable_delete
    BEFORE DELETE ON run_formal_gate_artifact
    FOR EACH ROW
    EXECUTE FUNCTION reject_immutable_formal_result();

COMMENT ON TABLE run_formal_gate_artifact IS
    'run 已生成的权威资格/配置快照；正式发布只能消费该不可变 artifact';
