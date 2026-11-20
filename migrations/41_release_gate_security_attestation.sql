-- 安全测试 attestation：只保存受控 runner 的结果身份与结论，不接受调用方自由 JSON。
CREATE TABLE release_gate_security_record (
    artifact_id       TEXT PRIMARY KEY,
    artifact_sha256   CHAR(64) NOT NULL UNIQUE,
    eval_run_id       TEXT NOT NULL REFERENCES eval_run_record(eval_run_id),
    dataset_version   TEXT NOT NULL,
    dataset_sha256    CHAR(64) NOT NULL,
    release_id        TEXT NOT NULL,
    git_sha           CHAR(40) NOT NULL,
    gate_id           TEXT NOT NULL
        CHECK (gate_id IN ('CONSENT', 'DELETION', 'RETENTION', 'RESTORE')),
    passed            BOOLEAN NOT NULL,
    suite             TEXT NOT NULL,
    payload           JSONB NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX uq_release_gate_security_once
    ON release_gate_security_record(eval_run_id, gate_id);

CREATE INDEX idx_release_gate_security_identity
    ON release_gate_security_record(
        eval_run_id, release_id, dataset_version, git_sha
    );

CREATE OR REPLACE FUNCTION reject_release_gate_security_record_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'release_gate_security_record is immutable'
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER release_gate_security_no_update
BEFORE UPDATE ON release_gate_security_record
FOR EACH ROW EXECUTE FUNCTION reject_release_gate_security_record_mutation();

CREATE TRIGGER release_gate_security_no_delete
BEFORE DELETE ON release_gate_security_record
FOR EACH ROW EXECUTE FUNCTION reject_release_gate_security_record_mutation();
