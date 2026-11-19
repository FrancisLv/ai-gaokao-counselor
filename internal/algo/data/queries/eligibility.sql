-- name: GetEligibilityRelease :one
SELECT status
FROM release
WHERE release_id = @release_id::text;

-- name: EligibilityReleaseSupportsScope :one
SELECT EXISTS (
    SELECT 1
    FROM release_scope
    WHERE release_id = @release_id::text
      AND province = @province::text
      AND application_year = @application_year::int
      AND category = @category::text
      AND batch = @batch::text
);

-- name: ListPublishedEligibilityRules :many
SELECT
    rule_id,
    version,
    rule_key,
    rule_type,
    scope,
    required_profile_fields,
    expression,
    expression_version,
    outcome_when_true,
    reason_code,
    review_status,
    release_id,
    source_evidence
FROM rule_version
WHERE release_id = @release_id::text
  AND review_status = 'PUBLISHED'
  AND valid_from <= NOW()
  AND (valid_to IS NULL OR valid_to > NOW())
ORDER BY rule_key, rule_id, version;

-- name: ListEligibilityCoverage :many
SELECT
    rule_type,
    scope,
    status,
    covered_data_sources,
    notes
FROM rule_coverage
WHERE release_id = @release_id::text
ORDER BY rule_type, scope::text, status;

-- name: ListEligibilityProfileFields :many
SELECT
    field_key,
    value_type,
    allowed_values
FROM eligibility_profile_field
WHERE release_id = @release_id::text
ORDER BY field_key;

-- name: ListAllEligibilityProfileFields :many
SELECT
    release_id,
    field_key,
    value_type,
    allowed_values
FROM eligibility_profile_field
ORDER BY field_key, release_id;

-- name: ListRevokedEligibilityRules :many
SELECT rule_key, rule_type, scope
FROM rule_version
WHERE release_id = @release_id::text
  AND review_status = 'REVOKED'
ORDER BY rule_key, rule_type, scope::text;
