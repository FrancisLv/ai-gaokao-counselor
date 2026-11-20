-- name: InsertCacheClaim :execrows
INSERT INTO llm_result_cache (
    cache_key_hash,
    cache_schema_version,
    release_id,
    role,
    route_version,
    model_id,
    prompt_template_key,
    prompt_template_version,
    prompt_template_sha256,
    response_schema_key,
    response_schema_version,
    response_schema_sha256,
    data_classification,
    max_output_tokens,
    temperature,
    request_key_manifest,
    status,
    claim_token,
    claim_started_at,
    claim_expires_at
)
VALUES (
    $1,
    $2,
    $3,
    $4,
    $5,
    $6,
    $7,
    $8,
    $9,
    $10,
    $11,
    $12,
    $13,
    $14,
    0,
    $15::jsonb,
    'IN_PROGRESS',
    $16::uuid,
    $17,
    $18
)
ON CONFLICT (cache_key_hash) DO NOTHING;

-- name: GetCacheEntry :one
SELECT release_id,
       role,
       route_version,
       model_id,
       prompt_template_key,
       prompt_template_version,
       prompt_template_sha256,
       response_schema_key,
       response_schema_version,
       response_schema_sha256,
       data_classification,
       max_output_tokens,
       temperature,
       status,
       claim_token::text,
       claim_expires_at,
       result_payload
FROM llm_result_cache
WHERE cache_key_hash = $1;

-- name: TakeOverCacheClaim :execrows
UPDATE llm_result_cache
SET cache_schema_version = $2,
    release_id = $3,
    role = $4,
    route_version = $5,
    model_id = $6,
    prompt_template_key = $7,
    prompt_template_version = $8,
    prompt_template_sha256 = $9,
    response_schema_key = $10,
    response_schema_version = $11,
    response_schema_sha256 = $12,
    data_classification = $13,
    max_output_tokens = $14,
    temperature = 0,
    request_key_manifest = $15::jsonb,
    status = 'IN_PROGRESS',
    claim_token = $16::uuid,
    claim_started_at = $17,
    claim_expires_at = $18,
    result_payload = NULL,
    invalidation_reason = NULL,
    invalidated_at = NULL,
    completed_at = NULL,
    updated_at = $17
WHERE cache_key_hash = $1
  AND (
      status IN ('FAILED', 'INVALIDATED')
      OR (status = 'IN_PROGRESS' AND claim_expires_at <= $17)
  );

-- name: LockBudgetScopeForCacheHit :one
SELECT budget_scope_id::text,
       budget_status
FROM llm_budget_scope
WHERE principal_id = $1
  AND session_id = $2::uuid
FOR SHARE;

-- name: InsertCacheHitLogicalCall :execrows
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
    actual_input_tokens,
    actual_output_tokens,
    actual_cost_micro_usd,
    response_payload,
    prompt_template_key,
    prompt_template_version,
    prompt_template_sha256,
    response_schema_key,
    response_schema_version,
    response_schema_sha256,
    cache_hit,
    cache_key_hash,
    completed_at
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
    'SUCCEEDED',
    $14,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    $15,
    $16,
    $17,
    $18,
    $19,
    $20,
    $21,
    TRUE,
    $22,
    $23
);

-- name: MarkCacheReady :execrows
UPDATE llm_result_cache
SET status = 'READY',
    result_payload = $1::jsonb,
    completed_at = $2,
    updated_at = $2
WHERE cache_key_hash = $3
  AND status = 'IN_PROGRESS'
  AND claim_token = $4::uuid;

-- name: MarkCacheFailed :execrows
UPDATE llm_result_cache
SET status = 'FAILED',
    result_payload = NULL,
    completed_at = $1,
    updated_at = $1
WHERE cache_key_hash = $2
  AND status = 'IN_PROGRESS'
  AND claim_token = $3::uuid;
