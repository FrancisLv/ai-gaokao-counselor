-- name: GetEligibilityRelease :one
SELECT status
FROM release
WHERE release_id = @release_id::text;

-- name: ListPublishedEligibilityRules :many
SELECT
    rule_id,
    version,
    rule_key,
    rule_type,
    scope,
    required_profile_fields,
    expression,
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

-- name: ListRevokedEligibilityRules :many
SELECT rule_key, rule_type, scope
FROM rule_version
WHERE release_id = @release_id::text
  AND review_status = 'REVOKED'
ORDER BY rule_key, rule_type, scope::text;
