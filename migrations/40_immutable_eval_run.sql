-- 不可变 EvalRun：发布门禁只读取已持久化的结果，不接受调用方临时伪造 PASS。
CREATE TABLE eval_run_record (
    eval_run_id       TEXT PRIMARY KEY,
    result_sha256     CHAR(64) NOT NULL UNIQUE,
    dataset_version   TEXT NOT NULL,
    dataset_sha256    CHAR(64) NOT NULL,
    release_id        TEXT NOT NULL,
    git_sha           CHAR(40) NOT NULL,
    gate_status       VARCHAR(32) NOT NULL
        CHECK (gate_status IN ('PASS', 'FAIL', 'BLOCKED_PENDING_SIGNOFF')),
    payload           JSONB NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_eval_run_record_release
    ON eval_run_record(release_id, dataset_version);

CREATE OR REPLACE FUNCTION reject_eval_run_record_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'eval_run_record is immutable'
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER eval_run_record_no_update
BEFORE UPDATE ON eval_run_record
FOR EACH ROW EXECUTE FUNCTION reject_eval_run_record_mutation();

CREATE TRIGGER eval_run_record_no_delete
BEFORE DELETE ON eval_run_record
FOR EACH ROW EXECUTE FUNCTION reject_eval_run_record_mutation();
