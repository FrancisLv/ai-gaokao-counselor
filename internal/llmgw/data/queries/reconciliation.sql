-- name: LockReconciliationRow :one
SELECT call.logical_call_pk::text,
       call.logical_call_id,
       call.ledger_entry_id::text,
       call.status AS logical_status,
       call.reserved_cost_micro_usd AS logical_reserved_cost,
       COALESCE(call.reserved_provider_tokens, -1) AS logical_reserved_tokens,
       call.budget_scope_id::text,
       attempt.status AS attempt_status,
       attempt.attempt_no,
       attempt.reserved_cost_micro_usd AS attempt_reserved_cost,
       attempt.reserved_tokens AS attempt_reserved_tokens,
       COALESCE(attempt.error_code, '') AS attempt_error_code,
       scope.budget_status,
       price.input_micro_usd_per_million_tokens AS input_rate,
       price.output_micro_usd_per_million_tokens AS output_rate
FROM llm_provider_attempt attempt
JOIN llm_logical_call call
  ON call.logical_call_pk = attempt.logical_call_pk
JOIN llm_budget_scope scope
  ON scope.budget_scope_id = call.budget_scope_id
JOIN llm_price_entry price
  ON price.price_table_version = call.price_table_version
 AND price.model_id = attempt.model_id
WHERE call.logical_call_id = $1
  AND attempt.attempt_id = $2::uuid
FOR UPDATE OF attempt, call, scope;

-- name: GetExistingReconciliation :one
SELECT outcome,
       input_tokens,
       output_tokens,
       cost_micro_usd,
       evidence_ref
FROM llm_attempt_reconciliation
WHERE attempt_id = $1::uuid;

-- name: ReleaseBudgetForReconciliation :execrows
UPDATE llm_budget_scope
SET provider_tokens_reserved = provider_tokens_reserved - $1,
    cost_reserved_micro_usd = cost_reserved_micro_usd - $2,
    provider_tokens_settled = provider_tokens_settled + $3,
    cost_settled_micro_usd = cost_settled_micro_usd + $4,
    updated_at = $5
WHERE budget_scope_id = $6::uuid
  AND provider_tokens_reserved >= $1
  AND cost_reserved_micro_usd >= $2;

-- name: LockBudgetScopeOverrun :execrows
UPDATE llm_budget_scope
SET budget_status = 'BUDGET_OVERRUN',
    overrun_at = COALESCE(overrun_at, $1),
    overrun_reason = 'RECONCILIATION_EXCEEDS_RESERVATION',
    updated_at = $1
WHERE budget_scope_id = $2::uuid;

-- name: UpdateReconciledAttempt :execrows
UPDATE llm_provider_attempt
SET status = $1,
    input_tokens = $2,
    output_tokens = $3,
    cost_micro_usd = $4,
    error_code = $5
WHERE attempt_id = $6::uuid
  AND status = 'PENDING_RECONCILIATION';

-- name: UpdateReconciledLogicalCall :execrows
UPDATE llm_logical_call
SET status = $1,
    actual_input_tokens = $2,
    actual_output_tokens = $3,
    actual_cost_micro_usd = $4,
    error_reason = $5,
    completed_at = $6,
    updated_at = $7
WHERE logical_call_pk = $8::uuid
  AND status = 'PARTIAL_UNKNOWN';

-- name: InsertReconciliationAudit :exec
INSERT INTO llm_attempt_reconciliation (
    attempt_id,
    logical_call_pk,
    logical_call_id,
    outcome,
    previous_attempt_status,
    previous_error_code,
    previous_reserved_tokens,
    previous_reserved_cost_micro_usd,
    input_tokens,
    output_tokens,
    cost_micro_usd,
    evidence_ref,
    reconciled_at
)
VALUES (
    $1::uuid,
    $2::uuid,
    $3,
    $4,
    $5,
    NULLIF($6, ''),
    $7,
    $8,
    $9,
    $10,
    $11,
    $12,
    $13
);
