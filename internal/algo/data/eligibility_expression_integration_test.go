//go:build integration

package data

import (
	"context"
	"errors"
	"fmt"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/choucisan/ai-gaokao-counselor/internal/algo/biz"
)

func TestIntegration_RuleExpressionStorageContract(t *testing.T) {
	dsn := os.Getenv("ALGO_TEST_DSN")
	if dsn == "" {
		t.Skip("ALGO_TEST_DSN 未设置，跳过集成测试")
	}
	pool, err := pgxpool.New(context.Background(), dsn)
	if err != nil {
		t.Fatalf("pgxpool.New: %v", err)
	}
	t.Cleanup(pool.Close)

	releaseID := fmt.Sprintf("rel_m4b_ast_storage_%d", time.Now().UnixNano())
	if _, err := pool.Exec(context.Background(),
		`INSERT INTO release (release_id, status, manifest, manifest_sha256, published_at)
		 VALUES ($1, 'STAGING', '{}'::jsonb, 'sha256:m4b-ast-storage', NULL)`, releaseID); err != nil {
		t.Fatalf("insert release: %v", err)
	}
	t.Cleanup(func() {
		_, _ = pool.Exec(context.Background(), `DELETE FROM release WHERE release_id = $1`, releaseID)
	})

	var hasCreatedBy bool
	if err := pool.QueryRow(context.Background(),
		`SELECT EXISTS (
			SELECT 1
			FROM information_schema.columns
			WHERE table_schema = current_schema()
			  AND table_name = 'rule_version'
			  AND column_name = 'created_by'
		)`).Scan(&hasCreatedBy); err != nil {
		t.Fatalf("inspect rule_version lifecycle columns: %v", err)
	}
	insert := func(ruleID string, expression string, expressionVersion int) error {
		query := `INSERT INTO rule_version (
				rule_id, version, rule_key, rule_type, scope, required_profile_fields,
				expression, expression_version, outcome_when_true, reason_code,
				review_status, valid_from, source_evidence, created_by
			) VALUES (
				$1, 1, $1, 'OTHER_HARD', '{}'::jsonb, '["category"]'::jsonb,
				$2::jsonb, $3, 'FAIL', 'RULE_FIXTURE', 'DRAFT', NOW(), $4,
				jsonb_build_array(jsonb_build_object(
					'evidence_id', 'ev',
					'evidence_type', 'DOCUMENT',
					'release_id', $4::text
				))
			)`
		if hasCreatedBy {
			query = `INSERT INTO rule_version (
					rule_id, version, rule_key, rule_type, scope, required_profile_fields,
					expression, expression_version, outcome_when_true, reason_code,
					review_status, valid_from, release_id, source_evidence, created_by
				) VALUES (
					$1, 1, $1, 'OTHER_HARD', '{}'::jsonb, '["category"]'::jsonb,
					$2::jsonb, $3, 'FAIL', 'RULE_FIXTURE', 'DRAFT', NOW(), $4,
					jsonb_build_array(jsonb_build_object(
						'evidence_id', 'ev',
						'evidence_type', 'DOCUMENT',
						'release_id', $4::text
					)),
					'fixture-author'
				)`
		}
		_, err := pool.Exec(context.Background(), query, ruleID, expression, expressionVersion, releaseID)
		return err
	}

	if err := insert("wrong-version", `{"op":"PRESENT","field":"category"}`, 2); !isCheckViolation(err, "rule_version_expression_version_check") {
		t.Fatalf("expression_version=2 应由 CHECK 约束拒绝，got %v", err)
	}
	oversized := fmt.Sprintf(
		`{"op":"EQ","field":"category","value":%q}`,
		strings.Repeat("x", 33*1024),
	)
	if err := insert("oversized", oversized, 1); !isCheckViolation(err, "rule_version_expression_size_check") {
		t.Fatalf("超过 32KiB 的表达式应由 CHECK 约束拒绝，got %v", err)
	}
}

func TestIntegration_EligibilityProfileFieldCatalog(t *testing.T) {
	dsn := os.Getenv("ALGO_TEST_DSN")
	if dsn == "" {
		t.Skip("ALGO_TEST_DSN 未设置，跳过集成测试")
	}
	pool, err := pgxpool.New(context.Background(), dsn)
	if err != nil {
		t.Fatalf("pgxpool.New: %v", err)
	}
	t.Cleanup(pool.Close)
	applyMigrationIfNeeded(
		t,
		pool,
		"26_release_bound_profile_fields.sql",
		"eligibility_profile_field",
		"release_id",
	)

	expected := map[string]string{
		"single_subject_scores.数学":     "INT",
		"single_subject_scores.英语":     "INT",
		"physical_exam.color_vision":   "STRING",
		"gender":                       "STRING",
		"household_province":           "STRING",
		"school_registration_province": "STRING",
	}
	for fieldKey, valueType := range expected {
		var gotType string
		if err := pool.QueryRow(context.Background(),
			`SELECT value_type
			 FROM eligibility_profile_field
			 WHERE release_id = 'rel_legacy_v1' AND field_key = $1`, fieldKey,
		).Scan(&gotType); err != nil {
			t.Fatalf("load %s field contract: %v", fieldKey, err)
		}
		if gotType != valueType {
			t.Fatalf("%s value_type = %q, want %q", fieldKey, gotType, valueType)
		}
	}

	var colorValues []string
	if err := pool.QueryRow(context.Background(),
		`SELECT allowed_values
		 FROM eligibility_profile_field
			 WHERE release_id = 'rel_legacy_v1'
			   AND field_key = 'physical_exam.color_vision'`,
	).Scan(&colorValues); err != nil {
		t.Fatalf("load color-vision values: %v", err)
	}
	if got := strings.Join(colorValues, ","); got != "NORMAL,COLOR_WEAK,COLOR_BLIND" {
		t.Fatalf("color-vision values = %q", got)
	}

	rows, err := pool.Query(context.Background(),
		`SELECT field_key, value_type, allowed_values
		 FROM eligibility_profile_field
		 WHERE release_id = 'rel_legacy_v1'
		 ORDER BY field_key`,
	)
	if err != nil {
		t.Fatalf("list field catalog: %v", err)
	}
	persisted := make(map[string]biz.ProfileFieldDefinition)
	for rows.Next() {
		var field biz.ProfileFieldDefinition
		if err := rows.Scan(&field.FieldKey, &field.ValueType, &field.AllowedValues); err != nil {
			t.Fatalf("scan field catalog: %v", err)
		}
		persisted[field.FieldKey] = field
	}
	if err := rows.Err(); err != nil {
		t.Fatalf("iterate field catalog: %v", err)
	}
	rows.Close()
	builtin := biz.BuiltinEligibilityProfileFields()
	if len(persisted) != len(builtin) {
		t.Fatalf("field catalog size = %d, builtin = %d", len(persisted), len(builtin))
	}
	for _, expected := range builtin {
		actual, ok := persisted[expected.FieldKey]
		if !ok {
			t.Fatalf("field catalog missing %s", expected.FieldKey)
		}
		if actual.ValueType != expected.ValueType ||
			strings.Join(actual.AllowedValues, ",") != strings.Join(expected.AllowedValues, ",") {
			t.Fatalf("field catalog %s = %+v, want %+v", expected.FieldKey, actual, expected)
		}
	}

	stagingReleaseID := fmt.Sprintf("rel_m4b_field_contract_%d", time.Now().UnixNano())
	if _, err := pool.Exec(context.Background(), `
		INSERT INTO release (release_id, status, manifest, manifest_sha256, published_at)
		VALUES ($1, 'STAGING', '{}'::jsonb, 'sha256:m4b-field-contract', NULL)`,
		stagingReleaseID,
	); err != nil {
		t.Fatalf("insert staging release: %v", err)
	}
	t.Cleanup(func() {
		_, _ = pool.Exec(context.Background(),
			`DELETE FROM release WHERE release_id = $1`, stagingReleaseID)
	})
	_, err = pool.Exec(context.Background(),
		`INSERT INTO eligibility_profile_field (
			release_id, field_key, value_type, allowed_values, description
		 ) VALUES ($1, 'invalid-field-type', 'BOOLEAN', ARRAY[]::TEXT[], 'invalid')`,
		stagingReleaseID,
	)
	if !isCheckViolation(err, "eligibility_profile_field_value_type_check") {
		t.Fatalf("invalid field value_type should be rejected by CHECK, got %v", err)
	}
}

func TestIntegration_EligibilityProfileFieldCatalogIsReleaseBound(t *testing.T) {
	dsn := os.Getenv("ALGO_TEST_DSN")
	if dsn == "" {
		t.Skip("ALGO_TEST_DSN 未设置，跳过集成测试")
	}
	pool, err := pgxpool.New(context.Background(), dsn)
	if err != nil {
		t.Fatalf("pgxpool.New: %v", err)
	}
	t.Cleanup(pool.Close)
	applyMigrationIfNeeded(
		t,
		pool,
		"26_release_bound_profile_fields.sql",
		"eligibility_profile_field",
		"release_id",
	)

	firstRelease := fmt.Sprintf("rel_m4b_field_first_%d", time.Now().UnixNano())
	secondRelease := fmt.Sprintf("rel_m4b_field_second_%d", time.Now().UnixNano())
	for _, releaseID := range []string{firstRelease, secondRelease} {
		if _, err := pool.Exec(context.Background(), `
			INSERT INTO release (release_id, status, manifest, manifest_sha256, published_at)
			VALUES ($1, 'STAGING', '{}'::jsonb, 'sha256:m4b-fields', NULL)`,
			releaseID,
		); err != nil {
			t.Fatalf("insert release %s: %v", releaseID, err)
		}
	}
	t.Cleanup(func() {
		_, _ = pool.Exec(context.Background(),
			`DELETE FROM release WHERE release_id = ANY($1::text[])`,
			[]string{firstRelease, secondRelease},
		)
	})

	fieldKey := fmt.Sprintf("single_subject_scores.技术%d", time.Now().UnixNano())
	if _, err := pool.Exec(context.Background(), `
		INSERT INTO eligibility_profile_field (
			release_id, field_key, value_type, allowed_values, description
		) VALUES ($1, $2, 'INT', ARRAY[]::TEXT[], 'first release only')`,
		firstRelease, fieldKey,
	); err != nil {
		t.Fatalf("insert release-scoped field: %v", err)
	}

	load := func(releaseID string) int {
		t.Helper()
		var count int
		if err := pool.QueryRow(context.Background(), `
			SELECT count(*)
			FROM eligibility_profile_field
			WHERE release_id = $1 AND field_key = $2`,
			releaseID, fieldKey,
		).Scan(&count); err != nil {
			t.Fatalf("load field count: %v", err)
		}
		return count
	}
	if got := load(firstRelease); got != 1 {
		t.Fatalf("first release custom field count = %d, want 1", got)
	}
	if got := load(secondRelease); got != 0 {
		t.Fatalf("second release custom field count = %d, want 0", got)
	}

	if _, err := pool.Exec(context.Background(), `
		UPDATE release SET status = 'PUBLISHED', published_at = clock_timestamp()
		WHERE release_id = $1`, firstRelease,
	); err != nil {
		t.Fatalf("publish first release: %v", err)
	}
	if _, err := pool.Exec(context.Background(), `
		UPDATE eligibility_profile_field
		SET allowed_values = ARRAY['changed']
		WHERE release_id = $1 AND field_key = $2`,
		firstRelease, fieldKey,
	); !isPostgresCode(err, "23514") {
		t.Fatalf("published field update err = %v, want 23514", err)
	}
	if _, err := pool.Exec(context.Background(), `
		DELETE FROM eligibility_profile_field
		WHERE release_id = $1 AND field_key = $2`,
		firstRelease, fieldKey,
	); !isPostgresCode(err, "23514") {
		t.Fatalf("published field delete err = %v, want 23514", err)
	}
}

func applyMigrationIfNeeded(
	t *testing.T,
	pool *pgxpool.Pool,
	file string,
	table string,
	column string,
) {
	t.Helper()
	var present bool
	if err := pool.QueryRow(context.Background(), `
		SELECT EXISTS (
			SELECT 1
			FROM information_schema.columns
			WHERE table_schema = current_schema()
			  AND table_name = $1
			  AND column_name = $2
		)`, table, column,
	).Scan(&present); err != nil {
		t.Fatalf("inspect %s.%s: %v", table, column, err)
	}
	if present {
		return
	}
	body, err := os.ReadFile("../../../migrations/" + file)
	if err != nil {
		t.Fatalf("read migration %s: %v", file, err)
	}
	if _, err := pool.Exec(context.Background(), string(body)); err != nil {
		t.Fatalf("apply migration %s: %v", file, err)
	}
}

func isCheckViolation(err error, constraint string) bool {
	var pgErr *pgconn.PgError
	return errors.As(err, &pgErr) &&
		pgErr.Code == "23514" &&
		pgErr.ConstraintName == constraint
}
