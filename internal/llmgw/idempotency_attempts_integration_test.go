//go:build integration

package llmgw_test

import (
	"context"
	"strings"
	"testing"
	"time"

	"google.golang.org/grpc/codes"

	llmgwv1 "github.com/choucisan/ai-gaokao-counselor/api/llmgw/v1"
	"github.com/choucisan/ai-gaokao-counselor/internal/llmgw"
)

func TestSameLogicalCallWithChangedRequestConflicts(t *testing.T) {
	fixture := newLLMTestFixture(t)
	provider := &fakeProvider{response: llmgw.ProviderResponse{
		Content: "first", InputTokens: 1, OutputTokens: 1, FinishReason: "STOP",
		UsageStatus: llmgw.ProviderUsageTrusted,
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
				UsageStatus:       llmgw.ProviderUsageTrusted,
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
				UsageStatus:  llmgw.ProviderUsageTrusted,
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
			UsageStatus:       llmgw.ProviderUsageTrusted,
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
	if _, err := fixture.pool.Exec(context.Background(), `
		UPDATE llm_role_route
		SET allowed_data_classes = ARRAY['PUBLIC_DATA', 'PRINCIPAL_DATA']::TEXT[]
		WHERE release_id = $1 AND role = 'profile'`,
		fixture.releaseID,
	); err != nil {
		t.Fatalf("允许 request hash 使用的 data classes: %v", err)
	}
	provider := &fakeProvider{response: llmgw.ProviderResponse{
		Content: "first", InputTokens: 10, OutputTokens: 5, FinishReason: "STOP",
		UsageStatus: llmgw.ProviderUsageTrusted,
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
