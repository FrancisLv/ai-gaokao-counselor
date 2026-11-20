//go:build integration

package llmgw_test

import (
	"context"
	"testing"

	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/protobuf/reflect/protoreflect"

	llmgwv1 "github.com/choucisan/ai-gaokao-counselor/api/llmgw/v1"
	"github.com/choucisan/ai-gaokao-counselor/internal/llmgw"
)

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
		UsageStatus:       llmgw.ProviderUsageTrusted,
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
		{
			name: "ChatDelta",
			want: map[int32]string{
				1: "content", 2: "done", 3: "event",
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
			case "ChatDelta":
				deltaFields := (&llmgwv1.ChatDelta{}).ProtoReflect().Descriptor().Fields()
				for i := 0; i < deltaFields.Len(); i++ {
					field := deltaFields.Get(i)
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

	eventFields := (&llmgwv1.ChatStreamEvent{}).ProtoReflect().Descriptor().Fields()
	wantEventFields := map[int32]string{
		1: "sequence", 2: "type", 3: "logical_call_id", 4: "attempt_id",
		5: "model_id", 6: "delta_text", 7: "finish_reason", 8: "usage",
		9: "cost", 10: "ledger_entry_id", 11: "error_code",
		12: "schema_validated", 13: "structured_output", 14: "content",
		15: "attempts", 16: "route_version", 17: "price_table_version",
		18: "release_id", 19: "response_schema_key",
		20: "response_schema_version", 21: "cache_hit",
		22: "error_message", 23: "error_metadata",
	}
	for number, name := range wantEventFields {
		field := eventFields.ByNumber(protoreflect.FieldNumber(number))
		if field == nil || string(field.Name()) != name {
			t.Errorf("ChatStreamEvent field %d = %v, want %q", number, field, name)
		}
	}
	if eventFields.ByName("event_seq") != nil {
		t.Error("ChatStreamEvent 不应复用 run event_seq")
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
		UsageStatus:  llmgw.ProviderUsageTrusted,
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
