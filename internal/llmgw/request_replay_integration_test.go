//go:build integration

package llmgw_test

import (
	"context"
	"testing"

	"google.golang.org/grpc/codes"

	"github.com/choucisan/ai-gaokao-counselor/internal/llmgw"
)

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
		UsageStatus: llmgw.ProviderUsageTrusted,
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
