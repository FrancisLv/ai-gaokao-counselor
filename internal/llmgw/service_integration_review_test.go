//go:build integration

package llmgw_test

import (
	"context"
	"errors"
	"io"
	"net"
	"testing"
	"time"

	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/test/bufconn"

	llmgwv1 "github.com/choucisan/ai-gaokao-counselor/api/llmgw/v1"
	"github.com/choucisan/ai-gaokao-counselor/internal/llmgw"
)

func TestMissingUsageStatusPreservesReservationAsUnknownCharge(t *testing.T) {
	fixture := newLLMTestFixture(t)
	provider := &fakeProvider{response: llmgw.ProviderResponse{
		Content:      "供应商返回了非零 usage 但未声明可信",
		InputTokens:  100,
		OutputTokens: 50,
		FinishReason: "STOP",
	}}
	client := fixture.chatClient(t, provider)

	_, err := client.Chat(
		trustedChatContext(fixture.principal),
		fixture.chatRequest("logical-missing-usage-status"),
	)
	assertStableError(t, err, codes.Unavailable, llmgw.ReasonReconciliation)

	var logicalStatus, attemptStatus, attemptReason string
	var reservedCost, settledCost int64
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT logical.status,
		       attempt.status,
		       attempt.error_code,
		       budget.cost_reserved_micro_usd,
		       budget.cost_settled_micro_usd
		FROM llm_logical_call logical
		JOIN llm_provider_attempt attempt
		  ON attempt.logical_call_pk = logical.logical_call_pk
		JOIN llm_budget_scope budget
		  ON budget.budget_scope_id = logical.budget_scope_id
		WHERE logical.logical_call_id = 'logical-missing-usage-status'`,
	).Scan(
		&logicalStatus,
		&attemptStatus,
		&attemptReason,
		&reservedCost,
		&settledCost,
	); err != nil {
		t.Fatalf("读取未声明 usage 账本: %v", err)
	}
	if logicalStatus != "PARTIAL_UNKNOWN" ||
		attemptStatus != "PENDING_RECONCILIATION" ||
		attemptReason != llmgw.ReasonReconciliation ||
		reservedCost <= 0 ||
		settledCost != 0 {
		t.Fatalf(
			"未声明 usage 账本 = logical %q attempt %q reason %q reserved %d settled %d",
			logicalStatus,
			attemptStatus,
			attemptReason,
			reservedCost,
			settledCost,
		)
	}
}

func TestStreamStartSendFailureReleasesReservationWithoutProvider(t *testing.T) {
	fixture := newLLMTestFixture(t)
	fixture.enableResultCache(t)
	provider := &fakeProvider{}
	client := fixture.chatClientWithStreamInterceptor(t, provider, failingStreamSendInterceptor)

	stream, err := client.StreamChat(
		trustedChatContext(fixture.principal),
		fixture.chatRequest("logical-stream-start-send-failure"),
	)
	if err == nil {
		_, _ = stream.Recv()
	}
	if calls := provider.callsSnapshot(); len(calls) != 0 {
		t.Fatalf("START 发送失败后仍调用 provider %d 次", len(calls))
	}

	var (
		logicalStatus   string
		logicalReason   string
		attemptStatus   string
		attemptReason   string
		cacheStatus     string
		reservedTokens  int64
		reservedCost    int64
		settledTokens   int64
		settledCost     int64
		projectionCount int
	)
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT logical.status,
		       COALESCE(logical.error_reason, ''),
		       attempt.status,
		       COALESCE(attempt.error_code, ''),
		       cache.status,
		       budget.provider_tokens_reserved,
		       budget.cost_reserved_micro_usd,
		       budget.provider_tokens_settled,
		       budget.cost_settled_micro_usd,
		       (SELECT count(*) FROM llm_call_log
		        WHERE logical_call_id = logical.logical_call_id)
		FROM llm_logical_call logical
		JOIN llm_provider_attempt attempt
		  ON attempt.logical_call_pk = logical.logical_call_pk
		JOIN llm_budget_scope budget
		  ON budget.budget_scope_id = logical.budget_scope_id
		JOIN llm_result_cache cache
		  ON cache.release_id = logical.release_id
		WHERE logical.logical_call_id = 'logical-stream-start-send-failure'`,
	).Scan(
		&logicalStatus,
		&logicalReason,
		&attemptStatus,
		&attemptReason,
		&cacheStatus,
		&reservedTokens,
		&reservedCost,
		&settledTokens,
		&settledCost,
		&projectionCount,
	); err != nil {
		t.Fatalf("读取 START 发送失败账本: %v", err)
	}
	if logicalStatus != "FAILED" ||
		attemptStatus != "FAILED_NO_CHARGE" ||
		attemptReason != "STREAM_START_FAILED" ||
		cacheStatus != "FAILED" ||
		reservedTokens != 0 ||
		reservedCost != 0 ||
		settledTokens != 0 ||
		settledCost != 0 ||
		projectionCount != 1 ||
		logicalReason == "" {
		t.Fatalf(
			"START 发送失败账本 = logical %q/%q attempt %q/%q cache %q reserved %d/%d settled %d/%d projection %d",
			logicalStatus,
			logicalReason,
			attemptStatus,
			attemptReason,
			cacheStatus,
			reservedTokens,
			reservedCost,
			settledTokens,
			settledCost,
			projectionCount,
		)
	}
}

func TestProductionFailClosedProviderIsDefinitelyUnsent(t *testing.T) {
	fixture := newLLMTestFixture(t)
	client := fixture.chatClient(t, nil)

	_, err := client.Chat(
		trustedChatContext(fixture.principal),
		fixture.chatRequest("logical-production-fail-closed"),
	)
	assertStableError(t, err, codes.Unavailable, llmgw.ReasonProviderUnavailable)

	var logicalStatus, attemptStatus string
	var reservedTokens, reservedCost, settledTokens, settledCost int64
	var attempts, projections int
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT logical.status,
		       attempt.status,
		       budget.provider_tokens_reserved,
		       budget.cost_reserved_micro_usd,
		       budget.provider_tokens_settled,
		       budget.cost_settled_micro_usd,
		       (SELECT count(*) FROM llm_provider_attempt
		        WHERE logical_call_id = logical.logical_call_id),
		       (SELECT count(*) FROM llm_call_log
		        WHERE logical_call_id = logical.logical_call_id)
		FROM llm_logical_call logical
		JOIN llm_provider_attempt attempt
		  ON attempt.logical_call_pk = logical.logical_call_pk
		JOIN llm_budget_scope budget
		  ON budget.budget_scope_id = logical.budget_scope_id
		WHERE logical.logical_call_id = 'logical-production-fail-closed'`,
	).Scan(
		&logicalStatus,
		&attemptStatus,
		&reservedTokens,
		&reservedCost,
		&settledTokens,
		&settledCost,
		&attempts,
		&projections,
	); err != nil {
		t.Fatalf("读取生产 fail-closed 账本: %v", err)
	}
	if logicalStatus != "FAILED" ||
		attemptStatus != "FAILED_NO_CHARGE" ||
		reservedTokens != 0 ||
		reservedCost != 0 ||
		settledTokens != 0 ||
		settledCost != 0 ||
		attempts != 1 ||
		projections != 1 {
		t.Fatalf(
			"生产 fail-closed 账本 = logical %q attempt %q reserved %d/%d settled %d/%d attempts %d projections %d",
			logicalStatus,
			attemptStatus,
			reservedTokens,
			reservedCost,
			settledTokens,
			settledCost,
			attempts,
			projections,
		)
	}
}

func TestRoleAllowlistRejectsUnsupportedRolesBeforeCacheReservationOrProvider(t *testing.T) {
	fixture := newLLMTestFixture(t)
	fixture.enableResultCache(t)
	provider := &fakeProvider{response: trustedProviderResponse("unexpected")}
	client := fixture.chatClient(t, provider)

	for _, role := range []string{"strategy", "critic", "", "unknown-role"} {
		req := fixture.chatRequest("logical-role-allowlist-" + uuid.NewString())
		req.Role = role
		_, err := client.Chat(trustedChatContext(fixture.principal), req)
		assertStableError(t, err, codes.InvalidArgument, llmgw.ReasonRoleNotAllowed)

		var counts [4]int
		if err := fixture.pool.QueryRow(context.Background(), `
			SELECT (SELECT count(*) FROM llm_result_cache),
			       (SELECT count(*) FROM llm_logical_call
			        WHERE logical_call_id = $1),
			       (SELECT count(*) FROM llm_provider_attempt),
			       (SELECT count(*) FROM llm_call_log
			        WHERE logical_call_id = $1)`,
			req.GetLogicalCallId(),
		).Scan(&counts[0], &counts[1], &counts[2], &counts[3]); err != nil {
			t.Fatalf("读取角色拒绝副作用: %v", err)
		}
		if counts != [4]int{} {
			t.Fatalf("角色 %q 在 provider 前拒绝仍产生副作用: %+v", role, counts)
		}
	}
	if calls := provider.callsSnapshot(); len(calls) != 0 {
		t.Fatalf("不支持角色调用 provider %d 次", len(calls))
	}
}

func TestRequiredGovernanceFieldsFailClosedBeforeProvider(t *testing.T) {
	fixture := newLLMTestFixture(t)
	provider := &fakeProvider{response: trustedProviderResponse("unexpected")}
	client := fixture.chatClient(t, provider)

	tests := []struct {
		name       string
		mutate     func(*llmgwv1.ChatRequest)
		wantReason string
	}{
		{
			name:       "role",
			mutate:     func(req *llmgwv1.ChatRequest) { req.Role = "" },
			wantReason: llmgw.ReasonRoleNotAllowed,
		},
		{
			name:       "release",
			mutate:     func(req *llmgwv1.ChatRequest) { req.ReleaseId = "" },
			wantReason: llmgw.ReasonRequestInvalid,
		},
		{
			name:       "logical call",
			mutate:     func(req *llmgwv1.ChatRequest) { req.LogicalCallId = "" },
			wantReason: llmgw.ReasonRequestInvalid,
		},
		{
			name:       "session",
			mutate:     func(req *llmgwv1.ChatRequest) { req.SessionId = "not-a-uuid" },
			wantReason: llmgw.ReasonRequestInvalid,
		},
		{
			name:       "run",
			mutate:     func(req *llmgwv1.ChatRequest) { req.RunId = "not-a-uuid" },
			wantReason: llmgw.ReasonRequestInvalid,
		},
		{
			name: "prompt identity",
			mutate: func(req *llmgwv1.ChatRequest) {
				req.PromptTemplateKey = ""
				req.PromptTemplateVersion = ""
			},
			wantReason: llmgw.ReasonPromptNotAllowed,
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			req := fixture.chatRequest("logical-required-" + uuid.NewString())
			test.mutate(req)
			_, err := client.Chat(trustedChatContext(fixture.principal), req)
			assertStableError(t, err, codes.InvalidArgument, test.wantReason)
		})
	}
	if calls := provider.callsSnapshot(); len(calls) != 0 {
		t.Fatalf("缺少必需字段仍调用 provider %d 次", len(calls))
	}
}

func TestMessageRoleAllowlistAndSensitiveRoleScanFailClosed(t *testing.T) {
	fixture := newLLMTestFixture(t)
	provider := &fakeProvider{response: trustedProviderResponse("unexpected")}
	client := fixture.chatClient(t, provider)

	tests := []struct {
		name   string
		role   string
		reason string
	}{
		{name: "tool role", role: "tool", reason: llmgw.ReasonRequestInvalid},
		{name: "invented role", role: "developer", reason: llmgw.ReasonRequestInvalid},
		{name: "direct identifier in role", role: "user-13800138000", reason: llmgw.ReasonSensitiveDataNotAllowed},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			req := fixture.chatRequest("logical-message-role-" + uuid.NewString())
			req.Messages[0].Role = test.role
			_, err := client.Chat(trustedChatContext(fixture.principal), req)
			if test.reason == llmgw.ReasonSensitiveDataNotAllowed {
				assertStableError(
					t,
					err,
					codes.FailedPrecondition,
					llmgw.ReasonSensitiveDataNotAllowed,
				)
			} else {
				assertStableError(t, err, codes.InvalidArgument, test.reason)
			}

			var logicalCount int
			if err := fixture.pool.QueryRow(context.Background(), `
				SELECT count(*) FROM llm_logical_call
				WHERE logical_call_id = $1`,
				req.GetLogicalCallId(),
			).Scan(&logicalCount); err != nil {
				t.Fatalf("读取消息角色拒绝副作用: %v", err)
			}
			if logicalCount != 0 {
				t.Fatalf("消息角色 %q 失败后创建 logical call %d 条", test.role, logicalCount)
			}
		})
	}
	if calls := provider.callsSnapshot(); len(calls) != 0 {
		t.Fatalf("非法消息角色调用 provider %d 次", len(calls))
	}
}

func TestSensitiveScanCoversEscapedRolesAndRenderedMessages(t *testing.T) {
	fixture := newLLMTestFixture(t)
	fixture.seedPromptVersion(t, "v2", "请处理{{candidate_name}}", "")
	provider := &fakeProvider{response: trustedProviderResponse("unexpected")}
	client := fixture.chatClient(t, provider)

	t.Run("escaped role", func(t *testing.T) {
		req := fixture.chatRequest("logical-escaped-role-" + uuid.NewString())
		req.Messages[0].Role = `user\u002d13800138000`
		_, err := client.Chat(trustedChatContext(fixture.principal), req)
		assertStableError(t, err, codes.FailedPrecondition, llmgw.ReasonSensitiveDataNotAllowed)
	})

	t.Run("escaped rendered prompt", func(t *testing.T) {
		req := fixture.chatRequest("logical-escaped-rendered-" + uuid.NewString())
		req.PromptTemplateVersion = "v2"
		req.PromptVariables["candidate_name"] = `\u0031\u0033\u0038\u0030\u0030\u0031\u0033\u0038\u0030\u0030\u0030`
		_, err := client.Chat(trustedChatContext(fixture.principal), req)
		assertStableError(t, err, codes.FailedPrecondition, llmgw.ReasonSensitiveDataNotAllowed)
	})

	if calls := provider.callsSnapshot(); len(calls) != 0 {
		t.Fatalf("JSON 转义后的敏感原文调用 provider %d 次", len(calls))
	}
}

func TestBudgetOverrunPrecedesCacheHitAndCreatesNoLogicalCall(t *testing.T) {
	fixture := newLLMTestFixture(t)
	fixture.enableResultCache(t)
	provider := &fakeProvider{response: trustedProviderResponse("可缓存但随后超限")}
	client := fixture.chatClient(t, provider)

	first, err := client.Chat(
		trustedChatContext(fixture.principal),
		fixture.chatRequest("logical-overrun-cache-first"),
	)
	if err != nil {
		t.Fatalf("首次缓存 miss: %v", err)
	}
	if first.GetCacheHit() {
		t.Fatal("首次调用不应命中缓存")
	}
	if _, err := fixture.pool.Exec(context.Background(), `
		UPDATE llm_budget_scope
		SET budget_status = 'BUDGET_OVERRUN',
		    overrun_at = NOW(),
		    overrun_reason = 'TEST_OVERRUN'
		WHERE principal_id = $1 AND session_id = $2`,
		fixture.principal,
		fixture.sessionID,
	); err != nil {
		t.Fatalf("锁存预算超限: %v", err)
	}

	hitRequest := fixture.chatRequest("logical-overrun-cache-hit")
	_, err = client.Chat(trustedChatContext(fixture.principal), hitRequest)
	assertStableError(t, err, codes.ResourceExhausted, llmgw.ReasonBudgetOverrun)

	var logicalCount int
	var cacheStatus string
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT (SELECT count(*) FROM llm_logical_call
		        WHERE logical_call_id = $1),
		       (SELECT status FROM llm_result_cache LIMIT 1)`,
		hitRequest.GetLogicalCallId(),
	).Scan(&logicalCount, &cacheStatus); err != nil {
		t.Fatalf("读取超限缓存命中结果: %v", err)
	}
	if logicalCount != 0 || cacheStatus != "READY" {
		t.Fatalf("超限缓存命中 = logical %d cache %q", logicalCount, cacheStatus)
	}
	if calls := provider.callsSnapshot(); len(calls) != 1 {
		t.Fatalf("超限缓存命中 provider calls = %d, want 1", len(calls))
	}
}

func trustedProviderResponse(content string) llmgw.ProviderResponse {
	return llmgw.ProviderResponse{
		Content:      content,
		InputTokens:  1,
		OutputTokens: 1,
		FinishReason: "STOP",
		UsageStatus:  llmgw.ProviderUsageTrusted,
	}
}

type failingSendStream struct {
	grpc.ServerStream
}

func (s *failingSendStream) SendMsg(any) error {
	return io.ErrClosedPipe
}

func failingStreamSendInterceptor(
	srv any,
	stream grpc.ServerStream,
	_ *grpc.StreamServerInfo,
	handler grpc.StreamHandler,
) error {
	return handler(srv, &failingSendStream{ServerStream: stream})
}

func (f *llmTestFixture) chatClientWithStreamInterceptor(
	t *testing.T,
	provider llmgw.Provider,
	streamInterceptor grpc.StreamServerInterceptor,
) llmgwv1.LLMGatewayClient {
	t.Helper()
	svc := llmgw.NewService(f.pool, provider)
	listener := bufconn.Listen(1024 * 1024)
	server := grpc.NewServer(grpc.StreamInterceptor(streamInterceptor))
	llmgwv1.RegisterLLMGatewayServer(server, svc)
	done := make(chan error, 1)
	go func() {
		done <- server.Serve(listener)
	}()
	t.Cleanup(func() {
		server.Stop()
		_ = listener.Close()
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
		"passthrough:///llmgw-review-bufnet",
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
			return listener.DialContext(ctx)
		}),
	)
	if err != nil {
		t.Fatalf("创建 llmgw 客户端: %v", err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	return llmgwv1.NewLLMGatewayClient(conn)
}
