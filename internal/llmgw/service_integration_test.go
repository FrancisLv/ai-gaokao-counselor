//go:build integration

package llmgw_test

import (
	"context"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	kerrors "github.com/go-kratos/kratos/v2/errors"
	"github.com/go-kratos/kratos/v2/log"
	kgrpc "github.com/go-kratos/kratos/v2/transport/grpc"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/reflect/protoreflect"

	commonv1 "github.com/choucisan/ai-gaokao-counselor/api/common/v1"
	llmgwv1 "github.com/choucisan/ai-gaokao-counselor/api/llmgw/v1"
	"github.com/choucisan/ai-gaokao-counselor/internal/llmgw"
)

type providerCall struct {
	attemptID string
	model     string
	maxTokens int32
	messages  []llmgw.ProviderMessage
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
		attemptID: req.AttemptID,
		model:     req.Model,
		maxTokens: req.MaxOutputTokens,
		messages:  append([]llmgw.ProviderMessage(nil), req.Messages...),
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
	applyLLMGatewayMigrationsThrough(t, pool, 31)
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

func (f *llmTestFixture) chatClient(t *testing.T, provider llmgw.Provider) llmgwv1.LLMGatewayClient {
	t.Helper()
	svc := llmgw.NewService(f.pool, provider)
	lis := bufconn.Listen(1024 * 1024)
	server := kgrpc.NewServer(kgrpc.Timeout(30*time.Second), kgrpc.Logger(log.DefaultLogger))
	llmgwv1.RegisterLLMGatewayServer(server, svc)
	done := make(chan error, 1)
	go func() { done <- server.Serve(lis) }()
	t.Cleanup(func() {
		server.Server.Stop()
		if err := server.Stop(context.Background()); err != nil {
			t.Errorf("停止 llmgw bufconn: %v", err)
		}
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
	return &llmgwv1.ChatRequest{
		Trace:              &commonTrace,
		Role:               "profile",
		Messages:           []*llmgwv1.Msg{{Role: "user", Content: "请概括这份已脱敏画像"}},
		Temperature:        0,
		MaxTokens:          800,
		TimeoutMs:          2000,
		SessionId:          f.sessionID,
		RunId:              f.runID,
		ReleaseId:          f.releaseID,
		LogicalCallId:      logicalCallID,
		PromptTemplateKey:  "profile_explain",
		ResponseSchemaKey:  "profile_explain_v1",
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

func valueOrEmptyForTest(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}

func TestChatCommitsBudgetAndLedgerThroughGeneratedClient(t *testing.T) {
	fixture := newLLMTestFixture(t)
	var (
		preCallLogicalStatus string
		preCallAttemptStatus string
		preCallTokens        int64
		preCallCost          int64
	)
	provider := &fakeProvider{response: llmgw.ProviderResponse{
		Content:           "画像解释",
		InputTokens:       1000,
		OutputTokens:      500,
		ProviderRequestID: "provider-request-1",
		FinishReason:      "STOP",
	}, onCall: func(ctx context.Context) {
		if err := fixture.pool.QueryRow(ctx, `
			SELECT logical.status, attempt.status,
			       budget.provider_tokens_reserved,
			       budget.cost_reserved_micro_usd
			FROM llm_logical_call logical
			JOIN llm_provider_attempt attempt
			  ON attempt.logical_call_pk = logical.logical_call_pk
			JOIN llm_budget_scope budget
			  ON budget.budget_scope_id = logical.budget_scope_id
			WHERE logical.logical_call_id = 'logical-chat-success'`,
		).Scan(
			&preCallLogicalStatus, &preCallAttemptStatus,
			&preCallTokens, &preCallCost,
		); err != nil {
			t.Errorf("provider 调用前读取预占: %v", err)
		}
	}}
	client := fixture.chatClient(t, provider)

	resp, err := client.Chat(
		trustedChatContext(fixture.principal),
		fixture.chatRequest("logical-chat-success"),
	)
	if err != nil {
		t.Fatalf("Chat: %v", err)
	}

	calls := provider.callsSnapshot()
	if len(calls) != 1 {
		t.Fatalf("provider calls = %d, want 1", len(calls))
	}
	if preCallLogicalStatus != "IN_PROGRESS" || preCallAttemptStatus != "IN_PROGRESS" ||
		preCallTokens <= 0 || preCallCost <= 0 {
		t.Fatalf(
			"provider 前预占状态 = logical %q attempt %q tokens %d cost %d",
			preCallLogicalStatus, preCallAttemptStatus, preCallTokens, preCallCost,
		)
	}
	if calls[0].model != fixture.model {
		t.Fatalf("route model = %q, want %q", calls[0].model, fixture.model)
	}
	if got := resp.GetUsage().GetInputTokens(); got != 1000 {
		t.Fatalf("typed input tokens = %d, want 1000", got)
	}
	if got := resp.GetUsage().GetOutputTokens(); got != 500 {
		t.Fatalf("typed output tokens = %d, want 500", got)
	}
	if got := resp.GetCostDetail().GetActualMicroUsd(); got != 2000 {
		t.Fatalf("actual cost = %d USD_MICRO, want 2000", got)
	}
	if got := resp.GetCostDetail().GetSettlementStatus(); got != "SETTLED" {
		t.Fatalf("settlement status = %q, want SETTLED", got)
	}
	if resp.GetRouteVersion() != fixture.route || resp.GetPriceTableVersion() != fixture.priceTable {
		t.Fatalf("governance identity = %q/%q", resp.GetRouteVersion(), resp.GetPriceTableVersion())
	}
	if resp.GetModel() != fixture.model || resp.GetLogicalCallId() != "logical-chat-success" {
		t.Fatalf("response identity = model %q logical %q", resp.GetModel(), resp.GetLogicalCallId())
	}
	if len(resp.GetAttempts()) != 1 {
		t.Fatalf("attempt summaries = %d, want 1", len(resp.GetAttempts()))
	}
	attempt := resp.GetAttempts()[0]
	if attempt.GetAttemptId() == "" || attempt.GetStatus() != "SUCCEEDED" || attempt.GetCostMicroUsd() != 2000 {
		t.Fatalf("attempt = %+v", attempt)
	}

	var (
		scopeID        string
		logicalID      string
		publicAttempt  string
		settledTokens  int64
		reservedTokens int64
		settledCost    int64
		reservedCost   int64
		ledgerID       string
		callStatus     string
	)
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT
			budget.budget_scope_id::text,
			call.logical_call_id,
			attempt.attempt_id::text,
			budget.provider_tokens_settled,
			budget.provider_tokens_reserved,
			budget.cost_settled_micro_usd,
			budget.cost_reserved_micro_usd,
			call.ledger_entry_id::text,
			call.status
		FROM llm_budget_scope budget
		JOIN llm_logical_call call
		  ON call.budget_scope_id = budget.budget_scope_id
		JOIN llm_provider_attempt attempt
		  ON attempt.logical_call_id = call.logical_call_id
		WHERE call.principal_id = $1
		  AND call.logical_call_id = 'logical-chat-success'`,
		fixture.principal,
	).Scan(
		&scopeID, &logicalID, &publicAttempt,
		&settledTokens, &reservedTokens,
		&settledCost, &reservedCost,
		&ledgerID, &callStatus,
	); err != nil {
		t.Fatalf("查询权威账本: %v", err)
	}
	if scopeID == "" || logicalID != "logical-chat-success" || publicAttempt != attempt.GetAttemptId() {
		t.Fatalf("ledger identity = scope %q logical %q attempt %q", scopeID, logicalID, publicAttempt)
	}
	if ledgerID != resp.GetLedgerEntryId() || callStatus != "SUCCEEDED" {
		t.Fatalf("ledger response mismatch = ledger %q status %q", ledgerID, callStatus)
	}
	if settledTokens != 1500 || reservedTokens != 0 || settledCost != 2000 || reservedCost != 0 {
		t.Fatalf(
			"budget settlement = tokens %d/%d cost %d/%d",
			settledTokens, reservedTokens, settledCost, reservedCost,
		)
	}
}

func TestProtocolKeepsLegacyChatFieldNumbers(t *testing.T) {
	t.Helper()
	cases := []struct {
		name string
		want map[int32]string
	}{
		{
			name: "ChatRequest",
			want: map[int32]string{
				1: "trace", 2: "role", 3: "messages", 4: "json_schema",
				5: "temperature", 6: "max_tokens", 7: "timeout_ms",
			},
		},
		{
			name: "ChatResponse",
			want: map[int32]string{
				1: "content", 2: "model", 3: "in_tokens", 4: "out_tokens", 5: "cost",
			},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			actual := map[int32]string{}
			switch tc.name {
			case "ChatRequest":
				requestFields := (&llmgwv1.ChatRequest{}).ProtoReflect().Descriptor().Fields()
				for i := 0; i < requestFields.Len(); i++ {
					field := requestFields.Get(i)
					actual[int32(field.Number())] = string(field.Name())
				}
			case "ChatResponse":
				responseFields := (&llmgwv1.ChatResponse{}).ProtoReflect().Descriptor().Fields()
				for i := 0; i < responseFields.Len(); i++ {
					field := responseFields.Get(i)
					actual[int32(field.Number())] = string(field.Name())
				}
			}
			for number, name := range tc.want {
				if actual[number] != name {
					t.Errorf("%s field %d = %q, want %q", tc.name, number, actual[number], name)
				}
			}
		})
	}

	requestFields := (&llmgwv1.ChatRequest{}).ProtoReflect().Descriptor().Fields()
	for _, forbidden := range []string{"principal_id", "model", "provider", "base_url", "retry_count", "max_attempts"} {
		if requestFields.ByName(protoreflect.Name(forbidden)) != nil {
			t.Errorf("ChatRequest 暴露了不允许调用方覆盖的字段 %q", forbidden)
		}
	}
}

func TestChatUsesTrustedMetadataInsteadOfTraceOrBody(t *testing.T) {
	fixture := newLLMTestFixture(t)
	provider := &fakeProvider{response: llmgw.ProviderResponse{
		Content: "ok", InputTokens: 1, OutputTokens: 1, FinishReason: "STOP",
	}}
	client := fixture.chatClient(t, provider)
	req := fixture.chatRequest("logical-identity")
	req.Trace.SessionId = "forged-session"

	_, err := client.Chat(trustedChatContext("principal-other"), req)
	assertStableError(t, err, codes.PermissionDenied, llmgw.ReasonRunNotAllowed)
	if calls := provider.callsSnapshot(); len(calls) != 0 {
		t.Fatalf("身份拒绝仍调用 provider %d 次", len(calls))
	}

	_, err = client.Chat(context.Background(), fixture.chatRequest("logical-no-principal"))
	assertStableError(t, err, codes.Unauthenticated, llmgw.ReasonPrincipalRequired)
}

func TestChatRejectsDisabledModelBeforeProvider(t *testing.T) {
	fixture := newLLMTestFixture(t)
	if _, err := fixture.pool.Exec(context.Background(), `
		UPDATE llm_model_allowlist
		SET enabled = FALSE
		WHERE release_id = $1 AND model_id = $2`,
		fixture.releaseID, fixture.model,
	); err != nil {
		t.Fatalf("禁用模型: %v", err)
	}
	provider := &fakeProvider{response: llmgw.ProviderResponse{
		Content: "unexpected", InputTokens: 1, OutputTokens: 1, FinishReason: "STOP",
	}}
	client := fixture.chatClient(t, provider)

	_, err := client.Chat(
		trustedChatContext(fixture.principal),
		fixture.chatRequest("logical-model-disabled"),
	)
	assertStableError(t, err, codes.FailedPrecondition, llmgw.ReasonModelNotAllowed)
	if calls := provider.callsSnapshot(); len(calls) != 0 {
		t.Fatalf("模型拒绝仍调用 provider %d 次", len(calls))
	}
}

func TestChatRejectsRevokedReleaseBeforeProvider(t *testing.T) {
	fixture := newLLMTestFixture(t)
	if _, err := fixture.pool.Exec(context.Background(), `
		UPDATE release
		SET status = 'REVOKED', revoked_at = NOW()
		WHERE release_id = $1`,
		fixture.releaseID,
	); err != nil {
		t.Fatalf("撤销 release: %v", err)
	}
	provider := &fakeProvider{response: llmgw.ProviderResponse{
		Content: "unexpected", InputTokens: 1, OutputTokens: 1, FinishReason: "STOP",
	}}
	client := fixture.chatClient(t, provider)

	_, err := client.Chat(
		trustedChatContext(fixture.principal),
		fixture.chatRequest("logical-release-revoked"),
	)
	assertStableError(t, err, codes.FailedPrecondition, llmgw.ReasonReleaseRevoked)
	if calls := provider.callsSnapshot(); len(calls) != 0 {
		t.Fatalf("撤销 release 仍调用 provider %d 次", len(calls))
	}
}

func TestChatRejectsUnpublishedReleaseBeforeProvider(t *testing.T) {
	fixture := newLLMTestFixture(t)
	if _, err := fixture.pool.Exec(context.Background(), `
		UPDATE release
		SET status = 'STAGING', published_at = NULL, revoked_at = NULL
		WHERE release_id = $1`,
		fixture.releaseID,
	); err != nil {
		t.Fatalf("改为未发布 release: %v", err)
	}
	provider := &fakeProvider{response: llmgw.ProviderResponse{
		Content: "unexpected", InputTokens: 1, OutputTokens: 1, FinishReason: "STOP",
	}}
	client := fixture.chatClient(t, provider)

	_, err := client.Chat(
		trustedChatContext(fixture.principal),
		fixture.chatRequest("logical-release-staging"),
	)
	assertStableError(t, err, codes.FailedPrecondition, llmgw.ReasonReleaseUnavailable)
	if calls := provider.callsSnapshot(); len(calls) != 0 {
		t.Fatalf("未发布 release 仍调用 provider %d 次", len(calls))
	}
}

func TestChatRejectsMissingRunBindingBeforeProvider(t *testing.T) {
	fixture := newLLMTestFixture(t)
	provider := &fakeProvider{response: llmgw.ProviderResponse{
		Content: "unexpected", InputTokens: 1, OutputTokens: 1, FinishReason: "STOP",
	}}
	client := fixture.chatClient(t, provider)
	req := fixture.chatRequest("logical-invalid-run")
	req.RunId = uuid.NewString()

	_, err := client.Chat(trustedChatContext(fixture.principal), req)
	assertStableError(t, err, codes.PermissionDenied, llmgw.ReasonRunNotAllowed)
	if calls := provider.callsSnapshot(); len(calls) != 0 {
		t.Fatalf("非法 run 仍调用 provider %d 次", len(calls))
	}
}

func TestChatRejectsUnknownRoleBeforeProvider(t *testing.T) {
	fixture := newLLMTestFixture(t)
	provider := &fakeProvider{response: llmgw.ProviderResponse{
		Content: "unexpected", InputTokens: 1, OutputTokens: 1, FinishReason: "STOP",
	}}
	client := fixture.chatClient(t, provider)
	req := fixture.chatRequest("logical-role")
	req.Role = "strategy"

	_, err := client.Chat(trustedChatContext(fixture.principal), req)
	assertStableError(t, err, codes.InvalidArgument, llmgw.ReasonRoleNotAllowed)
	if calls := provider.callsSnapshot(); len(calls) != 0 {
		t.Fatalf("角色拒绝仍调用 provider %d 次", len(calls))
	}
}

func TestChatRejectsMissingPriceBeforeProvider(t *testing.T) {
	fixture := newLLMTestFixture(t)
	emptyPriceTable := "price_m5a_32_empty"
	if _, err := fixture.pool.Exec(context.Background(), `
		INSERT INTO llm_price_table (
			price_table_version, currency_unit, effective_at, published_at
		) VALUES ($1, 'USD_MICRO', NOW() - INTERVAL '1 minute', NOW())`,
		emptyPriceTable,
	); err != nil {
		t.Fatalf("创建空价格版本: %v", err)
	}
	if _, err := fixture.pool.Exec(context.Background(), `
		UPDATE llm_role_route
		SET price_table_version = $1
		WHERE release_id = $2 AND role = 'profile'`,
		emptyPriceTable, fixture.releaseID,
	); err != nil {
		t.Fatalf("绑定空价格版本: %v", err)
	}
	provider := &fakeProvider{response: llmgw.ProviderResponse{
		Content: "unexpected", InputTokens: 1, OutputTokens: 1, FinishReason: "STOP",
	}}
	client := fixture.chatClient(t, provider)

	_, err := client.Chat(
		trustedChatContext(fixture.principal),
		fixture.chatRequest("logical-price"),
	)
	assertStableError(t, err, codes.FailedPrecondition, llmgw.ReasonPriceUnavailable)
	if calls := provider.callsSnapshot(); len(calls) != 0 {
		t.Fatalf("价格拒绝仍调用 provider %d 次", len(calls))
	}
}

func TestChatBudgetRejectCreatesNoProviderAttempt(t *testing.T) {
	fixture := newLLMTestFixture(t)
	if _, err := fixture.pool.Exec(context.Background(), `
		INSERT INTO llm_budget_scope (
			budget_scope_id, principal_id, session_id, token_limit,
			cost_limit_micro_usd, provider_tokens_reserved, cost_reserved_micro_usd
		) VALUES (gen_random_uuid(), $1, $2, 1, 1, 1, 1)`,
		fixture.principal, fixture.sessionID,
	); err != nil {
		t.Fatalf("预占预算: %v", err)
	}
	provider := &fakeProvider{response: llmgw.ProviderResponse{
		Content: "unexpected", InputTokens: 1, OutputTokens: 1, FinishReason: "STOP",
	}}
	client := fixture.chatClient(t, provider)

	_, err := client.Chat(
		trustedChatContext(fixture.principal),
		fixture.chatRequest("logical-budget"),
	)
	assertStableError(t, err, codes.ResourceExhausted, llmgw.ReasonBudgetExceeded)
	if calls := provider.callsSnapshot(); len(calls) != 0 {
		t.Fatalf("预算拒绝仍调用 provider %d 次", len(calls))
	}
	var attempts int
	if err := fixture.pool.QueryRow(context.Background(),
		`SELECT count(*) FROM llm_provider_attempt WHERE logical_call_id = 'logical-budget'`,
	).Scan(&attempts); err != nil {
		t.Fatalf("统计 attempt: %v", err)
	}
	if attempts != 0 {
		t.Fatalf("预算拒绝创建了 %d 个 attempt", attempts)
	}
}

func TestBudgetOverrunBlocksSubsequentChatBeforeProvider(t *testing.T) {
	fixture := newLLMTestFixture(t)
	provider := &fakeProvider{response: llmgw.ProviderResponse{
		Content: "用量异常", InputTokens: 1_000_000, OutputTokens: 1,
		FinishReason: "STOP",
	}}
	client := fixture.chatClient(t, provider)

	_, err := client.Chat(
		trustedChatContext(fixture.principal),
		fixture.chatRequest("logical-overrun-first"),
	)
	assertStableError(t, err, codes.ResourceExhausted, llmgw.ReasonBudgetOverrun)
	if calls := provider.callsSnapshot(); len(calls) != 1 {
		t.Fatalf("首次超预留调用 provider %d 次, want 1", len(calls))
	}

	var budgetStatus, callStatus string
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT budget.budget_status, call.status
		FROM llm_budget_scope budget
		JOIN llm_logical_call call
		  ON call.budget_scope_id = budget.budget_scope_id
		WHERE call.logical_call_id = 'logical-overrun-first'`,
	).Scan(&budgetStatus, &callStatus); err != nil {
		t.Fatalf("读取超限状态: %v", err)
	}
	if budgetStatus != "BUDGET_OVERRUN" || callStatus != "BUDGET_OVERRUN" {
		t.Fatalf("超限状态 = budget %q call %q", budgetStatus, callStatus)
	}

	_, err = client.Chat(
		trustedChatContext(fixture.principal),
		fixture.chatRequest("logical-overrun-next"),
	)
	assertStableError(t, err, codes.ResourceExhausted, llmgw.ReasonBudgetOverrun)
	if calls := provider.callsSnapshot(); len(calls) != 1 {
		t.Fatalf("超限作用域后续调用 provider %d 次, want 1", len(calls))
	}
}

func TestExplicitProviderRejectionReleasesEntireReservation(t *testing.T) {
	fixture := newLLMTestFixture(t)
	if _, err := fixture.pool.Exec(context.Background(), `
		INSERT INTO llm_budget_scope (
			budget_scope_id, principal_id, session_id, token_limit,
			cost_limit_micro_usd
		) VALUES (gen_random_uuid(), $1, $2, 5000, 5000)`,
		fixture.principal, fixture.sessionID,
	); err != nil {
		t.Fatalf("seed constrained budget scope: %v", err)
	}
	provider := &fakeProvider{}
	provider.setBehavior(llmgw.ProviderResponse{}, llmgw.ErrProviderNotCharged)
	client := fixture.chatClient(t, provider)

	_, err := client.Chat(
		trustedChatContext(fixture.principal),
		fixture.chatRequest("logical-uncharged"),
	)
	assertStableError(t, err, codes.Unavailable, llmgw.ReasonProviderUnavailable)

	var (
		budgetStatus   string
		reservedTokens int64
		settledTokens  int64
		reservedCost   int64
		settledCost    int64
		attemptStatus  string
		attemptError   string
		callStatus     string
	)
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT budget.budget_status,
		       budget.provider_tokens_reserved,
		       budget.provider_tokens_settled,
		       budget.cost_reserved_micro_usd,
		       budget.cost_settled_micro_usd,
		       attempt.status,
		       attempt.error_code,
		       call.status
		FROM llm_budget_scope budget
		JOIN llm_logical_call call
		  ON call.budget_scope_id = budget.budget_scope_id
		JOIN llm_provider_attempt attempt
		  ON attempt.logical_call_pk = call.logical_call_pk
		WHERE call.logical_call_id = 'logical-uncharged'`,
	).Scan(
		&budgetStatus, &reservedTokens, &settledTokens,
		&reservedCost, &settledCost, &attemptStatus,
		&attemptError, &callStatus,
	); err != nil {
		t.Fatalf("读取免计费账本: %v", err)
	}
	if budgetStatus != "ACTIVE" ||
		reservedTokens != 0 || settledTokens != 0 ||
		reservedCost != 0 || settledCost != 0 {
		t.Fatalf(
			"免计费预算 = status %q tokens %d/%d cost %d/%d",
			budgetStatus, settledTokens, reservedTokens, settledCost, reservedCost,
		)
	}
	if attemptStatus != "FAILED_NO_CHARGE" ||
		attemptError != "PROVIDER_NOT_CHARGED" ||
		callStatus != "FAILED" {
		t.Fatalf(
			"免计费状态 = attempt %q/%q call %q",
			attemptStatus, attemptError, callStatus,
		)
	}
}

func TestConcurrentChatsEnforceTokenBudgetBeforeProvider(t *testing.T) {
	fixture := newLLMTestFixture(t)
	fixture.seedBudget(t, 5000, 150000)
	provider := &fakeProvider{
		response: llmgw.ProviderResponse{
			Content: "ok", InputTokens: 100, OutputTokens: 50,
			FinishReason: "STOP",
		},
		onCall: func(context.Context) {
			time.Sleep(100 * time.Millisecond)
		},
	}
	client := fixture.chatClient(t, provider)

	const requests = 12
	errs := make(chan error, requests)
	var wg sync.WaitGroup
	for i := 0; i < requests; i++ {
		wg.Add(1)
		go func(index int) {
			defer wg.Done()
			_, err := client.Chat(
				trustedChatContext(fixture.principal),
				fixture.chatRequest(fmt.Sprintf("logical-token-race-%02d", index)),
			)
			errs <- err
		}(i)
	}
	wg.Wait()
	close(errs)

	successes := 0
	rejections := 0
	for err := range errs {
		if err == nil {
			successes++
			continue
		}
		if status.Code(err) == codes.ResourceExhausted &&
			kerrors.Reason(err) == llmgw.ReasonBudgetExceeded {
			rejections++
			continue
		}
		t.Fatalf("并发 Chat 返回意外错误: %v", err)
	}
	if successes == 0 || rejections == 0 || successes+rejections != requests {
		t.Fatalf("并发预算结果 = success %d reject %d total %d", successes, rejections, requests)
	}
	if calls := provider.callsSnapshot(); len(calls) != successes {
		t.Fatalf("provider calls = %d, want %d successful reservations", len(calls), successes)
	}

	var (
		settledTokens  int64
		reservedTokens int64
		settledCost    int64
		reservedCost   int64
		attempts       int
	)
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT provider_tokens_settled,
		       provider_tokens_reserved,
		       cost_settled_micro_usd,
		       cost_reserved_micro_usd
		FROM llm_budget_scope
		WHERE principal_id = $1 AND session_id = $2`,
		fixture.principal, fixture.sessionID,
	).Scan(&settledTokens, &reservedTokens, &settledCost, &reservedCost); err != nil {
		t.Fatalf("读取并发 token 预算: %v", err)
	}
	if err := fixture.pool.QueryRow(context.Background(),
		`SELECT count(*) FROM llm_provider_attempt`,
	).Scan(&attempts); err != nil {
		t.Fatalf("统计并发 token attempt: %v", err)
	}
	if settledTokens+reservedTokens > 5000 ||
		settledCost+reservedCost > 150000 ||
		attempts != successes {
		t.Fatalf(
			"并发 token 账本 = tokens %d/%d cost %d/%d attempts %d success %d",
			settledTokens, reservedTokens, settledCost, reservedCost, attempts, successes,
		)
	}
}

func TestConcurrentChatsEnforceCostBudgetBeforeProvider(t *testing.T) {
	fixture := newLLMTestFixture(t)
	fixture.seedBudget(t, 50000, 5000)
	provider := &fakeProvider{
		response: llmgw.ProviderResponse{
			Content: "ok", InputTokens: 100, OutputTokens: 50,
			FinishReason: "STOP",
		},
		onCall: func(context.Context) {
			time.Sleep(100 * time.Millisecond)
		},
	}
	client := fixture.chatClient(t, provider)

	const requests = 12
	errs := make(chan error, requests)
	var wg sync.WaitGroup
	for i := 0; i < requests; i++ {
		wg.Add(1)
		go func(index int) {
			defer wg.Done()
			_, err := client.Chat(
				trustedChatContext(fixture.principal),
				fixture.chatRequest(fmt.Sprintf("logical-cost-race-%02d", index)),
			)
			errs <- err
		}(i)
	}
	wg.Wait()
	close(errs)

	successes := 0
	rejections := 0
	for err := range errs {
		if err == nil {
			successes++
			continue
		}
		if status.Code(err) == codes.ResourceExhausted &&
			kerrors.Reason(err) == llmgw.ReasonBudgetExceeded {
			rejections++
			continue
		}
		t.Fatalf("并发 Chat 返回意外错误: %v", err)
	}
	if successes == 0 || rejections == 0 || successes+rejections != requests {
		t.Fatalf("并发成本结果 = success %d reject %d total %d", successes, rejections, requests)
	}
	if calls := provider.callsSnapshot(); len(calls) != successes {
		t.Fatalf("provider calls = %d, want %d successful reservations", len(calls), successes)
	}

	var (
		settledTokens  int64
		reservedTokens int64
		settledCost    int64
		reservedCost   int64
		attempts       int
	)
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT provider_tokens_settled,
		       provider_tokens_reserved,
		       cost_settled_micro_usd,
		       cost_reserved_micro_usd
		FROM llm_budget_scope
		WHERE principal_id = $1 AND session_id = $2`,
		fixture.principal, fixture.sessionID,
	).Scan(&settledTokens, &reservedTokens, &settledCost, &reservedCost); err != nil {
		t.Fatalf("读取并发成本预算: %v", err)
	}
	if err := fixture.pool.QueryRow(context.Background(),
		`SELECT count(*) FROM llm_provider_attempt`,
	).Scan(&attempts); err != nil {
		t.Fatalf("统计并发成本 attempt: %v", err)
	}
	if settledTokens+reservedTokens > 50000 ||
		settledCost+reservedCost > 5000 ||
		attempts != successes {
		t.Fatalf(
			"并发成本账本 = tokens %d/%d cost %d/%d attempts %d success %d",
			settledTokens, reservedTokens, settledCost, reservedCost, attempts, successes,
		)
	}
}

func TestBudgetScopeAccumulatesAcrossRunsAndIsolatesSessions(t *testing.T) {
	fixture := newLLMTestFixture(t)
	provider := &fakeProvider{response: llmgw.ProviderResponse{
		Content: "ok", InputTokens: 1000, OutputTokens: 500, FinishReason: "STOP",
	}}
	client := fixture.chatClient(t, provider)

	firstRequest := fixture.chatRequest("logical-budget-scope")
	if _, err := client.Chat(trustedChatContext(fixture.principal), firstRequest); err != nil {
		t.Fatalf("首个 run Chat: %v", err)
	}

	secondRunID := uuid.NewString()
	if _, err := fixture.pool.Exec(context.Background(), `
		INSERT INTO run (
			run_id, session_id, principal_id, task_kind, task_input,
			state, stage, release_id
		) VALUES ($1, $2, $3, 'REVISE_PLAN', '{}'::jsonb, 'RUNNING', 'llm', $4)`,
		secondRunID, fixture.sessionID, fixture.principal, fixture.releaseID,
	); err != nil {
		t.Fatalf("seed second run: %v", err)
	}
	secondRequest := fixture.chatRequest("logical-second-run")
	secondRequest.RunId = secondRunID
	if _, err := client.Chat(trustedChatContext(fixture.principal), secondRequest); err != nil {
		t.Fatalf("同 session 第二 run Chat: %v", err)
	}

	secondSessionID := uuid.NewString()
	thirdRunID := uuid.NewString()
	if _, err := fixture.pool.Exec(context.Background(), `
		INSERT INTO chat_session (id, principal_id, status)
		VALUES ($1, $2, 'active')`,
		secondSessionID, fixture.principal,
	); err != nil {
		t.Fatalf("seed second session: %v", err)
	}
	if _, err := fixture.pool.Exec(context.Background(), `
		INSERT INTO run (
			run_id, session_id, principal_id, task_kind, task_input,
			state, stage, release_id
		) VALUES ($1, $2, $3, 'GENERATE_PLAN', '{}'::jsonb, 'RUNNING', 'llm', $4)`,
		thirdRunID, secondSessionID, fixture.principal, fixture.releaseID,
	); err != nil {
		t.Fatalf("seed second session run: %v", err)
	}
	thirdRequest := fixture.chatRequest("logical-budget-scope")
	thirdRequest.SessionId = secondSessionID
	thirdRequest.RunId = thirdRunID
	if _, err := client.Chat(trustedChatContext(fixture.principal), thirdRequest); err != nil {
		t.Fatalf("独立 session Chat: %v", err)
	}

	if calls := provider.callsSnapshot(); len(calls) != 3 {
		t.Fatalf("provider calls = %d, want 3", len(calls))
	}
	rows, err := fixture.pool.Query(context.Background(), `
		SELECT session_id::text,
		       token_limit,
		       cost_limit_micro_usd,
		       provider_tokens_settled,
		       provider_tokens_reserved,
		       cost_settled_micro_usd,
		       cost_reserved_micro_usd
		FROM llm_budget_scope
		WHERE principal_id = $1
		ORDER BY session_id`,
		fixture.principal,
	)
	if err != nil {
		t.Fatalf("读取预算作用域: %v", err)
	}
	defer rows.Close()
	scopes := map[string][6]int64{}
	for rows.Next() {
		var (
			sessionID string
			values    [6]int64
		)
		if err := rows.Scan(
			&sessionID, &values[0], &values[1], &values[2],
			&values[3], &values[4], &values[5],
		); err != nil {
			t.Fatalf("读取预算作用域行: %v", err)
		}
		scopes[sessionID] = values
	}
	if err := rows.Err(); err != nil {
		t.Fatalf("遍历预算作用域: %v", err)
	}
	if len(scopes) != 2 {
		t.Fatalf("budget scopes = %d, want 2", len(scopes))
	}
	firstScope, ok := scopes[fixture.sessionID]
	if !ok {
		t.Fatal("缺少首 session 的预算作用域")
	}
	secondScope, ok := scopes[secondSessionID]
	if !ok {
		t.Fatal("缺少第二 session 的预算作用域")
	}
	if firstScope != [6]int64{50000, 150000, 3000, 0, 4000, 0} {
		t.Fatalf("首 session 预算 = %v", firstScope)
	}
	if secondScope != [6]int64{50000, 150000, 1500, 0, 2000, 0} {
		t.Fatalf("第二 session 预算 = %v", secondScope)
	}
}

func TestMissingCallLogProjectionCannotChangeAuthoritativeBudget(t *testing.T) {
	fixture := newLLMTestFixture(t)
	if _, err := fixture.pool.Exec(context.Background(), `DROP TABLE llm_call_log`); err != nil {
		t.Fatalf("移除旧调用日志投影: %v", err)
	}
	provider := &fakeProvider{response: llmgw.ProviderResponse{
		Content: "投影缺失不影响结算", InputTokens: 200, OutputTokens: 100,
		FinishReason: "STOP",
	}}
	client := fixture.chatClient(t, provider)

	response, err := client.Chat(
		trustedChatContext(fixture.principal),
		fixture.chatRequest("logical-no-projection"),
	)
	if err != nil {
		t.Fatalf("投影缺失时 Chat: %v", err)
	}
	if response.GetUsage().GetInputTokens() != 200 ||
		response.GetUsage().GetOutputTokens() != 100 {
		t.Fatalf("投影缺失时响应 usage = %+v", response.GetUsage())
	}
	var settledTokens, settledCost int64
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT provider_tokens_settled, cost_settled_micro_usd
		FROM llm_budget_scope
		WHERE principal_id = $1 AND session_id = $2`,
		fixture.principal, fixture.sessionID,
	).Scan(&settledTokens, &settledCost); err != nil {
		t.Fatalf("读取权威预算: %v", err)
	}
	if settledTokens != 300 || settledCost != 400 {
		t.Fatalf("权威预算 = tokens %d cost %d", settledTokens, settledCost)
	}
}

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
}

func TestLLMGatewayRejectsMoreThanTwoAttempts(t *testing.T) {
	fixture := newLLMTestFixture(t)
	_, err := fixture.pool.Exec(context.Background(), `
		INSERT INTO llm_role_route (
			release_id, role, route_version, primary_model_id, fallback_model_ids,
			max_output_tokens, timeout_ms, max_attempts, price_table_version
		) VALUES (
			$1, 'report', 'route_report_three_attempts', $2,
			ARRAY[$2, $2]::TEXT[], 1000, 5000, 3, $3
		)`,
		fixture.releaseID, fixture.model, fixture.priceTable,
	)
	if err == nil {
		t.Fatal("route 的 max_attempts=3 应被数据库拒绝")
	}
}

func TestRequestLimitCannotRelaxPublishedRoute(t *testing.T) {
	fixture := newLLMTestFixture(t)
	provider := &fakeProvider{response: llmgw.ProviderResponse{
		Content: "unexpected", InputTokens: 1, OutputTokens: 1, FinishReason: "STOP",
	}}
	client := fixture.chatClient(t, provider)
	req := fixture.chatRequest("logical-relax-limit")
	req.MaxTokens = 1001

	_, err := client.Chat(trustedChatContext(fixture.principal), req)
	assertStableError(t, err, codes.InvalidArgument, llmgw.ReasonLimitExceeded)
	if calls := provider.callsSnapshot(); len(calls) != 0 {
		t.Fatalf("上限拒绝仍调用 provider %d 次", len(calls))
	}
}

func TestCompletionReplayReturnsStoredTypedResultAndSameAttempt(t *testing.T) {
	fixture := newLLMTestFixture(t)
	provider := &fakeProvider{response: llmgw.ProviderResponse{
		Content: "可重放结果", InputTokens: 10, OutputTokens: 5,
		ProviderRequestID: "provider-replay", FinishReason: "STOP",
	}}
	client := fixture.chatClient(t, provider)
	req := fixture.chatRequest("logical-replay")

	first, err := client.Chat(trustedChatContext(fixture.principal), req)
	if err != nil {
		t.Fatalf("首次 Chat: %v", err)
	}
	second, err := client.Chat(trustedChatContext(fixture.principal), req)
	if err != nil {
		t.Fatalf("重放 Chat: %v", err)
	}
	if calls := provider.callsSnapshot(); len(calls) != 1 {
		t.Fatalf("重放产生 provider 调用 %d 次, want 1", len(calls))
	}
	if first.GetLedgerEntryId() != second.GetLedgerEntryId() ||
		first.GetAttempts()[0].GetAttemptId() != second.GetAttempts()[0].GetAttemptId() {
		t.Fatal("重放未返回原账本/attempt 身份")
	}
	if second.GetContent() != "可重放结果" ||
		second.GetUsage().GetInputTokens() != 10 ||
		second.GetCostDetail().GetActualMicroUsd() != 20 {
		t.Fatalf("重放未返回原类型化结果: %+v", second)
	}
}

func TestSameLogicalCallWithChangedRequestConflicts(t *testing.T) {
	fixture := newLLMTestFixture(t)
	provider := &fakeProvider{response: llmgw.ProviderResponse{
		Content: "first", InputTokens: 1, OutputTokens: 1, FinishReason: "STOP",
	}}
	client := fixture.chatClient(t, provider)
	req := fixture.chatRequest("logical-conflict")
	if _, err := client.Chat(trustedChatContext(fixture.principal), req); err != nil {
		t.Fatalf("首次 Chat: %v", err)
	}
	changed := fixture.chatRequest("logical-conflict")
	changed.Messages[0].Content = "不同语义请求"
	_, err := client.Chat(trustedChatContext(fixture.principal), changed)
	assertStableError(t, err, codes.Aborted, llmgw.ReasonIdempotencyConflict)
	if calls := provider.callsSnapshot(); len(calls) != 1 {
		t.Fatalf("冲突路径产生 provider 调用 %d 次, want 1", len(calls))
	}
}

func TestRetryablePrimaryFailureFallsBackAndReplaysBothAttempts(t *testing.T) {
	fixture := newLLMTestFixture(t)
	fixture.enableFallback(t)
	provider := &fakeProvider{behaviors: []fakeBehavior{
		{
			err: &llmgw.ProviderFailure{
				Code:              llmgw.ReasonProviderRateLimited,
				Retryable:         true,
				ProviderRequestID: "provider-rate-limited",
			},
		},
		{
			response: llmgw.ProviderResponse{
				Content:           "备用模型结果",
				InputTokens:       10,
				OutputTokens:      5,
				ProviderRequestID: "provider-fallback",
				FinishReason:      "STOP",
			},
		},
	}}
	client := fixture.chatClient(t, provider)
	req := fixture.chatRequest("logical-fallback")

	first, err := client.Chat(trustedChatContext(fixture.principal), req)
	if err != nil {
		t.Fatalf("主备切换 Chat: %v", err)
	}
	if first.GetModel() != fixture.fallback || first.GetContent() != "备用模型结果" {
		t.Fatalf("最终结果未归属备用模型: %+v", first)
	}
	if first.GetUsage().GetInputTokens() != 10 ||
		first.GetUsage().GetOutputTokens() != 5 ||
		first.GetCostDetail().GetActualMicroUsd() != 40 {
		t.Fatalf("最终 usage/cost = %+v", first.GetCostDetail())
	}
	if len(first.GetAttempts()) != 2 {
		t.Fatalf("attempt summaries = %d, want 2", len(first.GetAttempts()))
	}
	primary, fallback := first.GetAttempts()[0], first.GetAttempts()[1]
	if primary.GetAttemptId() == "" ||
		primary.GetAttemptId() == fallback.GetAttemptId() ||
		primary.GetModelId() != fixture.model ||
		primary.GetStatus() != "FAILED_NO_CHARGE" ||
		primary.GetErrorCode() != llmgw.ReasonProviderRateLimited ||
		primary.GetProviderRequestId() != "provider-rate-limited" ||
		primary.GetCostMicroUsd() != 0 {
		t.Fatalf("主模型 attempt = %+v", primary)
	}
	if fallback.GetAttemptId() == "" ||
		fallback.GetModelId() != fixture.fallback ||
		fallback.GetStatus() != "SUCCEEDED" ||
		fallback.GetProviderRequestId() != "provider-fallback" ||
		fallback.GetCostMicroUsd() != 40 {
		t.Fatalf("备用模型 attempt = %+v", fallback)
	}
	calls := provider.callsSnapshot()
	if len(calls) != 2 ||
		calls[0].model != fixture.model ||
		calls[1].model != fixture.fallback ||
		calls[0].attemptID == calls[1].attemptID ||
		calls[0].attemptID != primary.GetAttemptId() ||
		calls[1].attemptID != fallback.GetAttemptId() {
		t.Fatalf("provider attempts = %+v", calls)
	}

	replayed, err := client.Chat(trustedChatContext(fixture.principal), req)
	if err != nil {
		t.Fatalf("重放主备切换结果: %v", err)
	}
	if len(provider.callsSnapshot()) != 2 {
		t.Fatal("幂等重放新增了 provider 调用")
	}
	if replayed.GetLedgerEntryId() != first.GetLedgerEntryId() ||
		len(replayed.GetAttempts()) != 2 ||
		replayed.GetAttempts()[0].GetAttemptId() != primary.GetAttemptId() ||
		replayed.GetAttempts()[1].GetAttemptId() != fallback.GetAttemptId() ||
		replayed.GetModel() != fixture.fallback ||
		replayed.GetCostDetail().GetActualMicroUsd() != 40 {
		t.Fatalf("重放账本身份 = %+v", replayed)
	}

	type attemptLedgerRow struct {
		no, cost          int64
		in, out           *int64
		id, model, status string
		request, error    *string
		started, finished bool
	}
	rows, err := fixture.pool.Query(context.Background(), `
		SELECT attempt_no, attempt_id::text, model_id, status,
		       provider_request_id, input_tokens, output_tokens,
		       cost_micro_usd, error_code,
		       started_at IS NOT NULL, finished_at IS NOT NULL
		FROM llm_provider_attempt
		WHERE logical_call_pk = (
			SELECT logical_call_pk
			FROM llm_logical_call
			WHERE logical_call_id = 'logical-fallback'
		)
		ORDER BY attempt_no`)
	if err != nil {
		t.Fatalf("读取主备切换 attempt 账本: %v", err)
	}
	defer rows.Close()
	var ledgerAttempts []attemptLedgerRow
	for rows.Next() {
		var row attemptLedgerRow
		if err := rows.Scan(
			&row.no, &row.id, &row.model, &row.status, &row.request,
			&row.in, &row.out, &row.cost, &row.error,
			&row.started, &row.finished,
		); err != nil {
			t.Fatalf("解析主备切换 attempt 账本: %v", err)
		}
		ledgerAttempts = append(ledgerAttempts, row)
	}
	if err := rows.Err(); err != nil {
		t.Fatalf("遍历主备切换 attempt 账本: %v", err)
	}
	if len(ledgerAttempts) != 2 ||
		ledgerAttempts[0].no != 1 ||
		ledgerAttempts[0].id != primary.GetAttemptId() ||
		ledgerAttempts[0].model != fixture.model ||
		ledgerAttempts[0].status != "FAILED_NO_CHARGE" ||
		valueOrEmptyForTest(ledgerAttempts[0].request) != "provider-rate-limited" ||
		valueOrEmptyForTest(ledgerAttempts[0].error) != llmgw.ReasonProviderRateLimited ||
		ledgerAttempts[0].cost != 0 ||
		!ledgerAttempts[0].started ||
		!ledgerAttempts[0].finished ||
		ledgerAttempts[1].no != 2 ||
		ledgerAttempts[1].id != fallback.GetAttemptId() ||
		ledgerAttempts[1].model != fixture.fallback ||
		ledgerAttempts[1].status != "SUCCEEDED" ||
		valueOrEmptyForTest(ledgerAttempts[1].request) != "provider-fallback" ||
		valueOrEmptyForTest(ledgerAttempts[1].error) != "" ||
		ledgerAttempts[1].in == nil ||
		*ledgerAttempts[1].in != 10 ||
		ledgerAttempts[1].out == nil ||
		*ledgerAttempts[1].out != 5 ||
		ledgerAttempts[1].cost != 40 ||
		!ledgerAttempts[1].started ||
		!ledgerAttempts[1].finished {
		t.Fatalf("主备切换权威账本 = %+v", ledgerAttempts)
	}
}

func TestNonRetryableProviderFailuresDoNotUseFallback(t *testing.T) {
	tests := []struct {
		name       string
		code       string
		wantStatus codes.Code
	}{
		{
			name:       "bad request",
			code:       llmgw.ReasonProviderBadRequest,
			wantStatus: codes.InvalidArgument,
		},
		{
			name:       "authentication",
			code:       llmgw.ReasonProviderAuthFailed,
			wantStatus: codes.PermissionDenied,
		},
		{
			name:       "schema unsupported",
			code:       llmgw.ReasonSchemaUnsupported,
			wantStatus: codes.FailedPrecondition,
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			fixture := newLLMTestFixture(t)
			fixture.enableFallback(t)
			provider := &fakeProvider{behaviors: []fakeBehavior{
				{
					err: &llmgw.ProviderFailure{
						Code:              test.code,
						Retryable:         false,
						ProviderRequestID: "provider-rejected",
					},
				},
				{
					response: llmgw.ProviderResponse{
						Content:      "must not run",
						InputTokens:  1,
						OutputTokens: 1,
					},
				},
			}}
			client := fixture.chatClient(t, provider)

			_, err := client.Chat(
				trustedChatContext(fixture.principal),
				fixture.chatRequest("logical-permanent-"+strings.ReplaceAll(test.name, " ", "-")),
			)
			assertStableError(t, err, test.wantStatus, test.code)
			calls := provider.callsSnapshot()
			if len(calls) != 1 || calls[0].model != fixture.model {
				t.Fatalf("不可重试故障 provider calls = %+v", calls)
			}

			var (
				attemptCount  int
				attemptStatus string
				attemptCode   string
				callStatus    string
				reservedCost  int64
				reservedToken int64
			)
			if err := fixture.pool.QueryRow(context.Background(), `
				SELECT count(*) OVER (),
				       attempt.status,
				       attempt.error_code,
				       call.status,
				       budget.cost_reserved_micro_usd,
				       budget.provider_tokens_reserved
				FROM llm_logical_call call
				JOIN llm_budget_scope budget
				  ON budget.budget_scope_id = call.budget_scope_id
				JOIN llm_provider_attempt attempt
				  ON attempt.logical_call_pk = call.logical_call_pk
				WHERE call.principal_id = $1
				  AND call.session_id = $2
				  AND call.logical_call_id LIKE 'logical-permanent-%'`,
				fixture.principal, fixture.sessionID,
			).Scan(
				&attemptCount, &attemptStatus, &attemptCode, &callStatus,
				&reservedCost, &reservedToken,
			); err != nil {
				t.Fatalf("读取不可重试账本: %v", err)
			}
			if attemptCount != 1 ||
				attemptStatus != "FAILED_NO_CHARGE" ||
				attemptCode != test.code ||
				callStatus != "FAILED" ||
				reservedCost != 0 ||
				reservedToken != 0 {
				t.Fatalf(
					"不可重试账本 = attempts %d status %q/%q call %q reserved %d/%d",
					attemptCount, attemptStatus, attemptCode, callStatus,
					reservedToken, reservedCost,
				)
			}
		})
	}
}

func TestSemanticRepairUsesANewLogicalCall(t *testing.T) {
	fixture := newLLMTestFixture(t)
	provider := &fakeProvider{behaviors: []fakeBehavior{
		{
			err: &llmgw.ProviderFailure{
				Code:      llmgw.ReasonProviderBadRequest,
				Retryable: false,
			},
		},
		{
			response: llmgw.ProviderResponse{
				Content:      "repaired",
				InputTokens:  10,
				OutputTokens: 5,
				FinishReason: "STOP",
			},
		},
	}}
	client := fixture.chatClient(t, provider)

	_, err := client.Chat(
		trustedChatContext(fixture.principal),
		fixture.chatRequest("logical-before-repair"),
	)
	assertStableError(t, err, codes.InvalidArgument, llmgw.ReasonProviderBadRequest)

	repaired, err := client.Chat(
		trustedChatContext(fixture.principal),
		fixture.chatRequest("logical-after-repair"),
	)
	if err != nil {
		t.Fatalf("新 logical_call_id 语义修复失败: %v", err)
	}
	if repaired.GetLogicalCallId() != "logical-after-repair" ||
		repaired.GetContent() != "repaired" ||
		len(provider.callsSnapshot()) != 2 {
		t.Fatalf("语义修复结果 = %+v", repaired)
	}
}

func TestTerminalFailureReplayReturnsSameStableError(t *testing.T) {
	fixture := newLLMTestFixture(t)
	provider := &fakeProvider{behaviors: []fakeBehavior{
		{
			err: &llmgw.ProviderFailure{
				Code:      llmgw.ReasonProviderBadRequest,
				Retryable: false,
			},
		},
		{
			response: llmgw.ProviderResponse{
				Content: "must not run", InputTokens: 1, OutputTokens: 1,
			},
		},
	}}
	client := fixture.chatClient(t, provider)
	req := fixture.chatRequest("logical-terminal-failure")

	_, err := client.Chat(trustedChatContext(fixture.principal), req)
	assertStableError(t, err, codes.InvalidArgument, llmgw.ReasonProviderBadRequest)
	_, replayErr := client.Chat(trustedChatContext(fixture.principal), req)
	assertStableError(t, replayErr, codes.InvalidArgument, llmgw.ReasonProviderBadRequest)
	if calls := provider.callsSnapshot(); len(calls) != 1 {
		t.Fatalf("终态失败重放新增 provider calls = %d, want 1", len(calls))
	}
}

func TestRetryableFailuresStopAtTwoAttemptsAndReleaseReservation(t *testing.T) {
	fixture := newLLMTestFixture(t)
	fixture.enableFallbackChain(t)
	provider := &fakeProvider{behaviors: []fakeBehavior{
		{err: &llmgw.ProviderFailure{Code: llmgw.ReasonProviderRateLimited, Retryable: true}},
		{err: &llmgw.ProviderFailure{Code: llmgw.ReasonProviderRateLimited, Retryable: true}},
		{
			response: llmgw.ProviderResponse{
				Content: "third request must not run", InputTokens: 1, OutputTokens: 1,
			},
		},
	}}
	client := fixture.chatClient(t, provider)

	_, err := client.Chat(
		trustedChatContext(fixture.principal),
		fixture.chatRequest("logical-two-attempt-limit"),
	)
	assertStableError(t, err, codes.ResourceExhausted, llmgw.ReasonProviderRateLimited)
	calls := provider.callsSnapshot()
	if len(calls) != 2 ||
		calls[0].model != fixture.model ||
		calls[1].model != fixture.fallback {
		t.Fatalf("两次 attempt 后 provider calls = %+v", calls)
	}

	var (
		attemptCount  int
		failedCount   int
		callStatus    string
		reservedCost  int64
		reservedToken int64
	)
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT count(*),
		       count(*) FILTER (WHERE attempt.status = 'FAILED_NO_CHARGE'),
		       min(call.status),
		       min(budget.cost_reserved_micro_usd),
		       min(budget.provider_tokens_reserved)
		FROM llm_logical_call call
		JOIN llm_budget_scope budget
		  ON budget.budget_scope_id = call.budget_scope_id
		JOIN llm_provider_attempt attempt
		  ON attempt.logical_call_pk = call.logical_call_pk
		WHERE call.principal_id = $1
		  AND call.session_id = $2
		  AND call.logical_call_id = 'logical-two-attempt-limit'`,
		fixture.principal, fixture.sessionID,
	).Scan(
		&attemptCount, &failedCount, &callStatus,
		&reservedCost, &reservedToken,
	); err != nil {
		t.Fatalf("读取两次 attempt 账本: %v", err)
	}
	if attemptCount != 2 ||
		failedCount != 2 ||
		callStatus != "FAILED" ||
		reservedCost != 0 ||
		reservedToken != 0 {
		t.Fatalf(
			"两次 attempt 账本 = attempts %d failed %d call %q reserved %d/%d",
			attemptCount, failedCount, callStatus, reservedToken, reservedCost,
		)
	}
}

func TestConcurrentSameLogicalCallUsesPostgresArbitration(t *testing.T) {
	fixture := newLLMTestFixture(t)
	entered := make(chan struct{}, 1)
	release := make(chan struct{})
	provider := &fakeProvider{
		response: llmgw.ProviderResponse{
			Content:           "single authoritative result",
			InputTokens:       10,
			OutputTokens:      5,
			ProviderRequestID: "provider-concurrent",
			FinishReason:      "STOP",
		},
		onCall: func(ctx context.Context) {
			entered <- struct{}{}
			select {
			case <-release:
			case <-ctx.Done():
			}
		},
	}
	client := fixture.chatClient(t, provider)
	req := fixture.chatRequest("logical-concurrent-arbitration")

	type chatResult struct {
		response *llmgwv1.ChatResponse
		err      error
	}
	firstDone := make(chan chatResult, 1)
	go func() {
		response, err := client.Chat(trustedChatContext(fixture.principal), req)
		firstDone <- chatResult{response: response, err: err}
	}()
	select {
	case <-entered:
	case <-time.After(5 * time.Second):
		t.Fatal("首个调用未到达 provider")
	}

	_, err := client.Chat(trustedChatContext(fixture.principal), req)
	assertStableError(t, err, codes.Aborted, llmgw.ReasonCallInProgress)
	if calls := provider.callsSnapshot(); len(calls) != 1 {
		t.Fatalf("在途仲裁产生 provider calls = %d, want 1", len(calls))
	}

	close(release)
	var first chatResult
	select {
	case first = <-firstDone:
	case <-time.After(5 * time.Second):
		t.Fatal("首个调用未完成")
	}
	if first.err != nil {
		t.Fatalf("首个调用失败: %v", first.err)
	}

	replayed, err := client.Chat(trustedChatContext(fixture.principal), req)
	if err != nil {
		t.Fatalf("完成态重放: %v", err)
	}
	if len(provider.callsSnapshot()) != 1 ||
		replayed.GetLedgerEntryId() != first.response.GetLedgerEntryId() ||
		len(replayed.GetAttempts()) != 1 ||
		replayed.GetAttempts()[0].GetAttemptId() != first.response.GetAttempts()[0].GetAttemptId() {
		t.Fatalf("完成态重放账本身份不一致")
	}

	var (
		logicalCount int
		attemptCount int
		settledCost  int64
	)
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT count(DISTINCT call.logical_call_pk),
		       count(attempt.attempt_id),
		       min(budget.cost_settled_micro_usd)
		FROM llm_logical_call call
		JOIN llm_budget_scope budget
		  ON budget.budget_scope_id = call.budget_scope_id
		JOIN llm_provider_attempt attempt
		  ON attempt.logical_call_pk = call.logical_call_pk
		WHERE call.principal_id = $1
		  AND call.session_id = $2
		  AND call.logical_call_id = 'logical-concurrent-arbitration'`,
		fixture.principal, fixture.sessionID,
	).Scan(&logicalCount, &attemptCount, &settledCost); err != nil {
		t.Fatalf("读取并发仲裁账本: %v", err)
	}
	if logicalCount != 1 || attemptCount != 1 || settledCost != 20 {
		t.Fatalf(
			"并发仲裁账本 = logical %d attempt %d settled cost %d",
			logicalCount, attemptCount, settledCost,
		)
	}
}

func TestRequestHashCoversGovernanceAndParameters(t *testing.T) {
	fixture := newLLMTestFixture(t)
	provider := &fakeProvider{response: llmgw.ProviderResponse{
		Content: "first", InputTokens: 10, OutputTokens: 5, FinishReason: "STOP",
	}}
	client := fixture.chatClient(t, provider)
	base := fixture.chatRequest("logical-request-hash")
	if _, err := client.Chat(trustedChatContext(fixture.principal), base); err != nil {
		t.Fatalf("首次 Chat: %v", err)
	}

	variants := []struct {
		name   string
		change func(*llmgwv1.ChatRequest)
	}{
		{
			name:   "messages",
			change: func(req *llmgwv1.ChatRequest) { req.Messages[0].Content = "changed" },
		},
		{
			name:   "prompt version",
			change: func(req *llmgwv1.ChatRequest) { req.PromptTemplateVersion = "v2" },
		},
		{
			name:   "schema version",
			change: func(req *llmgwv1.ChatRequest) { req.ResponseSchemaVersion = "v2" },
		},
		{
			name:   "raw schema",
			change: func(req *llmgwv1.ChatRequest) { req.JsonSchema = `{"type":"object"}` },
		},
		{
			name:   "data classification",
			change: func(req *llmgwv1.ChatRequest) { req.DataClassification = "PRINCIPAL_DATA" },
		},
		{
			name:   "temperature",
			change: func(req *llmgwv1.ChatRequest) { req.Temperature = 0.5 },
		},
		{
			name:   "max tokens",
			change: func(req *llmgwv1.ChatRequest) { req.MaxTokens = 700 },
		},
		{
			name:   "timeout",
			change: func(req *llmgwv1.ChatRequest) { req.TimeoutMs = 1000 },
		},
	}
	for _, variant := range variants {
		t.Run(variant.name, func(t *testing.T) {
			req := fixture.chatRequest("logical-request-hash")
			variant.change(req)
			_, err := client.Chat(trustedChatContext(fixture.principal), req)
			assertStableError(t, err, codes.Aborted, llmgw.ReasonIdempotencyConflict)
		})
	}
	if calls := provider.callsSnapshot(); len(calls) != 1 {
		t.Fatalf("request hash 变体产生 provider calls = %d, want 1", len(calls))
	}
}
