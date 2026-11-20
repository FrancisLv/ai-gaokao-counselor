-- name: LockLogicalCallStatus :one
SELECT status
FROM llm_logical_call
WHERE logical_call_pk = $1::uuid
FOR UPDATE;

-- name: SettleBudget :execrows
UPDATE llm_budget_scope
SET provider_tokens_reserved = provider_tokens_reserved - $1,
    cost_reserved_micro_usd = cost_reserved_micro_usd - $2,
    provider_tokens_settled = provider_tokens_settled + $3,
    cost_settled_micro_usd = cost_settled_micro_usd + $4,
    updated_at = $5
WHERE budget_scope_id = (
    SELECT budget_scope_id
    FROM llm_logical_call
    WHERE logical_call_pk = $6::uuid
)
  AND provider_tokens_reserved >= $1
  AND cost_reserved_micro_usd >= $2;

-- name: MarkAttemptSucceeded :execrows
UPDATE llm_provider_attempt
SET status = 'SUCCEEDED',
    input_tokens = $1,
    output_tokens = $2,
    cost_micro_usd = $3,
    provider_request_id = NULLIF($4, ''),
    error_code = NULL,
    finished_at = $5
WHERE attempt_id = $6::uuid
  AND status = 'IN_PROGRESS';

-- name: MarkAttemptRejectedSucceeded :execrows
UPDATE llm_provider_attempt
SET status = 'SUCCEEDED',
    input_tokens = $1,
    output_tokens = $2,
    cost_micro_usd = $3,
    provider_request_id = NULLIF($4, ''),
    error_code = $5,
    finished_at = $6
WHERE attempt_id = $7::uuid
  AND status = 'IN_PROGRESS';

-- name: CompleteLogicalCall :execrows
UPDATE llm_logical_call
SET status = 'SUCCEEDED',
    actual_input_tokens = $1,
    actual_output_tokens = $2,
    actual_cost_micro_usd = $3,
    response_payload = $4,
    completed_at = $5,
    updated_at = $5
WHERE logical_call_pk = $6::uuid
  AND status = 'IN_PROGRESS';

-- name: RejectLogicalCall :execrows
UPDATE llm_logical_call
SET status = 'FAILED',
    actual_input_tokens = $1,
    actual_output_tokens = $2,
    actual_cost_micro_usd = $3,
    response_payload = NULL,
    error_reason = $4,
    completed_at = $5,
    updated_at = $5
WHERE logical_call_pk = $6::uuid
  AND status = 'IN_PROGRESS';

-- name: ReleaseBudgetReservation :execrows
UPDATE llm_budget_scope
SET provider_tokens_reserved = provider_tokens_reserved - $1,
    cost_reserved_micro_usd = cost_reserved_micro_usd - $2,
    updated_at = $3
WHERE budget_scope_id = (
    SELECT budget_scope_id
    FROM llm_logical_call
    WHERE logical_call_pk = $4::uuid
)
  AND provider_tokens_reserved >= $1
  AND cost_reserved_micro_usd >= $2;

-- name: MarkAttemptNoCharge :execrows
UPDATE llm_provider_attempt
SET status = 'FAILED_NO_CHARGE',
    cost_micro_usd = 0,
    provider_request_id = NULLIF($1, ''),
    error_code = $2,
    finished_at = $3
WHERE attempt_id = $4::uuid
  AND status = 'IN_PROGRESS';

-- name: FailLogicalCallWithoutCharge :execrows
UPDATE llm_logical_call
SET status = 'FAILED',
    error_reason = $1,
    completed_at = $2,
    updated_at = $2
WHERE logical_call_pk = $3::uuid
  AND status = 'IN_PROGRESS';

-- name: MarkBudgetScopeOverrun :execrows
UPDATE llm_budget_scope
SET budget_status = 'BUDGET_OVERRUN',
    overrun_at = COALESCE(overrun_at, $1),
    overrun_reason = $2,
    updated_at = $1
WHERE budget_scope_id = (
    SELECT budget_scope_id
    FROM llm_logical_call
    WHERE logical_call_pk = $3::uuid
);

-- name: MarkAttemptPendingReconciliation :execrows
UPDATE llm_provider_attempt
SET status = 'PENDING_RECONCILIATION',
    error_code = $1,
    finished_at = $2
WHERE attempt_id = $3::uuid
  AND status IN ('RESERVED', 'IN_PROGRESS');

-- name: MarkAttemptBudgetOverrun :execrows
UPDATE llm_provider_attempt
SET status = 'PENDING_RECONCILIATION',
    input_tokens = $1,
    output_tokens = $2,
    cost_micro_usd = $3,
    provider_request_id = NULLIF($4, ''),
    error_code = $5,
    finished_at = $6
WHERE attempt_id = $7::uuid
  AND status = 'IN_PROGRESS';

-- name: MarkLogicalCallBudgetOverrun :execrows
UPDATE llm_logical_call
SET status = 'BUDGET_OVERRUN',
    actual_input_tokens = $1,
    actual_output_tokens = $2,
    actual_cost_micro_usd = $3,
    response_payload = NULL,
    error_reason = $4,
    updated_at = $5
WHERE logical_call_pk = $6::uuid
  AND status = 'IN_PROGRESS';

-- name: MarkLogicalCallPartialUnknown :execrows
UPDATE llm_logical_call
SET status = 'PARTIAL_UNKNOWN',
    error_reason = $1,
    updated_at = $2
WHERE logical_call_pk = $3::uuid
  AND status = 'IN_PROGRESS';
