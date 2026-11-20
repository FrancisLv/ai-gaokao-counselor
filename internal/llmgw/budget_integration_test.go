//go:build integration

package llmgw_test

import (
	"context"
	"fmt"
	"sync"
	"testing"
	"time"

	kerrors "github.com/go-kratos/kratos/v2/errors"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/choucisan/ai-gaokao-counselor/internal/llmgw"
)

func TestConcurrentChatsEnforceTokenBudgetBeforeProvider(t *testing.T) {
	fixture := newLLMTestFixture(t)
	fixture.seedBudget(t, 5000, 150000)
	provider := &fakeProvider{
		response: llmgw.ProviderResponse{
			Content: "ok", InputTokens: 100, OutputTokens: 50,
			FinishReason: "STOP",
			UsageStatus:  llmgw.ProviderUsageTrusted,
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
			UsageStatus:  llmgw.ProviderUsageTrusted,
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
		UsageStatus: llmgw.ProviderUsageTrusted,
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
		UsageStatus:  llmgw.ProviderUsageTrusted,
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
