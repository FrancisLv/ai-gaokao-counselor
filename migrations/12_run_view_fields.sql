-- RunView 暴露结果/检查点保留截止时间。
ALTER TABLE run ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

UPDATE run
SET expires_at = updated_at + INTERVAL '30 days'
WHERE expires_at IS NULL;

ALTER TABLE run ALTER COLUMN expires_at SET DEFAULT (NOW() + INTERVAL '30 days');
ALTER TABLE run ALTER COLUMN expires_at SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_run_expires_at
    ON run(expires_at);
