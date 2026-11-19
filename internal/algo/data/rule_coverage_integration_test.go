//go:build integration

package data

import (
	"context"
	"errors"
	"fmt"
	"os"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

func TestIntegration_RuleCoverageStorageContract(t *testing.T) {
	dsn := os.Getenv("ALGO_TEST_DSN")
	if dsn == "" {
		t.Skip("ALGO_TEST_DSN 未设置，跳过集成测试")
	}
	pool, err := pgxpool.New(context.Background(), dsn)
	if err != nil {
		t.Fatalf("pgxpool.New: %v", err)
	}
	t.Cleanup(pool.Close)
	applyRuleCoverageMigration(t, pool)

	releaseID := fmt.Sprintf("rel_m4b_coverage_%d", time.Now().UnixNano())
	if _, err := pool.Exec(context.Background(), `
		INSERT INTO release (release_id, status, manifest, manifest_sha256, published_at)
		VALUES ($1, 'STAGING', '{}'::jsonb, 'sha256:m4b-coverage', NULL)`,
		releaseID,
	); err != nil {
		t.Fatalf("insert staging release: %v", err)
	}
	t.Cleanup(func() {
		_, _ = pool.Exec(context.Background(), `DELETE FROM release WHERE release_id = $1`, releaseID)
	})

	insertCoverage := func(scope, status, sources, notes string) error {
		_, err := pool.Exec(context.Background(), `
			INSERT INTO rule_coverage (
				release_id, rule_type, scope, status, covered_data_sources, notes
			) VALUES ($1, 'SUBJECT_REQUIREMENT', $2::jsonb, $3, $4::jsonb, $5)`,
			releaseID, scope, status, sources, notes,
		)
		return err
	}

	if err := insertCoverage(
		`{"province":"湖南","application_year":2025}`,
		"FULL",
		`["enrollment_plan"]`,
		"complete source coverage",
	); err != nil {
		t.Fatalf("insert valid FULL coverage: %v", err)
	}
	if err := insertCoverage(
		`{"province":"湖南","application_year":2025}`,
		"NONE",
		`[]`,
		"duplicate declaration",
	); !isConstraintViolation(err, "rule_coverage_release_type_scope_key_key") {
		t.Fatalf("duplicate normalized scope err = %v, want scope_key unique violation", err)
	}
	if err := insertCoverage(
		`{"province":"湖南","mystery":"value"}`,
		"NONE",
		`[]`,
		"unknown scope key",
	); !isConstraintViolation(err, "rule_coverage_scope_valid") {
		t.Fatalf("unknown scope key err = %v, want scope validity violation", err)
	}
	if err := insertCoverage(
		`{"province":"湖南","category":"物理类"}`,
		"FULL",
		`[]`,
		"missing source",
	); !isConstraintViolation(err, "rule_coverage_full_sources") {
		t.Fatalf("empty FULL source err = %v, want source violation", err)
	}
	if err := insertCoverage(
		`{"province":"湖南","category":"历史类"}`,
		"PARTIAL",
		`[]`,
		"",
	); !isConstraintViolation(err, "rule_coverage_gap_notes") {
		t.Fatalf("empty PARTIAL notes err = %v, want gap-notes violation", err)
	}

	if _, err := pool.Exec(context.Background(), `
		UPDATE release SET status = 'PUBLISHED', published_at = clock_timestamp()
		WHERE release_id = $1`, releaseID,
	); err != nil {
		t.Fatalf("publish staged release: %v", err)
	}

	if _, err := pool.Exec(context.Background(), `
		UPDATE rule_coverage SET status = 'PARTIAL', notes = 'changed after publish'
		WHERE release_id = $1`, releaseID,
	); !isPostgresCode(err, "23514") {
		t.Fatalf("published coverage update err = %v, want 23514", err)
	}
	if _, err := pool.Exec(context.Background(), `
		DELETE FROM rule_coverage WHERE release_id = $1`, releaseID,
	); !isPostgresCode(err, "23514") {
		t.Fatalf("published coverage delete err = %v, want 23514", err)
	}
	if err := insertCoverage(
		`{"province":"湖南","category":"物理类"}`,
		"NONE",
		`[]`,
		"late declaration",
	); !isPostgresCode(err, "23514") {
		t.Fatalf("published coverage insert err = %v, want 23514", err)
	}
}

func applyRuleCoverageMigration(t *testing.T, pool *pgxpool.Pool) {
	t.Helper()
	body, err := os.ReadFile("../../../migrations/25_rule_coverage_contract.sql")
	if err != nil {
		t.Fatalf("read rule coverage migration: %v", err)
	}
	if _, err := pool.Exec(context.Background(), string(body)); err != nil {
		t.Fatalf("apply rule coverage migration: %v", err)
	}
}

func isConstraintViolation(err error, constraint string) bool {
	var pgErr *pgconn.PgError
	return errors.As(err, &pgErr) && pgErr.ConstraintName == constraint
}

func isPostgresCode(err error, code string) bool {
	var pgErr *pgconn.PgError
	return errors.As(err, &pgErr) && pgErr.Code == code
}
