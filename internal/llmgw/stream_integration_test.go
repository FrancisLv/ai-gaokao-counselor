//go:build integration

package llmgw_test

import (
	"context"
	"fmt"
	"strings"
	"testing"
	"time"

	kerrors "github.com/go-kratos/kratos/v2/errors"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	llmgwv1 "github.com/choucisan/ai-gaokao-counselor/api/llmgw/v1"
	"github.com/choucisan/ai-gaokao-counselor/internal/llmgw"
)

func TestStreamChatEmitsTypedGovernedEventsThroughGeneratedClient(t *testing.T) {
	fixture := newLLMTestFixture(t)
	provider := &fakeProvider{response: llmgw.ProviderResponse{
		Content:           "画像流式解释",
		InputTokens:       1000,
		OutputTokens:      500,
		ProviderRequestID: "provider-stream-1",
		FinishReason:      "STOP",
		UsageStatus:       llmgw.ProviderUsageTrusted,
	}}
	client := fixture.chatClient(t, provider)

	stream, err := client.StreamChat(
		trustedChatContext(fixture.principal),
		fixture.chatRequest("logical-stream-success"),
	)
	if err != nil {
		t.Fatalf("StreamChat: %v", err)
	}
	deltas, err := collectStreamEvents(t, stream)
	if err != nil {
		t.Fatalf("接收流事件: %v", err)
	}

	wantTypes := []llmgwv1.ChatStreamEventType{
		llmgwv1.ChatStreamEventType_CHAT_STREAM_EVENT_START,
		llmgwv1.ChatStreamEventType_CHAT_STREAM_EVENT_DELTA,
		llmgwv1.ChatStreamEventType_CHAT_STREAM_EVENT_USAGE,
		llmgwv1.ChatStreamEventType_CHAT_STREAM_EVENT_DONE,
	}
	if len(deltas) != len(wantTypes) {
		t.Fatalf("流事件数 = %d, want %d: %+v", len(deltas), len(wantTypes), deltas)
	}
	for index, wantType := range wantTypes {
		event := deltas[index].GetEvent()
		if event == nil {
			t.Fatalf("第 %d 个 ChatDelta 缺少 typed event", index+1)
		}
		if event.GetSequence() != int64(index+1) {
			t.Errorf("事件 %d sequence = %d", index+1, event.GetSequence())
		}
		if event.GetType() != wantType {
			t.Errorf("事件 %d type = %s, want %s", index+1, event.GetType(), wantType)
		}
		if event.GetLogicalCallId() != "logical-stream-success" {
			t.Errorf("事件 %d logical_call_id = %q", index+1, event.GetLogicalCallId())
		}
	}

	start := deltas[0].GetEvent()
	if start.GetAttemptId() == "" || start.GetModelId() != fixture.model ||
		start.GetLedgerEntryId() == "" || start.GetRouteVersion() != fixture.route ||
		start.GetPriceTableVersion() != fixture.priceTable {
		t.Fatalf("START 治理身份不完整: %+v", start)
	}
	streamed := deltas[1]
	if streamed.GetContent() != "画像流式解释" ||
		streamed.GetEvent().GetDeltaText() != "画像流式解释" {
		t.Fatalf("DELTA 旧字段或 typed delta = %q/%q",
			streamed.GetContent(), streamed.GetEvent().GetDeltaText())
	}
	usageEvent := deltas[2].GetEvent()
	if usageEvent.GetUsage().GetInputTokens() != 1000 ||
		usageEvent.GetUsage().GetOutputTokens() != 500 ||
		usageEvent.GetCost().GetActualMicroUsd() != 2000 {
		t.Fatalf("USAGE 事件 accounting = %+v", usageEvent)
	}
	done := deltas[3]
	if !done.GetDone() || done.GetEvent().GetContent() != "画像流式解释" ||
		done.GetEvent().GetLedgerEntryId() != start.GetLedgerEntryId() ||
		done.GetEvent().GetFinishReason() != "STOP" ||
		len(done.GetEvent().GetAttempts()) != 1 ||
		done.GetEvent().GetAttempts()[0].GetAttemptId() != start.GetAttemptId() {
		t.Fatalf("DONE 最终结果不完整: %+v", done)
	}
	if calls := provider.callsSnapshot(); len(calls) != 1 ||
		calls[0].attemptID != start.GetAttemptId() {
		t.Fatalf("provider attempt 归属 = %+v", calls)
	}
}

func TestStreamChatRejectsBeforeStartWithoutProviderAttempt(t *testing.T) {
	cases := []struct {
		name       string
		prepare    func(*testing.T, *llmTestFixture)
		mutate     func(*llmgwv1.ChatRequest)
		wantCode   codes.Code
		wantReason string
	}{
		{
			name: "budget",
			prepare: func(t *testing.T, fixture *llmTestFixture) {
				t.Helper()
				if _, err := fixture.pool.Exec(context.Background(), `
					INSERT INTO llm_budget_scope (
						budget_scope_id, principal_id, session_id, token_limit,
						cost_limit_micro_usd, provider_tokens_reserved,
						cost_reserved_micro_usd
					) VALUES (gen_random_uuid(), $1, $2, 1, 1, 1, 1)`,
					fixture.principal, fixture.sessionID,
				); err != nil {
					t.Fatalf("预占预算: %v", err)
				}
			},
			mutate:     func(*llmgwv1.ChatRequest) {},
			wantCode:   codes.ResourceExhausted,
			wantReason: llmgw.ReasonBudgetExceeded,
		},
		{
			name:    "sensitive",
			prepare: func(*testing.T, *llmTestFixture) {},
			mutate: func(req *llmgwv1.ChatRequest) {
				req.Messages[0].Content = "性别为男，请概括"
			},
			wantCode:   codes.FailedPrecondition,
			wantReason: llmgw.ReasonSensitiveDataNotAllowed,
		},
		{
			name:    "raw schema",
			prepare: func(*testing.T, *llmTestFixture) {},
			mutate: func(req *llmgwv1.ChatRequest) {
				req.JsonSchema = `{"type":"object"}`
			},
			wantCode:   codes.InvalidArgument,
			wantReason: llmgw.ReasonSchemaNotAllowed,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			fixture := newLLMTestFixture(t)
			tc.prepare(t, fixture)
			provider := &fakeProvider{response: llmgw.ProviderResponse{
				Content: "unexpected", InputTokens: 1, OutputTokens: 1,
				FinishReason: "STOP",
			}}
			client := fixture.chatClient(t, provider)
			req := fixture.chatRequest("logical-stream-reject-" + tc.name)
			tc.mutate(req)

			stream, err := client.StreamChat(trustedChatContext(fixture.principal), req)
			if err != nil {
				t.Fatalf("StreamChat: %v", err)
			}
			events, streamErr := collectStreamEvents(t, stream)
			assertStableError(t, streamErr, tc.wantCode, tc.wantReason)
			if len(events) != 1 || events[0].GetEvent().GetType() !=
				llmgwv1.ChatStreamEventType_CHAT_STREAM_EVENT_ERROR {
				t.Fatalf("预占前拒绝事件 = %+v, want 单个 ERROR", events)
			}
			if events[0].GetEvent().GetErrorCode() != tc.wantReason {
				t.Fatalf("ERROR error_code = %q, want %q",
					events[0].GetEvent().GetErrorCode(), tc.wantReason)
			}
			if calls := provider.callsSnapshot(); len(calls) != 0 {
				t.Fatalf("预占前拒绝调用 provider %d 次", len(calls))
			}
		})
	}
}

func TestStreamChatDoneCarriesValidatedStructuredResult(t *testing.T) {
	fixture := newLLMTestFixture(t)
	fixture.seedResponseSchema(t, `{
		"type": "object",
		"required": ["summary"],
		"properties": {"summary": {"type": "string"}},
		"additionalProperties": false
	}`)
	provider := &fakeProvider{response: llmgw.ProviderResponse{
		Content:           `{"summary":"结构化流式结果"}`,
		InputTokens:       12,
		OutputTokens:      6,
		ProviderRequestID: "provider-stream-structured",
		FinishReason:      "STOP",
		UsageStatus:       llmgw.ProviderUsageTrusted,
	}}
	client := fixture.chatClient(t, provider)
	req := fixture.chatRequest("logical-stream-structured")
	req.ResponseSchemaKey = "profile_explain_v1"
	req.ResponseSchemaVersion = "v1"

	stream, err := client.StreamChat(trustedChatContext(fixture.principal), req)
	if err != nil {
		t.Fatalf("StreamChat: %v", err)
	}
	events, streamErr := collectStreamEvents(t, stream)
	if streamErr != nil {
		t.Fatalf("接收结构化流: %v", streamErr)
	}
	done := events[len(events)-1].GetEvent()
	if done.GetType() != llmgwv1.ChatStreamEventType_CHAT_STREAM_EVENT_DONE ||
		!done.GetSchemaValidated() ||
		done.GetResponseSchemaKey() != "profile_explain_v1" ||
		done.GetResponseSchemaVersion() != "v1" ||
		done.GetStructuredOutput().AsMap()["summary"] != "结构化流式结果" {
		t.Fatalf("DONE 结构化 schema 状态不完整: %+v", done)
	}
}

func TestStreamChatReplaysCacheHitWithZeroProviderAttempt(t *testing.T) {
	fixture := newLLMTestFixture(t)
	fixture.enableResultCache(t)
	provider := &fakeProvider{response: llmgw.ProviderResponse{
		Content:           "缓存流式回放",
		InputTokens:       10,
		OutputTokens:      5,
		ProviderRequestID: "provider-cache-stream-miss",
		FinishReason:      "STOP",
		UsageStatus:       llmgw.ProviderUsageTrusted,
	}}
	client := fixture.chatClient(t, provider)

	if _, err := client.Chat(
		trustedChatContext(fixture.principal),
		fixture.chatRequest("logical-stream-cache-miss"),
	); err != nil {
		t.Fatalf("首次缓存 miss: %v", err)
	}
	stream, err := client.StreamChat(
		trustedChatContext(fixture.principal),
		fixture.chatRequest("logical-stream-cache-hit"),
	)
	if err != nil {
		t.Fatalf("StreamChat: %v", err)
	}
	events, err := collectStreamEvents(t, stream)
	if err != nil {
		t.Fatalf("接收缓存流: %v", err)
	}

	wantTypes := []llmgwv1.ChatStreamEventType{
		llmgwv1.ChatStreamEventType_CHAT_STREAM_EVENT_DELTA,
		llmgwv1.ChatStreamEventType_CHAT_STREAM_EVENT_DONE,
	}
	if len(events) != len(wantTypes) {
		t.Fatalf("缓存流事件 = %+v, want DELTA/DONE", events)
	}
	for index, wantType := range wantTypes {
		if got := events[index].GetEvent().GetType(); got != wantType {
			t.Fatalf("缓存流事件 %d type = %s, want %s", index+1, got, wantType)
		}
	}
	done := events[1].GetEvent()
	if !done.GetCacheHit() || done.GetContent() != "缓存流式回放" ||
		done.GetUsage().GetInputTokens() != 0 ||
		done.GetUsage().GetOutputTokens() != 0 ||
		done.GetCost().GetActualMicroUsd() != 0 ||
		done.GetCost().GetSettlementStatus() != "CACHE_HIT" ||
		len(done.GetAttempts()) != 0 {
		t.Fatalf("缓存 DONE 未回放零 attempt 类型化结果: %+v", done)
	}
	if calls := provider.callsSnapshot(); len(calls) != 1 {
		t.Fatalf("缓存 miss/hit provider calls = %d, want 1", len(calls))
	}
	var attempts int
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT count(*)
		FROM llm_provider_attempt
		WHERE logical_call_id = 'logical-stream-cache-hit'`,
	).Scan(&attempts); err != nil {
		t.Fatalf("统计缓存命中 attempt: %v", err)
	}
	if attempts != 0 {
		t.Fatalf("缓存命中创建 provider attempt %d 个", attempts)
	}
}

func TestStreamChatFallbackKeepsLogicalCallAndAttributesBothAttempts(t *testing.T) {
	fixture := newLLMTestFixture(t)
	fixture.enableFallback(t)
	provider := &fakeProvider{behaviors: []fakeBehavior{
		{err: &llmgw.ProviderFailure{
			Code: llmgw.ReasonProviderRateLimited, Retryable: true,
		}},
		{response: llmgw.ProviderResponse{
			Content:           "备用模型结果",
			InputTokens:       20,
			OutputTokens:      10,
			ProviderRequestID: "provider-fallback-stream",
			FinishReason:      "STOP",
			UsageStatus:       llmgw.ProviderUsageTrusted,
		}},
	}}
	client := fixture.chatClient(t, provider)
	stream, err := client.StreamChat(
		trustedChatContext(fixture.principal),
		fixture.chatRequest("logical-stream-fallback"),
	)
	if err != nil {
		t.Fatalf("StreamChat: %v", err)
	}
	events, err := collectStreamEvents(t, stream)
	if err != nil {
		t.Fatalf("接收 fallback 流: %v", err)
	}
	if len(events) < 4 {
		t.Fatalf("fallback 流事件不足: %+v", events)
	}
	start := events[0].GetEvent()
	delta := events[1].GetEvent()
	done := events[len(events)-1].GetEvent()
	if start.GetType() != llmgwv1.ChatStreamEventType_CHAT_STREAM_EVENT_START ||
		start.GetModelId() != fixture.model ||
		delta.GetType() != llmgwv1.ChatStreamEventType_CHAT_STREAM_EVENT_DELTA ||
		delta.GetModelId() != fixture.fallback ||
		done.GetType() != llmgwv1.ChatStreamEventType_CHAT_STREAM_EVENT_DONE {
		t.Fatalf("fallback 事件归属 = start %+v delta %+v done %+v", start, delta, done)
	}
	if start.GetLogicalCallId() != done.GetLogicalCallId() ||
		start.GetAttemptId() == done.GetAttemptId() ||
		start.GetLedgerEntryId() != done.GetLedgerEntryId() {
		t.Fatal("fallback 应共享 logical/ledger、使用不同 attempt")
	}
	if len(done.GetAttempts()) != 2 ||
		done.GetAttempts()[0].GetStatus() != "FAILED_NO_CHARGE" ||
		done.GetAttempts()[1].GetStatus() != "SUCCEEDED" ||
		done.GetAttempts()[1].GetModelId() != fixture.fallback {
		t.Fatalf("fallback attempts = %+v", done.GetAttempts())
	}
	calls := provider.callsSnapshot()
	if len(calls) != 2 || calls[0].model != fixture.model ||
		calls[1].model != fixture.fallback ||
		calls[0].attemptID == calls[1].attemptID {
		t.Fatalf("fallback provider calls = %+v", calls)
	}
}

func TestStreamChatSchemaFailureStopsDeltasAndSettlesActualUsage(t *testing.T) {
	fixture := newLLMTestFixture(t)
	fixture.seedResponseSchema(t, `{
		"type": "object",
		"required": ["summary"],
		"properties": {"summary": {"type": "string"}},
		"additionalProperties": false
	}`)
	provider := &fakeProvider{response: llmgw.ProviderResponse{
		Content:           `{"summary":42}`,
		InputTokens:       10,
		OutputTokens:      5,
		ProviderRequestID: "provider-stream-schema-invalid",
		FinishReason:      "STOP",
		UsageStatus:       llmgw.ProviderUsageTrusted,
	}}
	client := fixture.chatClient(t, provider)
	req := fixture.chatRequest("logical-stream-schema-failure")
	req.ResponseSchemaKey = "profile_explain_v1"
	req.ResponseSchemaVersion = "v1"

	stream, err := client.StreamChat(trustedChatContext(fixture.principal), req)
	if err != nil {
		t.Fatalf("StreamChat: %v", err)
	}
	events, streamErr := collectStreamEvents(t, stream)
	assertStableError(
		t, streamErr, codes.FailedPrecondition, llmgw.ReasonSchemaValidationFailed,
	)
	if len(events) != 2 ||
		events[0].GetEvent().GetType() != llmgwv1.ChatStreamEventType_CHAT_STREAM_EVENT_START ||
		events[1].GetEvent().GetType() != llmgwv1.ChatStreamEventType_CHAT_STREAM_EVENT_ERROR {
		t.Fatalf("schema 失败事件 = %+v, want START/ERROR 且无 DELTA", events)
	}
	errorEvent := events[1].GetEvent()
	if errorEvent.GetErrorCode() != llmgw.ReasonSchemaValidationFailed ||
		errorEvent.GetLogicalCallId() != req.GetLogicalCallId() ||
		errorEvent.GetAttemptId() == "" || errorEvent.GetLedgerEntryId() == "" {
		t.Fatalf("schema ERROR 身份不完整: %+v", errorEvent)
	}
	var (
		logicalStatus string
		attemptStatus string
		actualTokens  int64
	)
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT logical.status, attempt.status,
		       COALESCE(logical.actual_input_tokens + logical.actual_output_tokens, 0)
		FROM llm_logical_call logical
		JOIN llm_provider_attempt attempt
		  ON attempt.logical_call_pk = logical.logical_call_pk
		WHERE logical.logical_call_id = $1`,
		req.GetLogicalCallId(),
	).Scan(&logicalStatus, &attemptStatus, &actualTokens); err != nil {
		t.Fatalf("读取 schema 失败账本: %v", err)
	}
	if logicalStatus != "FAILED" || attemptStatus != "SUCCEEDED" || actualTokens != 15 {
		t.Fatalf("schema 失败账本 = logical %q attempt %q tokens %d",
			logicalStatus, attemptStatus, actualTokens)
	}
}

func TestStreamChatProviderErrorSendsStableUnknownChargeEvent(t *testing.T) {
	fixture := newLLMTestFixture(t)
	provider := &fakeProvider{
		err: fmt.Errorf("upstream body must stay private: %w", context.DeadlineExceeded),
	}
	client := fixture.chatClient(t, provider)
	req := fixture.chatRequest("logical-stream-provider-timeout")

	stream, err := client.StreamChat(trustedChatContext(fixture.principal), req)
	if err != nil {
		t.Fatalf("StreamChat: %v", err)
	}
	events, streamErr := collectStreamEvents(t, stream)
	assertStableError(t, streamErr, codes.Unavailable, llmgw.ReasonProviderTimeout)
	if len(events) != 2 ||
		events[0].GetEvent().GetType() != llmgwv1.ChatStreamEventType_CHAT_STREAM_EVENT_START ||
		events[1].GetEvent().GetType() != llmgwv1.ChatStreamEventType_CHAT_STREAM_EVENT_ERROR {
		t.Fatalf("provider 错误事件 = %+v, want START/ERROR 且无 DELTA", events)
	}
	errorEvent := events[1].GetEvent()
	if errorEvent.GetErrorCode() != llmgw.ReasonProviderTimeout ||
		errorEvent.GetErrorMetadata()["logical_call_id"] != req.GetLogicalCallId() ||
		errorEvent.GetErrorMetadata()["attempt_id"] == "" ||
		errorEvent.GetErrorMetadata()["ledger_entry_id"] == "" {
		t.Fatalf("provider ERROR 待对账身份不完整: %+v", errorEvent)
	}
	if strings.Contains(errorEvent.GetErrorMessage(), "upstream body") {
		t.Fatalf("provider ERROR 泄露供应商正文: %+v", errorEvent)
	}
	var (
		logicalStatus string
		attemptStatus string
		reservedCost  int64
	)
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT logical.status, attempt.status, budget.cost_reserved_micro_usd
		FROM llm_logical_call logical
		JOIN llm_provider_attempt attempt
		  ON attempt.logical_call_pk = logical.logical_call_pk
		JOIN llm_budget_scope budget
		  ON budget.budget_scope_id = logical.budget_scope_id
		WHERE logical.logical_call_id = $1`,
		req.GetLogicalCallId(),
	).Scan(&logicalStatus, &attemptStatus, &reservedCost); err != nil {
		t.Fatalf("读取 provider 错误账本: %v", err)
	}
	if logicalStatus != "PARTIAL_UNKNOWN" ||
		attemptStatus != "PENDING_RECONCILIATION" || reservedCost <= 0 {
		t.Fatalf("provider 错误账本 = logical %q attempt %q reserved %d",
			logicalStatus, attemptStatus, reservedCost)
	}
}

func TestStreamChatClientDisconnectPersistsUnknownCharge(t *testing.T) {
	fixture := newLLMTestFixture(t)
	provider := &disconnectProvider{called: make(chan struct{})}
	client := fixture.chatClient(t, provider)
	ctx, cancel := context.WithCancel(trustedChatContext(fixture.principal))
	defer cancel()
	req := fixture.chatRequest("logical-stream-disconnect")

	stream, err := client.StreamChat(ctx, req)
	if err != nil {
		t.Fatalf("StreamChat: %v", err)
	}
	select {
	case <-provider.called:
	case <-time.After(5 * time.Second):
		t.Fatal("provider 未在超时内开始")
	}
	cancel()
	_, _ = stream.Recv()

	deadline := time.Now().Add(5 * time.Second)
	for {
		var (
			logicalStatus string
			attemptStatus string
			attemptReason string
			reservedCost  int64
		)
		err := fixture.pool.QueryRow(context.Background(), `
			SELECT logical.status, attempt.status,
			       COALESCE(attempt.error_code, ''),
			       budget.cost_reserved_micro_usd
			FROM llm_logical_call logical
			JOIN llm_provider_attempt attempt
			  ON attempt.logical_call_pk = logical.logical_call_pk
			JOIN llm_budget_scope budget
			  ON budget.budget_scope_id = logical.budget_scope_id
			WHERE logical.logical_call_id = $1`,
			req.GetLogicalCallId(),
		).Scan(&logicalStatus, &attemptStatus, &attemptReason, &reservedCost)
		if err == nil &&
			logicalStatus == "PARTIAL_UNKNOWN" &&
			attemptStatus == "PENDING_RECONCILIATION" &&
			attemptReason == llmgw.ReasonStreamInterrupted &&
			reservedCost > 0 {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf(
				"等待断流待对账状态超时: err=%v logical=%q attempt=%q reason=%q reserved=%d",
				err, logicalStatus, attemptStatus, attemptReason, reservedCost,
			)
		}
		time.Sleep(50 * time.Millisecond)
	}
}

func TestChatAndStreamChatShareFinalGovernanceSemantics(t *testing.T) {
	t.Run("success", func(t *testing.T) {
		fixture := newLLMTestFixture(t)
		provider := &fakeProvider{response: llmgw.ProviderResponse{
			Content:           "等价最终结果",
			InputTokens:       40,
			OutputTokens:      20,
			ProviderRequestID: "provider-equivalent-success",
			FinishReason:      "STOP",
			UsageStatus:       llmgw.ProviderUsageTrusted,
		}}
		client := fixture.chatClient(t, provider)

		chat, err := client.Chat(
			trustedChatContext(fixture.principal),
			fixture.chatRequest("logical-equivalent-chat"),
		)
		if err != nil {
			t.Fatalf("Chat: %v", err)
		}
		stream, err := client.StreamChat(
			trustedChatContext(fixture.principal),
			fixture.chatRequest("logical-equivalent-stream"),
		)
		if err != nil {
			t.Fatalf("StreamChat: %v", err)
		}
		events, streamErr := collectStreamEvents(t, stream)
		if streamErr != nil {
			t.Fatalf("接收等价流: %v", streamErr)
		}
		done := events[len(events)-1].GetEvent()
		if done.GetType() != llmgwv1.ChatStreamEventType_CHAT_STREAM_EVENT_DONE {
			t.Fatalf("最后事件不是 DONE: %+v", done)
		}

		if done.GetContent() != chat.GetContent() ||
			done.GetModelId() != chat.GetModel() ||
			done.GetFinishReason() != chat.GetFinishReason() ||
			done.GetRouteVersion() != chat.GetRouteVersion() ||
			done.GetPriceTableVersion() != chat.GetPriceTableVersion() ||
			done.GetReleaseId() != chat.GetReleaseId() ||
			done.GetUsage().GetInputTokens() != chat.GetUsage().GetInputTokens() ||
			done.GetUsage().GetOutputTokens() != chat.GetUsage().GetOutputTokens() ||
			done.GetCost().GetActualMicroUsd() != chat.GetCostDetail().GetActualMicroUsd() ||
			done.GetCost().GetSettlementStatus() !=
				chat.GetCostDetail().GetSettlementStatus() {
			t.Fatalf("Chat/DONE 最终治理结果不一致: chat=%+v done=%+v", chat, done)
		}
		if len(done.GetAttempts()) != len(chat.GetAttempts()) {
			t.Fatalf("attempt 数量 = stream %d chat %d",
				len(done.GetAttempts()), len(chat.GetAttempts()))
		}
		for index := range done.GetAttempts() {
			streamAttempt := done.GetAttempts()[index]
			chatAttempt := chat.GetAttempts()[index]
			if streamAttempt.GetStatus() != chatAttempt.GetStatus() ||
				streamAttempt.GetModelId() != chatAttempt.GetModelId() ||
				streamAttempt.GetCostMicroUsd() != chatAttempt.GetCostMicroUsd() {
				t.Fatalf("attempt %d 归属不一致: stream=%+v chat=%+v",
					index, streamAttempt, chatAttempt)
			}
		}
		if calls := provider.callsSnapshot(); len(calls) != 2 {
			t.Fatalf("等价 Chat/Stream provider calls = %d, want 2", len(calls))
		}
	})

	t.Run("schema error", func(t *testing.T) {
		fixture := newLLMTestFixture(t)
		fixture.seedResponseSchema(t, `{
			"type": "object",
			"required": ["summary"],
			"properties": {"summary": {"type": "string"}},
			"additionalProperties": false
		}`)
		provider := &fakeProvider{response: llmgw.ProviderResponse{
			Content:      `{"summary":42}`,
			InputTokens:  10,
			OutputTokens: 5,
			FinishReason: "STOP",
			UsageStatus:  llmgw.ProviderUsageTrusted,
		}}
		client := fixture.chatClient(t, provider)
		chatRequest := fixture.chatRequest("logical-equivalent-schema-chat")
		chatRequest.ResponseSchemaKey = "profile_explain_v1"
		chatRequest.ResponseSchemaVersion = "v1"
		_, chatErr := client.Chat(trustedChatContext(fixture.principal), chatRequest)
		assertStableError(
			t, chatErr, codes.FailedPrecondition, llmgw.ReasonSchemaValidationFailed,
		)

		streamRequest := fixture.chatRequest("logical-equivalent-schema-stream")
		streamRequest.ResponseSchemaKey = "profile_explain_v1"
		streamRequest.ResponseSchemaVersion = "v1"
		stream, err := client.StreamChat(
			trustedChatContext(fixture.principal),
			streamRequest,
		)
		if err != nil {
			t.Fatalf("StreamChat: %v", err)
		}
		events, streamErr := collectStreamEvents(t, stream)
		assertStableError(
			t, streamErr, codes.FailedPrecondition, llmgw.ReasonSchemaValidationFailed,
		)
		errorEvent := events[len(events)-1].GetEvent()
		if errorEvent.GetType() != llmgwv1.ChatStreamEventType_CHAT_STREAM_EVENT_ERROR ||
			errorEvent.GetErrorCode() != kerrors.Reason(chatErr) ||
			errorEvent.GetErrorMessage() != status.Convert(chatErr).Message() {
			t.Fatalf("Chat/Stream 错误语义不一致: chat=%v stream=%+v", chatErr, errorEvent)
		}
	})
}
