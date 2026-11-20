//go:build integration

package llmgw_test

import (
	"context"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
)

func TestLLMGatewayMigrationFreshAndUpgradePaths(t *testing.T) {
	fixture := newLLMTestFixture(t)
	for _, table := range []string{
		"llm_price_table",
		"llm_price_entry",
		"llm_role_route",
		"llm_model_allowlist",
		"llm_budget_scope",
		"llm_logical_call",
		"llm_provider_attempt",
		"llm_attempt_reconciliation",
		"llm_prompt_template",
		"llm_response_schema",
		"llm_result_cache",
	} {
		var exists bool
		if err := fixture.pool.QueryRow(context.Background(),
			`SELECT to_regclass($1) IS NOT NULL`, table,
		).Scan(&exists); err != nil {
			t.Fatalf("检查表 %s: %v", table, err)
		}
		if !exists {
			t.Fatalf("fresh/upgrade 后缺少表 %s", table)
		}
	}

	if _, err := fixture.pool.Exec(context.Background(), `
		INSERT INTO llm_budget_scope (
			budget_scope_id, principal_id, session_id
		) VALUES (gen_random_uuid(), $1, $2)`,
		fixture.principal, fixture.sessionID,
	); err != nil {
		t.Fatalf("首次插入 budget scope: %v", err)
	}
	var tokenLimit, costLimit int64
	var budgetStatus string
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT token_limit, cost_limit_micro_usd, budget_status
		FROM llm_budget_scope
		WHERE principal_id = $1 AND session_id = $2`,
		fixture.principal, fixture.sessionID,
	).Scan(&tokenLimit, &costLimit, &budgetStatus); err != nil {
		t.Fatalf("读取 budget scope 默认值: %v", err)
	}
	if tokenLimit != 50000 || costLimit != 150000 || budgetStatus != "ACTIVE" {
		t.Fatalf("budget scope 默认值 = %d/%d/%q", tokenLimit, costLimit, budgetStatus)
	}
	if _, err := fixture.pool.Exec(context.Background(), `
		UPDATE llm_budget_scope
		SET budget_status = 'INVALID'
		WHERE principal_id = $1 AND session_id = $2`,
		fixture.principal, fixture.sessionID,
	); err == nil {
		t.Fatal("非法 budget_status 应被约束拒绝")
	}
	if _, err := fixture.pool.Exec(context.Background(), `
		INSERT INTO llm_budget_scope (
			budget_scope_id, principal_id, session_id
		) VALUES (gen_random_uuid(), $1, $2)`,
		fixture.principal, fixture.sessionID,
	); err == nil {
		t.Fatal("重复 budget scope 应被唯一约束拒绝")
	}
	if _, err := fixture.pool.Exec(context.Background(), `
		INSERT INTO llm_logical_call (
			logical_call_id, principal_id, session_id, run_id, role,
			release_id, route_version, price_table_version, model_id,
			request_hash, budget_scope_id, reserved_input_tokens,
			reserved_output_tokens, reserved_cost_micro_usd
		)
		SELECT 'logical-duplicate', principal_id, session_id, $1, 'profile',
		       $2, $3, $4, $5, repeat('a', 64), budget_scope_id, 1, 1, 1
		FROM llm_budget_scope
		WHERE principal_id = $6 AND session_id = $7`,
		fixture.runID, fixture.releaseID, fixture.route, fixture.priceTable,
		fixture.model, fixture.principal, fixture.sessionID,
	); err != nil {
		t.Fatalf("首次插入 logical call: %v", err)
	}
	if _, err := fixture.pool.Exec(context.Background(), `
		INSERT INTO llm_logical_call (
			logical_call_id, principal_id, session_id, run_id, role,
			release_id, route_version, price_table_version, model_id,
			request_hash, budget_scope_id, reserved_input_tokens,
			reserved_output_tokens, reserved_cost_micro_usd
		)
		SELECT 'logical-duplicate', principal_id, session_id, $1, 'profile',
		       $2, $3, $4, $5, repeat('b', 64), budget_scope_id, 1, 1, 1
		FROM llm_budget_scope
		WHERE principal_id = $6 AND session_id = $7`,
		fixture.runID, fixture.releaseID, fixture.route, fixture.priceTable,
		fixture.model, fixture.principal, fixture.sessionID,
	); err == nil {
		t.Fatal("重复 logical call 应被唯一约束拒绝")
	}
	if _, err := fixture.pool.Exec(context.Background(), `
		UPDATE llm_price_entry
		SET input_micro_usd_per_million_tokens = 9
		WHERE price_table_version = $1 AND model_id = $2`,
		fixture.priceTable, fixture.model,
	); err == nil {
		t.Fatal("已发布 price entry 不应允许原地 UPDATE")
	}
	if _, err := fixture.pool.Exec(context.Background(), `
		UPDATE llm_prompt_template
		SET system_template = system_template || '篡改'
		WHERE release_id = $1
		  AND prompt_template_key = 'profile_explain'
		  AND prompt_template_version = 'v1'`,
		fixture.releaseID,
	); err == nil {
		t.Fatal("已注册 prompt template 不应允许原地改写内容")
	}
	fixture.seedResponseSchema(t, `{"type":"object"}`)
	if _, err := fixture.pool.Exec(context.Background(), `
		UPDATE llm_response_schema
		SET schema_json = '{"type":"string"}'::jsonb
		WHERE release_id = $1
		  AND response_schema_key = 'profile_explain_v1'
		  AND response_schema_version = 'v1'`,
		fixture.releaseID,
	); err == nil {
		t.Fatal("已注册 response schema 不应允许原地改写内容")
	}
	if _, err := fixture.pool.Exec(context.Background(), `
		UPDATE llm_prompt_template
		SET status = 'REVOKED', revoked_at = NOW()
		WHERE release_id = $1
		  AND prompt_template_key = 'profile_explain'
		  AND prompt_template_version = 'v1'`,
		fixture.releaseID,
	); err != nil {
		t.Fatalf("撤销 prompt template: %v", err)
	}
	if _, err := fixture.pool.Exec(context.Background(), `
		UPDATE llm_prompt_template
		SET status = 'ACTIVE', revoked_at = NULL
		WHERE release_id = $1
		  AND prompt_template_key = 'profile_explain'
		  AND prompt_template_version = 'v1'`,
		fixture.releaseID,
	); err == nil {
		t.Fatal("已撤销 prompt template 不应允许恢复或改写")
	}
}

func TestLLMGatewayMigrationUpgradesExistingRunSchema(t *testing.T) {
	fixture := newLLMTestFixture(t)
	ctx := context.Background()
	schema := "llmgw_upgrade_" + strings.ReplaceAll(uuid.NewString(), "-", "")
	if _, err := fixture.admin.Exec(ctx, "CREATE SCHEMA "+schema); err != nil {
		t.Fatalf("创建 upgrade schema: %v", err)
	}
	t.Cleanup(func() {
		cleanupCtx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		defer cancel()
		_, _ = fixture.admin.Exec(cleanupCtx, "DROP SCHEMA IF EXISTS "+schema+" CASCADE")
	})
	config, err := pgxpool.ParseConfig(os.Getenv("LLMGW_TEST_DSN"))
	if err != nil {
		t.Fatalf("解析 upgrade DSN: %v", err)
	}
	config.ConnConfig.RuntimeParams["search_path"] = schema + ",public"
	config.MaxConns = 1
	pool, err := pgxpool.NewWithConfig(ctx, config)
	if err != nil {
		t.Fatalf("连接 upgrade schema: %v", err)
	}
	t.Cleanup(pool.Close)
	var currentSchema string
	if err := pool.QueryRow(ctx, "SELECT current_schema()").Scan(&currentSchema); err != nil {
		t.Fatalf("读取 upgrade current_schema: %v", err)
	}
	if currentSchema != schema {
		t.Fatalf("upgrade current_schema = %q, want %q", currentSchema, schema)
	}

	applyLLMGatewayMigrationsThrough(t, pool, 28)
	releaseID := "rel_upgrade_m5a_32"
	sessionID := uuid.NewString()
	runID := uuid.NewString()
	if _, err := pool.Exec(ctx, `
		INSERT INTO release (release_id, status, manifest, manifest_sha256, published_at)
		VALUES ($1, 'PUBLISHED', '{}'::jsonb, 'upgrade-28', NOW())`,
		releaseID,
	); err != nil {
		t.Fatalf("seed old release: %v", err)
	}
	if _, err := pool.Exec(ctx, `
		INSERT INTO chat_session (id, principal_id, status)
		VALUES ($1, 'upgrade-principal', 'active')`,
		sessionID,
	); err != nil {
		t.Fatalf("seed old session: %v", err)
	}
	if _, err := pool.Exec(ctx, `
		INSERT INTO run (
			run_id, session_id, principal_id, task_kind, task_input,
			state, stage, release_id
		) VALUES ($1, $2, 'upgrade-principal', 'GENERATE_PLAN', '{}'::jsonb,
		          'RUNNING', 'llm', $3)`,
		runID, sessionID, releaseID,
	); err != nil {
		t.Fatalf("seed old run: %v", err)
	}

	applyLLMGatewayMigration(t, pool, "29_llm_gateway_chat.sql")
	applyLLMGatewayMigration(t, pool, "30_llm_budget_overrun.sql")
	applyLLMGatewayMigration(t, pool, "31_llm_attempt_limit.sql")
	applyLLMGatewayMigration(t, pool, "32_llm_prompt_schema_registry.sql")
	applyLLMGatewayMigration(t, pool, "33_llm_attempt_reconciliation.sql")
	applyLLMGatewayMigration(t, pool, "34_llm_data_classification.sql")
	applyLLMGatewayMigration(t, pool, "35_llm_result_cache.sql")
	var gotRun string
	if err := pool.QueryRow(ctx, `SELECT run_id::text FROM run WHERE run_id = $1`, runID).Scan(&gotRun); err != nil {
		t.Fatalf("upgrade 后读取旧 run: %v", err)
	}
	if gotRun != runID {
		t.Fatalf("upgrade 后 run_id = %q, want %q", gotRun, runID)
	}
	var tableExists bool
	if err := pool.QueryRow(ctx,
		`SELECT to_regclass('llm_logical_call') IS NOT NULL`,
	).Scan(&tableExists); err != nil {
		t.Fatalf("检查 upgrade 新表: %v", err)
	}
	if !tableExists {
		t.Fatal("upgrade 后缺少 llm_logical_call")
	}
	if err := pool.QueryRow(ctx,
		`SELECT to_regclass('llm_result_cache') IS NOT NULL`,
	).Scan(&tableExists); err != nil {
		t.Fatalf("检查 upgrade 结果缓存表: %v", err)
	}
	if !tableExists {
		t.Fatal("upgrade 后缺少 llm_result_cache")
	}
	var (
		allowCache      bool
		logicalCacheHit bool
	)
	if err := pool.QueryRow(ctx, `
		SELECT EXISTS (
		           SELECT 1
		           FROM information_schema.columns
		           WHERE table_schema = current_schema()
		             AND table_name = 'llm_role_route'
		             AND column_name = 'allow_result_cache'
		       ),
		       EXISTS (
		           SELECT 1
		           FROM information_schema.columns
		           WHERE table_schema = current_schema()
		             AND table_name = 'llm_logical_call'
		             AND column_name = 'cache_hit'
		       )`,
	).Scan(&allowCache, &logicalCacheHit); err != nil {
		t.Fatalf("检查 upgrade 缓存列: %v", err)
	}
	if !allowCache || !logicalCacheHit {
		t.Fatalf("upgrade 后缓存列 = route %t logical %t", allowCache, logicalCacheHit)
	}
}
