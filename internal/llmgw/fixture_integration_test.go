//go:build integration

package llmgw_test

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	kerrors "github.com/go-kratos/kratos/v2/errors"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"

	commonv1 "github.com/choucisan/ai-gaokao-counselor/api/common/v1"
	llmgwv1 "github.com/choucisan/ai-gaokao-counselor/api/llmgw/v1"
	"github.com/choucisan/ai-gaokao-counselor/internal/llmgw"
)

type providerCall struct {
	attemptID      string
	model          string
	maxTokens      int32
	messages       []llmgw.ProviderMessage
	responseSchema []byte
}

type fakeBehavior struct {
	response llmgw.ProviderResponse
	err      error
}

type fakeProvider struct {
	mu        sync.Mutex
	calls     []providerCall
	response  llmgw.ProviderResponse
	err       error
	behaviors []fakeBehavior
	onCall    func(context.Context)
}

func (p *fakeProvider) Chat(ctx context.Context, req llmgw.ProviderRequest) (llmgw.ProviderResponse, error) {
	p.mu.Lock()
	p.calls = append(p.calls, providerCall{
		attemptID:      req.AttemptID,
		model:          req.Model,
		maxTokens:      req.MaxOutputTokens,
		messages:       append([]llmgw.ProviderMessage(nil), req.Messages...),
		responseSchema: append([]byte(nil), req.ResponseSchema...),
	})
	onCall := p.onCall
	response := p.response
	providerErr := p.err
	if len(p.behaviors) > 0 {
		behavior := p.behaviors[0]
		p.behaviors = p.behaviors[1:]
		response = behavior.response
		providerErr = behavior.err
	}
	p.mu.Unlock()
	if onCall != nil {
		onCall(ctx)
	}
	if providerErr != nil {
		return llmgw.ProviderResponse{}, providerErr
	}
	return response, nil
}

func (p *fakeProvider) callsSnapshot() []providerCall {
	p.mu.Lock()
	defer p.mu.Unlock()
	return append([]providerCall(nil), p.calls...)
}

func (p *fakeProvider) setBehavior(response llmgw.ProviderResponse, err error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.response = response
	p.err = err
}

type disconnectProvider struct {
	called chan struct{}
}

func (p *disconnectProvider) Chat(
	ctx context.Context,
	_ llmgw.ProviderRequest,
) (llmgw.ProviderResponse, error) {
	close(p.called)
	<-ctx.Done()
	return llmgw.ProviderResponse{}, ctx.Err()
}

type llmTestFixture struct {
	pool       *pgxpool.Pool
	admin      *pgxpool.Pool
	schema     string
	principal  string
	sessionID  string
	runID      string
	releaseID  string
	priceTable string
	route      string
	model      string
	fallback   string
	fallback2  string
}

func newLLMTestFixture(t *testing.T) *llmTestFixture {
	t.Helper()
	dsn := os.Getenv("LLMGW_TEST_DSN")
	if dsn == "" {
		t.Skip("LLMGW_TEST_DSN 未设置")
	}

	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	admin, err := pgxpool.New(ctx, dsn)
	if err != nil {
		t.Fatalf("连接 LLMGW_TEST_DSN: %v", err)
	}
	t.Cleanup(admin.Close)

	var database string
	if err := admin.QueryRow(ctx, "SELECT current_database()").Scan(&database); err != nil {
		t.Fatalf("读取测试库名: %v", err)
	}
	if !strings.Contains(strings.ToLower(database), "test") {
		t.Fatalf("拒绝在非测试数据库运行: %s", database)
	}
	for _, extension := range []string{"vector", "pg_trgm"} {
		if _, err := admin.Exec(ctx, "CREATE EXTENSION IF NOT EXISTS "+extension+" WITH SCHEMA public"); err != nil {
			t.Fatalf("在 public 安装扩展 %s: %v", extension, err)
		}
	}

	schema := "llmgw_" + strings.ReplaceAll(uuid.NewString(), "-", "")
	if _, err := admin.Exec(ctx, "CREATE SCHEMA "+schema); err != nil {
		t.Fatalf("创建隔离 schema: %v", err)
	}
	t.Cleanup(func() {
		cleanupCtx, cleanupCancel := context.WithTimeout(context.Background(), 20*time.Second)
		defer cleanupCancel()
		_, _ = admin.Exec(cleanupCtx, "DROP SCHEMA IF EXISTS "+schema+" CASCADE")
	})

	config, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		t.Fatalf("解析 LLMGW_TEST_DSN: %v", err)
	}
	config.ConnConfig.RuntimeParams["search_path"] = schema + ",public"
	config.MaxConns = 8
	pool, err := pgxpool.NewWithConfig(ctx, config)
	if err != nil {
		t.Fatalf("建立隔离 schema 连接池: %v", err)
	}
	t.Cleanup(pool.Close)
	applyLLMGatewayMigrations(t, pool)

	fixture := &llmTestFixture{
		pool:       pool,
		admin:      admin,
		schema:     schema,
		principal:  "principal-chat",
		sessionID:  uuid.NewString(),
		runID:      uuid.NewString(),
		releaseID:  "rel_m5a_32_test",
		priceTable: "price_m5a_32_v1",
		route:      "route_profile_v1",
		model:      "fake-governed-model",
		fallback:   "fake-fallback-model",
		fallback2:  "fake-fallback-model-2",
	}
	fixture.seed(t)
	return fixture
}

func (f *llmTestFixture) seedBudget(t *testing.T, tokenLimit, costLimit int64) string {
	t.Helper()
	var budgetScopeID string
	if err := f.pool.QueryRow(context.Background(), `
		INSERT INTO llm_budget_scope (
			budget_scope_id, principal_id, session_id, token_limit,
			cost_limit_micro_usd
		) VALUES (gen_random_uuid(), $1, $2, $3, $4)
		RETURNING budget_scope_id::text`,
		f.principal, f.sessionID, tokenLimit, costLimit,
	).Scan(&budgetScopeID); err != nil {
		t.Fatalf("seed budget scope: %v", err)
	}
	return budgetScopeID
}

func applyLLMGatewayMigrations(t *testing.T, pool *pgxpool.Pool) {
	t.Helper()
	applyLLMGatewayMigrationsThrough(t, pool, 35)
}

func applyLLMGatewayMigrationsThrough(t *testing.T, pool *pgxpool.Pool, last int) {
	t.Helper()
	conn, err := pool.Acquire(context.Background())
	if err != nil {
		t.Fatalf("获取 migration 连接: %v", err)
	}
	defer conn.Release()
	root := filepath.Clean(filepath.Join("..", ".."))
	migrations := []string{"00_schema.sql", "07_policy_chunk_trgm.sql"}
	for i := 8; i <= last; i++ {
		matches, err := filepath.Glob(filepath.Join(root, "migrations", fmt.Sprintf("%02d_*.sql", i)))
		if err != nil {
			t.Fatalf("查找 migration %02d: %v", i, err)
		}
		if len(matches) != 1 {
			t.Fatalf("migration %02d 匹配 %d 个文件", i, len(matches))
		}
		migrations = append(migrations, filepath.Base(matches[0]))
	}
	for _, name := range migrations {
		body, err := os.ReadFile(filepath.Join(root, "migrations", name))
		if err != nil {
			t.Fatalf("读取 migration %s: %v", name, err)
		}
		if _, err := conn.Exec(context.Background(), string(body)); err != nil {
			t.Fatalf("应用 migration %s: %v", name, err)
		}
	}
}

func applyLLMGatewayMigration(t *testing.T, pool *pgxpool.Pool, name string) {
	t.Helper()
	body, err := os.ReadFile(filepath.Join(filepath.Clean(filepath.Join("..", "..")), "migrations", name))
	if err != nil {
		t.Fatalf("读取 migration %s: %v", name, err)
	}
	conn, err := pool.Acquire(context.Background())
	if err != nil {
		t.Fatalf("获取 migration 连接: %v", err)
	}
	defer conn.Release()
	if _, err := conn.Exec(context.Background(), string(body)); err != nil {
		t.Fatalf("应用 migration %s: %v", name, err)
	}
}

func (f *llmTestFixture) seed(t *testing.T) {
	t.Helper()
	ctx := context.Background()
	if _, err := f.pool.Exec(ctx, `
		INSERT INTO release (release_id, status, manifest, manifest_sha256, published_at)
		VALUES ($1, 'PUBLISHED', '{}'::jsonb, 'm5a-32', NOW())`,
		f.releaseID,
	); err != nil {
		t.Fatalf("seed release: %v", err)
	}
	if _, err := f.pool.Exec(ctx, `
		INSERT INTO chat_session (id, principal_id, status)
		VALUES ($1, $2, 'active')`,
		f.sessionID, f.principal,
	); err != nil {
		t.Fatalf("seed session: %v", err)
	}
	if _, err := f.pool.Exec(ctx, `
		INSERT INTO run (
			run_id, session_id, principal_id, task_kind, task_input,
			state, stage, release_id
		) VALUES ($1, $2, $3, 'GENERATE_PLAN', '{}'::jsonb, 'RUNNING', 'llm', $4)`,
		f.runID, f.sessionID, f.principal, f.releaseID,
	); err != nil {
		t.Fatalf("seed run: %v", err)
	}
	if _, err := f.pool.Exec(ctx, `
		INSERT INTO llm_price_table (
			price_table_version, currency_unit, effective_at, exchange_rate_version, published_at
		) VALUES ($1, 'USD_MICRO', NOW() - INTERVAL '1 minute', 'fx-v1', NULL)`,
		f.priceTable,
	); err != nil {
		t.Fatalf("seed price table: %v", err)
	}
	if _, err := f.pool.Exec(ctx, `
		INSERT INTO llm_price_entry (
			price_table_version, model_id,
			input_micro_usd_per_million_tokens,
			output_micro_usd_per_million_tokens
		) VALUES
			($1, $2, 1000000, 2000000),
			($1, $3, 2000000, 4000000),
			($1, $4, 3000000, 6000000)`,
		f.priceTable, f.model,
		f.fallback,
		f.fallback2,
	); err != nil {
		t.Fatalf("seed price entry: %v", err)
	}
	if _, err := f.pool.Exec(ctx, `
		UPDATE llm_price_table
		SET published_at = NOW()
		WHERE price_table_version = $1`,
		f.priceTable,
	); err != nil {
		t.Fatalf("publish price table: %v", err)
	}
	if _, err := f.pool.Exec(ctx, `
		INSERT INTO llm_model_allowlist (
			release_id, model_id, provider, enabled
		) VALUES ($1, $2, 'fake', TRUE)`,
		f.releaseID, f.model,
	); err != nil {
		t.Fatalf("seed allowlist: %v", err)
	}
	if _, err := f.pool.Exec(ctx, `
		INSERT INTO llm_role_route (
			release_id, role, route_version, primary_model_id, fallback_model_ids,
			max_output_tokens, timeout_ms, max_attempts, price_table_version
		) VALUES ($1, 'profile', $2, $3, ARRAY[]::TEXT[], 1000, 5000, 2, $4)`,
		f.releaseID, f.route, f.model, f.priceTable,
	); err != nil {
		t.Fatalf("seed route: %v", err)
	}
	systemTemplate := "你是高考志愿解释助手，只能依据结构化事实为{{candidate_name}}写作。"
	userTemplate := "请解释{{topic}}，不得改写任何数值。"
	templateDigest := sha256.Sum256([]byte(systemTemplate + "\x00" + userTemplate))
	if _, err := f.pool.Exec(ctx, `
		INSERT INTO llm_prompt_template (
			release_id, prompt_template_key, prompt_template_version,
			system_template, user_template, template_sha256
		) VALUES ($1, 'profile_explain', 'v1', $2, $3, $4)`,
		f.releaseID, systemTemplate, userTemplate, hex.EncodeToString(templateDigest[:]),
	); err != nil {
		t.Fatalf("seed prompt template: %v", err)
	}
}

func (f *llmTestFixture) seedResponseSchema(t *testing.T, schema string) {
	t.Helper()
	sum := sha256.Sum256([]byte(schema))
	if _, err := f.pool.Exec(context.Background(), `
		INSERT INTO llm_response_schema (
			release_id, response_schema_key, response_schema_version,
			schema_json, schema_sha256
		) VALUES ($1, 'profile_explain_v1', 'v1', $2::jsonb, $3)`,
		f.releaseID, schema, hex.EncodeToString(sum[:]),
	); err != nil {
		t.Fatalf("seed response schema: %v", err)
	}
}

func (f *llmTestFixture) seedPromptVersion(t *testing.T, version, systemTemplate, userTemplate string) {
	t.Helper()
	sum := sha256.Sum256([]byte(systemTemplate + "\x00" + userTemplate))
	if _, err := f.pool.Exec(context.Background(), `
		INSERT INTO llm_prompt_template (
			release_id, prompt_template_key, prompt_template_version,
			system_template, user_template, template_sha256
		) VALUES ($1, 'profile_explain', $2, $3, $4, $5)`,
		f.releaseID, version, systemTemplate, userTemplate, hex.EncodeToString(sum[:]),
	); err != nil {
		t.Fatalf("seed prompt version %s: %v", version, err)
	}
}

func (f *llmTestFixture) seedSchemaVersion(t *testing.T, version, schema string) {
	t.Helper()
	sum := sha256.Sum256([]byte(schema))
	if _, err := f.pool.Exec(context.Background(), `
		INSERT INTO llm_response_schema (
			release_id, response_schema_key, response_schema_version,
			schema_json, schema_sha256
		) VALUES ($1, 'profile_explain_v1', $2, $3::jsonb, $4)`,
		f.releaseID, version, schema, hex.EncodeToString(sum[:]),
	); err != nil {
		t.Fatalf("seed schema version %s: %v", version, err)
	}
}

func (f *llmTestFixture) enableFallback(t *testing.T) {
	t.Helper()
	ctx := context.Background()
	if _, err := f.pool.Exec(ctx, `
		INSERT INTO llm_model_allowlist (release_id, model_id, provider, enabled)
		VALUES ($1, $2, 'fake', TRUE)`,
		f.releaseID, f.fallback,
	); err != nil {
		t.Fatalf("seed fallback allowlist: %v", err)
	}
	if _, err := f.pool.Exec(ctx, `
		UPDATE llm_role_route
		SET fallback_model_ids = ARRAY[$1]::TEXT[]
		WHERE release_id = $2 AND role = 'profile'`,
		f.fallback, f.releaseID,
	); err != nil {
		t.Fatalf("seed fallback route: %v", err)
	}
}

func (f *llmTestFixture) enableFallbackChain(t *testing.T) {
	t.Helper()
	ctx := context.Background()
	if _, err := f.pool.Exec(ctx, `
		INSERT INTO llm_model_allowlist (release_id, model_id, provider, enabled)
		VALUES ($1, $2, 'fake', TRUE), ($1, $3, 'fake', TRUE)`,
		f.releaseID, f.fallback, f.fallback2,
	); err != nil {
		t.Fatalf("seed fallback chain allowlist: %v", err)
	}
	if _, err := f.pool.Exec(ctx, `
		UPDATE llm_role_route
		SET fallback_model_ids = ARRAY[$1, $2]::TEXT[]
		WHERE release_id = $3 AND role = 'profile'`,
		f.fallback, f.fallback2, f.releaseID,
	); err != nil {
		t.Fatalf("seed fallback chain route: %v", err)
	}
}

func (f *llmTestFixture) enableResultCache(t *testing.T) {
	t.Helper()
	if _, err := f.pool.Exec(context.Background(), `
		UPDATE llm_role_route
		SET allow_result_cache = TRUE
		WHERE release_id = $1 AND role = 'profile'`,
		f.releaseID,
	); err != nil {
		t.Fatalf("允许 route 结果缓存: %v", err)
	}
}

func (f *llmTestFixture) moveRunToClonedRelease(t *testing.T, releaseID string) {
	t.Helper()
	ctx := context.Background()
	if _, err := f.pool.Exec(ctx, `
		INSERT INTO release (
			release_id, status, manifest, manifest_sha256, published_at
		) VALUES ($1, 'PUBLISHED', '{}'::jsonb, $1, NOW())`,
		releaseID,
	); err != nil {
		t.Fatalf("seed cloned release: %v", err)
	}
	if _, err := f.pool.Exec(ctx, `
		INSERT INTO llm_model_allowlist (
			release_id, model_id, provider, enabled
		)
		SELECT $1, model_id, provider, enabled
		FROM llm_model_allowlist
		WHERE release_id = $2`,
		releaseID, f.releaseID,
	); err != nil {
		t.Fatalf("clone allowlist: %v", err)
	}
	if _, err := f.pool.Exec(ctx, `
		INSERT INTO llm_role_route (
			release_id, role, route_version, primary_model_id,
			fallback_model_ids, max_output_tokens, timeout_ms,
			max_attempts, price_table_version, requires_response_schema,
			allowed_data_classes, allow_result_cache
		)
		SELECT $1, role, route_version, primary_model_id,
		       fallback_model_ids, max_output_tokens, timeout_ms,
		       max_attempts, price_table_version, requires_response_schema,
		       allowed_data_classes, allow_result_cache
		FROM llm_role_route
		WHERE release_id = $2 AND role = 'profile'`,
		releaseID, f.releaseID,
	); err != nil {
		t.Fatalf("clone route: %v", err)
	}
	if _, err := f.pool.Exec(ctx, `
		INSERT INTO llm_prompt_template (
			release_id, prompt_template_key, prompt_template_version,
			system_template, user_template, template_sha256, status,
			published_at, revoked_at, created_at
		)
		SELECT $1, prompt_template_key, prompt_template_version,
		       system_template, user_template, template_sha256, status,
		       published_at, revoked_at, created_at
		FROM llm_prompt_template
		WHERE release_id = $2`,
		releaseID, f.releaseID,
	); err != nil {
		t.Fatalf("clone prompt templates: %v", err)
	}
	if _, err := f.pool.Exec(ctx, `
		INSERT INTO llm_response_schema (
			release_id, response_schema_key, response_schema_version,
			schema_json, schema_sha256, status, published_at,
			revoked_at, created_at
		)
		SELECT $1, response_schema_key, response_schema_version,
		       schema_json, schema_sha256, status, published_at,
		       revoked_at, created_at
		FROM llm_response_schema
		WHERE release_id = $2`,
		releaseID, f.releaseID,
	); err != nil {
		t.Fatalf("clone response schemas: %v", err)
	}
	if _, err := f.pool.Exec(ctx, `
		UPDATE run
		SET release_id = $1
		WHERE run_id = $2`,
		releaseID, f.runID,
	); err != nil {
		t.Fatalf("bind run to cloned release: %v", err)
	}
}

func (f *llmTestFixture) chatClient(t *testing.T, provider llmgw.Provider) llmgwv1.LLMGatewayClient {
	t.Helper()
	svc := llmgw.NewService(f.pool, provider)
	lis := bufconn.Listen(1024 * 1024)
	server := grpc.NewServer()
	llmgwv1.RegisterLLMGatewayServer(server, svc)
	done := make(chan error, 1)
	go func() { done <- server.Serve(lis) }()
	t.Cleanup(func() {
		server.Stop()
		_ = lis.Close()
		select {
		case err := <-done:
			if err != nil && !errors.Is(err, grpc.ErrServerStopped) {
				t.Errorf("llmgw bufconn Serve: %v", err)
			}
		case <-time.After(5 * time.Second):
			t.Error("llmgw bufconn Serve 未退出")
		}
	})

	conn, err := grpc.NewClient(
		"passthrough:///llmgw-bufnet",
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
			return lis.DialContext(ctx)
		}),
	)
	if err != nil {
		t.Fatalf("创建 llmgw 客户端: %v", err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	return llmgwv1.NewLLMGatewayClient(conn)
}

func (f *llmTestFixture) chatRequest(logicalCallID string) *llmgwv1.ChatRequest {
	trace := commonv1Trace()
	return &llmgwv1.ChatRequest{
		Trace:                 &trace,
		Role:                  "profile",
		Messages:              []*llmgwv1.Msg{{Role: "user", Content: "请概括这份已脱敏画像"}},
		Temperature:           0,
		MaxTokens:             800,
		TimeoutMs:             2000,
		SessionId:             f.sessionID,
		RunId:                 f.runID,
		ReleaseId:             f.releaseID,
		LogicalCallId:         logicalCallID,
		PromptTemplateKey:     "profile_explain",
		PromptTemplateVersion: "v1",
		PromptVariables: map[string]string{
			"candidate_name": "湖南考生",
			"topic":          "已脱敏画像",
		},
		DataClassification: "PUBLIC_DATA",
	}
}

var commonTrace = commonv1Trace()

func commonv1Trace() commonv1.Trace {
	return commonv1.Trace{TraceId: "trace-32", SessionId: "untrusted-trace-session", Node: "profile"}
}

func trustedChatContext(principal string) context.Context {
	return metadata.AppendToOutgoingContext(context.Background(), "x-principal-id", principal)
}

func assertStableError(t *testing.T, err error, wantCode codes.Code, wantReason string) {
	t.Helper()
	if err == nil {
		t.Fatalf("期望错误 %s/%s，实际成功", wantCode, wantReason)
	}
	if got := status.Code(err); got != wantCode {
		t.Errorf("gRPC code = %s, want %s", got, wantCode)
	}
	if got := kerrors.Reason(err); got != wantReason {
		t.Errorf("stable reason = %q, want %q", got, wantReason)
	}
}

func collectStreamEvents(
	t *testing.T,
	stream llmgwv1.LLMGateway_StreamChatClient,
) ([]*llmgwv1.ChatDelta, error) {
	t.Helper()
	var events []*llmgwv1.ChatDelta
	for {
		delta, err := stream.Recv()
		if errors.Is(err, io.EOF) {
			return events, nil
		}
		if err != nil {
			return events, err
		}
		events = append(events, delta)
	}
}

func valueOrEmptyForTest(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}
