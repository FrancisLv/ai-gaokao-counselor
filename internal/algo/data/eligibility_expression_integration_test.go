//go:build integration

package data

import (
	"context"
	"fmt"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
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
		 VALUES ($1, 'PUBLISHED', '{}'::jsonb, 'sha256:m4b-ast-storage', NOW())`, releaseID); err != nil {
		t.Fatalf("insert release: %v", err)
	}
	t.Cleanup(func() {
		_, _ = pool.Exec(context.Background(), `DELETE FROM release WHERE release_id = $1`, releaseID)
	})

	insert := func(ruleID string, expression string, expressionVersion int) error {
		_, err := pool.Exec(context.Background(),
			`INSERT INTO rule_version (
				rule_id, version, rule_key, rule_type, scope, required_profile_fields,
				expression, expression_version, outcome_when_true, reason_code,
				review_status, valid_from, release_id, source_evidence
			) VALUES (
				$1, 1, $1, 'OTHER_HARD', '{}'::jsonb, '["category"]'::jsonb,
				$2::jsonb, $3, 'FAIL', 'RULE_FIXTURE', 'PUBLISHED', NOW(), $4,
				'[{"evidence_id":"ev","evidence_type":"DOCUMENT"}]'::jsonb
			)`, ruleID, expression, expressionVersion, releaseID)
		return err
	}

	if err := insert("wrong-version", `{"op":"PRESENT","field":"category"}`, 2); err == nil {
		t.Fatal("expression_version=2 应被 schema 拒绝")
	}
	oversized := fmt.Sprintf(
		`{"op":"EQ","field":"category","value":%q}`,
		strings.Repeat("x", 33*1024),
	)
	if err := insert("oversized", oversized, 1); err == nil {
		t.Fatal("超过 32KiB 的表达式应被 schema 拒绝")
	}
}
