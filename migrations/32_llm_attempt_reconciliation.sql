-- M5a-04：未知计费 attempt 的追加式对账依据。
-- 不改历史迁移；对账只允许一次受控状态转换，审计行发布后不可改写。

ALTER TABLE llm_logical_call
    ADD COLUMN reserved_provider_tokens BIGINT
        CHECK (reserved_provider_tokens >= 0);

COMMENT ON COLUMN llm_logical_call.reserved_provider_tokens IS
    '首次受理时冻结的 token 预占总额（含全部允许 attempt）；旧行保持 NULL，不回填猜测值';

CREATE TABLE llm_attempt_reconciliation (
    reconciliation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    attempt_id UUID NOT NULL UNIQUE
        REFERENCES llm_provider_attempt(attempt_id),
    logical_call_pk UUID NOT NULL,
    logical_call_id TEXT NOT NULL CHECK (btrim(logical_call_id) <> ''),
    outcome TEXT NOT NULL
        CHECK (outcome IN ('CHARGED', 'NO_CHARGE', 'CHARGED_OVERRUN')),
    previous_attempt_status TEXT NOT NULL
        CHECK (previous_attempt_status = 'PENDING_RECONCILIATION'),
    previous_error_code TEXT,
    previous_reserved_tokens BIGINT NOT NULL
        CHECK (previous_reserved_tokens >= 0),
    previous_reserved_cost_micro_usd BIGINT NOT NULL
        CHECK (previous_reserved_cost_micro_usd >= 0),
    input_tokens BIGINT CHECK (input_tokens >= 0),
    output_tokens BIGINT CHECK (output_tokens >= 0),
    cost_micro_usd BIGINT CHECK (cost_micro_usd >= 0),
    evidence_ref TEXT NOT NULL CHECK (btrim(evidence_ref) <> ''),
    reconciled_at TIMESTAMPTZ NOT NULL,
    CHECK (
        (outcome IN ('CHARGED', 'CHARGED_OVERRUN')
         AND input_tokens IS NOT NULL
         AND output_tokens IS NOT NULL
         AND cost_micro_usd IS NOT NULL)
        OR
        (outcome = 'NO_CHARGE'
         AND input_tokens IS NULL
         AND output_tokens IS NULL
         AND cost_micro_usd IS NULL)
    ),
    FOREIGN KEY (logical_call_pk, logical_call_id)
        REFERENCES llm_logical_call(logical_call_pk, logical_call_id)
);

CREATE INDEX idx_llm_attempt_reconciliation_logical
    ON llm_attempt_reconciliation(logical_call_id, reconciled_at DESC);

CREATE OR REPLACE FUNCTION reject_llm_reconciliation_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'LLM reconciliation audit rows are immutable'
        USING ERRCODE = '23514';
END
$$;

CREATE TRIGGER trg_llm_attempt_reconciliation_immutable
    BEFORE UPDATE OR DELETE ON llm_attempt_reconciliation
    FOR EACH ROW
    EXECUTE FUNCTION reject_llm_reconciliation_mutation();

COMMENT ON TABLE llm_attempt_reconciliation IS
    '未知计费 attempt 的一次性对账结果；保留原状态、错误和预留依据';
COMMENT ON COLUMN llm_attempt_reconciliation.evidence_ref IS
    '明确供应商账单行或未受理证明的内部引用，不得存供应商原始正文';
