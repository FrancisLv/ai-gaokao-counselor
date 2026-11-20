-- name: InsertBudgetScopeIfAbsent :exec
INSERT INTO llm_budget_scope (budget_scope_id, principal_id, session_id)
VALUES ($1::uuid, $2, $3::uuid)
ON CONFLICT (principal_id, session_id) DO NOTHING;

-- name: GetBudgetScopeBySecurityScope :one
SELECT budget_scope_id::text,
       budget_status
FROM llm_budget_scope
WHERE principal_id = $1
  AND session_id = $2::uuid;

-- name: InsertLogicalCall :exec
INSERT INTO llm_logical_call (
    logical_call_pk,
    ledger_entry_id,
    logical_call_id,
    principal_id,
    session_id,
    run_id,
    role,
    release_id,
    route_version,
    price_table_version,
    model_id,
    request_hash,
    budget_scope_id,
    status,
    data_classification,
    reserved_input_tokens,
    reserved_output_tokens,
    reserved_cost_micro_usd,
    reserved_provider_tokens,
    prompt_template_key,
    prompt_template_version,
    prompt_template_sha256,
    response_schema_key,
    response_schema_version,
    response_schema_sha256
)
VALUES (
    $1::uuid,
    $2::uuid,
    $3,
    $4,
    $5::uuid,
    $6::uuid,
    $7,
    $8,
    $9,
    $10,
    $11,
    $12,
    $13::uuid,
    'IN_PROGRESS',
    $14,
    $15,
    $16,
    $17,
    $18,
    $19,
    $20,
    $21,
    $22,
    $23,
    $24
);

-- name: ReserveBudget :execrows
UPDATE llm_budget_scope
SET provider_tokens_reserved = provider_tokens_reserved + $1,
    cost_reserved_micro_usd = cost_reserved_micro_usd + $2,
    updated_at = NOW()
WHERE budget_scope_id = $3::uuid
  AND budget_status = 'ACTIVE'
  AND provider_tokens_settled + provider_tokens_reserved + $1 <= token_limit
  AND cost_settled_micro_usd + cost_reserved_micro_usd + $2 <= cost_limit_micro_usd;

-- name: GetBudgetStatusByID :one
SELECT budget_status
FROM llm_budget_scope
WHERE budget_scope_id = $1::uuid;

-- name: InsertFirstAttempt :exec
INSERT INTO llm_provider_attempt (
    attempt_id,
    logical_call_pk,
    logical_call_id,
    attempt_no,
    model_id,
    provider,
    status,
    reserved_tokens,
    reserved_cost_micro_usd,
    started_at
)
VALUES (
    $1::uuid,
    $2::uuid,
    $3,
    1,
    $4,
    $5,
    'IN_PROGRESS',
    $6,
    $7,
    $8
);

-- name: InsertAttempt :exec
INSERT INTO llm_provider_attempt (
    attempt_id,
    logical_call_pk,
    logical_call_id,
    attempt_no,
    model_id,
    provider,
    status,
    reserved_tokens,
    reserved_cost_micro_usd,
    started_at
)
VALUES (
    $1::uuid,
    $2::uuid,
    $3,
    $4,
    $5,
    $6,
    'IN_PROGRESS',
    $7,
    $8,
    $9
);

-- name: FinishRetryableAttempt :execrows
UPDATE llm_provider_attempt
SET status = 'FAILED_NO_CHARGE',
    cost_micro_usd = 0,
    provider_request_id = NULLIF($1, ''),
    error_code = $2,
    finished_at = $3
WHERE attempt_id = $4::uuid
  AND status = 'IN_PROGRESS';
