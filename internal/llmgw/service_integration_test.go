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
	model     string
	maxTokens int32
	messages  []llmgw.ProviderMessage
}

type fakeProvider struct {
	mu       sync.Mutex
	calls    []providerCall
	response llmgw.ProviderResponse
	err      error
	onCall   func(context.Context)
}

func (p *fakeProvider) Chat(ctx context.Context, req llmgw.ProviderRequest) (llmgw.ProviderResponse, error) {
	p.mu.Lock()
	p.calls = append(p.calls, providerCall{
		model:     req.Model,
		maxTokens: req.MaxOutputTokens,
		messages:  append([]llmgw.ProviderMessage(nil), req.Messages...),
	})
	onCall := p.onCall
	response := p.response
	providerErr := p.err
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
	config.MaxConns = 1
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
	}
	fixture.seed(t)
	return fixture
}

func applyLLMGatewayMigrations(t *testing.T, pool *pgxpool.Pool) {
	t.Helper()
	applyLLMGatewayMigrationsThrough(t, pool, 29)
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
		) VALUES ($1, $2, 1000000, 2000000)`,
		f.priceTable, f.model,
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
