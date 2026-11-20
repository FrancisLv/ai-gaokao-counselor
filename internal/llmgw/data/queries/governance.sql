-- name: GetRunBinding :one
SELECT release.status AS release_status,
       run.active_execution_remaining_ms
FROM run
JOIN chat_session session ON session.id = run.session_id
JOIN release ON release.release_id = run.release_id
WHERE run.run_id = $1::uuid
  AND run.session_id = $2::uuid
  AND run.principal_id = $3
  AND session.principal_id = $3
  AND run.release_id = $4;

-- name: GetRoute :one
SELECT route.route_version,
       route.primary_model_id,
       route.fallback_model_ids,
       route.allowed_data_classes,
       route.max_output_tokens,
       route.timeout_ms,
       route.max_attempts,
       route.allow_result_cache,
       route.requires_response_schema,
       route.price_table_version,
       price.effective_at,
       price.published_at
FROM llm_role_route route
JOIN llm_price_table price
  ON price.price_table_version = route.price_table_version
WHERE route.release_id = $1
  AND route.role = $2;

-- name: GetModelPrice :one
SELECT allow.provider,
       price.input_micro_usd_per_million_tokens,
       price.output_micro_usd_per_million_tokens
FROM llm_model_allowlist allow
JOIN llm_price_entry price
  ON price.model_id = allow.model_id
 AND price.price_table_version = $3
WHERE allow.release_id = $1
  AND allow.model_id = $2
  AND allow.enabled;

-- name: ModelAllowedExists :one
SELECT EXISTS (
    SELECT 1
    FROM llm_model_allowlist
    WHERE release_id = $1
      AND model_id = $2
      AND enabled
);

-- name: GetPromptTemplate :one
SELECT prompt_template_key,
       prompt_template_version,
       system_template,
       user_template,
       template_sha256,
       status
FROM llm_prompt_template
WHERE release_id = $1
  AND prompt_template_key = $2
  AND prompt_template_version = $3;

-- name: GetResponseSchema :one
SELECT response_schema_key,
       response_schema_version,
       schema_json::text AS schema_document,
       schema_sha256,
       status
FROM llm_response_schema
WHERE release_id = $1
  AND response_schema_key = $2
  AND response_schema_version = $3;

-- name: GetBudgetStatusBySecurityScope :one
SELECT budget_status
FROM llm_budget_scope
WHERE principal_id = $1
  AND session_id = $2::uuid;

-- name: GetLogicalCallForReplay :one
SELECT logical_call_pk::text,
       ledger_entry_id::text,
       request_hash,
       status,
       error_reason,
       route_version,
       price_table_version,
       model_id,
       prompt_template_key,
       prompt_template_version,
       prompt_template_sha256,
       response_schema_key,
       response_schema_version,
       response_schema_sha256,
       reserved_cost_micro_usd,
       COALESCE(
           reserved_provider_tokens,
           reserved_input_tokens + reserved_output_tokens
       ) AS reserved_provider_tokens,
       actual_input_tokens,
       actual_output_tokens,
       actual_cost_micro_usd,
       response_payload,
       cache_hit,
       cache_key_hash,
       COALESCE((
           SELECT attempt.attempt_id::text
           FROM llm_provider_attempt attempt
           WHERE attempt.logical_call_pk = call.logical_call_pk
           ORDER BY attempt.attempt_no DESC
           LIMIT 1
       ), '')::text AS attempt_id
FROM llm_logical_call call
WHERE call.principal_id = $1
  AND call.session_id = $2::uuid
  AND call.logical_call_id = $3;

-- name: ListAttemptSummaries :many
SELECT attempt_id::text,
       model_id,
       provider,
       status,
       input_tokens,
       output_tokens,
       cost_micro_usd,
       provider_request_id,
       error_code,
       started_at,
       finished_at
FROM llm_provider_attempt
WHERE logical_call_pk = $1::uuid
ORDER BY attempt_no;
