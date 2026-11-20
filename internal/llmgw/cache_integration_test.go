//go:build integration

package llmgw_test

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"google.golang.org/grpc/codes"

	llmgwv1 "github.com/choucisan/ai-gaokao-counselor/api/llmgw/v1"
	"github.com/choucisan/ai-gaokao-counselor/internal/llmgw"
)

func TestChatReturnsReleaseBoundCacheHitWithoutProviderAttempt(t *testing.T) {
	fixture := newLLMTestFixture(t)
	fixture.enableResultCache(t)
	provider := &fakeProvider{response: llmgw.ProviderResponse{
		Content:           "可缓存结果",
		InputTokens:       10,
		OutputTokens:      5,
		ProviderRequestID: "provider-cache-miss",
		FinishReason:      "STOP",
		UsageStatus:       llmgw.ProviderUsageTrusted,
	}}
	client := fixture.chatClient(t, provider)

	first, err := client.Chat(
		trustedChatContext(fixture.principal),
		fixture.chatRequest("logical-cache-miss"),
	)
	if err != nil {
		t.Fatalf("首次缓存 miss Chat: %v", err)
	}
	if first.GetCacheHit() {
		t.Fatal("首次调用不应标记为缓存命中")
	}

	second, err := client.Chat(
		trustedChatContext(fixture.principal),
		fixture.chatRequest("logical-cache-hit"),
	)
	if err != nil {
		t.Fatalf("第二次缓存 hit Chat: %v", err)
	}
	if !second.GetCacheHit() {
		t.Fatal("第二次调用应命中共享结果缓存")
	}
	if second.GetContent() != "可缓存结果" ||
		second.GetModel() != fixture.model ||
		second.GetRouteVersion() != fixture.route ||
		second.GetReleaseId() != fixture.releaseID {
		t.Fatalf("缓存命中结果或治理身份不完整: %+v", second)
	}
	if len(second.GetAttempts()) != 0 ||
		second.GetUsage().GetInputTokens() != 0 ||
		second.GetUsage().GetOutputTokens() != 0 ||
		second.GetCostDetail().GetActualMicroUsd() != 0 ||
		second.GetCostDetail().GetReservedMicroUsd() != 0 ||
		second.GetCostDetail().GetSettlementStatus() != "CACHE_HIT" {
		t.Fatalf("缓存命中不应产生 provider attempt/token/cost: %+v", second)
	}
	if calls := provider.callsSnapshot(); len(calls) != 1 {
		t.Fatalf("缓存 miss/hit provider calls = %d, want 1", len(calls))
	}

	var (
		logicalCount       int
		attemptCount       int
		missFlag           bool
		hitFlag            bool
		cacheHits          int
		settledToken       int64
		settledCost        int64
		cacheStatus        string
		cacheRelease       string
		cacheKeyHash       string
		cacheSchemaVersion string
	)
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT count(*),
		       count(*) FILTER (WHERE cache_hit),
		       (SELECT count(*) FROM llm_provider_attempt),
		       bool_or(cache_hit = FALSE),
		       bool_or(cache_hit = TRUE)
		FROM llm_logical_call
		WHERE principal_id = $1 AND session_id = $2`,
		fixture.principal, fixture.sessionID,
	).Scan(&logicalCount, &cacheHits, &attemptCount, &missFlag, &hitFlag); err != nil {
		t.Fatalf("读取 logical cache 账本: %v", err)
	}
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT provider_tokens_settled, cost_settled_micro_usd
		FROM llm_budget_scope
		WHERE principal_id = $1 AND session_id = $2`,
		fixture.principal, fixture.sessionID,
	).Scan(&settledToken, &settledCost); err != nil {
		t.Fatalf("读取 cache 预算结算: %v", err)
	}
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT status, release_id, cache_key_hash, cache_schema_version
		FROM llm_result_cache`,
	).Scan(&cacheStatus, &cacheRelease, &cacheKeyHash, &cacheSchemaVersion); err != nil {
		t.Fatalf("读取结果缓存身份: %v", err)
	}
	if logicalCount != 2 || cacheHits != 1 || attemptCount != 1 ||
		!missFlag || !hitFlag ||
		settledToken != 15 || settledCost != 20 ||
		cacheStatus != "READY" ||
		cacheRelease != fixture.releaseID ||
		len(cacheKeyHash) != 64 ||
		cacheSchemaVersion != "llm-result-cache-v1" {
		t.Fatalf(
			"cache 账本 = logical %d hits %d attempts %d flags %t/%t settled %d/%d cache %q/%q/%q/%q",
			logicalCount, cacheHits, attemptCount, missFlag, hitFlag,
			settledToken, settledCost, cacheStatus, cacheRelease, cacheKeyHash,
			cacheSchemaVersion,
		)
	}
}

func TestChatCachesOnlyExplicitPublicZeroTemperatureResults(t *testing.T) {
	tests := []struct {
		name         string
		configure    func(*testing.T, *llmTestFixture, *llmgwv1.ChatRequest)
		wantCalls    int
		wantCacheRow int
	}{
		{
			name: "route disabled",
			configure: func(_ *testing.T, _ *llmTestFixture, _ *llmgwv1.ChatRequest) {
			},
			wantCalls:    2,
			wantCacheRow: 0,
		},
		{
			name: "nonzero temperature",
			configure: func(t *testing.T, fixture *llmTestFixture, req *llmgwv1.ChatRequest) {
				fixture.enableResultCache(t)
				req.Temperature = 0.25
			},
			wantCalls:    2,
			wantCacheRow: 0,
		},
		{
			name: "principal data",
			configure: func(t *testing.T, fixture *llmTestFixture, req *llmgwv1.ChatRequest) {
				fixture.enableResultCache(t)
				if _, err := fixture.pool.Exec(context.Background(), `
					UPDATE llm_role_route
					SET allowed_data_classes = ARRAY['PUBLIC_DATA', 'PRINCIPAL_DATA']::TEXT[]
					WHERE release_id = $1 AND role = 'profile'`,
					fixture.releaseID,
				); err != nil {
					t.Fatalf("允许 PRINCIPAL_DATA: %v", err)
				}
				req.DataClassification = "PRINCIPAL_DATA"
			},
			wantCalls:    2,
			wantCacheRow: 0,
		},
		{
			name: "public zero temperature",
			configure: func(t *testing.T, fixture *llmTestFixture, _ *llmgwv1.ChatRequest) {
				fixture.enableResultCache(t)
			},
			wantCalls:    1,
			wantCacheRow: 1,
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			fixture := newLLMTestFixture(t)
			provider := &fakeProvider{response: llmgw.ProviderResponse{
				Content: "缓存准入结果", InputTokens: 10, OutputTokens: 5,
				FinishReason: "STOP",
				UsageStatus:  llmgw.ProviderUsageTrusted,
			}}
			client := fixture.chatClient(t, provider)
			first := fixture.chatRequest("logical-cache-eligibility-first")
			test.configure(t, fixture, first)
			if _, err := client.Chat(trustedChatContext(fixture.principal), first); err != nil {
				t.Fatalf("首次 Chat: %v", err)
			}

			second := fixture.chatRequest("logical-cache-eligibility-second")
			test.configure(t, fixture, second)
			if _, err := client.Chat(trustedChatContext(fixture.principal), second); err != nil {
				t.Fatalf("第二次 Chat: %v", err)
			}

			if calls := provider.callsSnapshot(); len(calls) != test.wantCalls {
				t.Fatalf("provider calls = %d, want %d", len(calls), test.wantCalls)
			}
			var cacheRows int
			if err := fixture.pool.QueryRow(context.Background(),
				`SELECT count(*) FROM llm_result_cache`,
			).Scan(&cacheRows); err != nil {
				t.Fatalf("统计缓存行: %v", err)
			}
			if cacheRows != test.wantCacheRow {
				t.Fatalf("cache rows = %d, want %d", cacheRows, test.wantCacheRow)
			}
		})
	}
}

func TestResultCacheKeyCoversGovernanceAndRequestDimensions(t *testing.T) {
	tests := []struct {
		name       string
		first      func(*testing.T, *llmTestFixture, *llmgwv1.ChatRequest)
		afterFirst func(*testing.T, *llmTestFixture, *llmgwv1.ChatRequest)
	}{
		{
			name: "normalized messages",
			afterFirst: func(_ *testing.T, _ *llmTestFixture, req *llmgwv1.ChatRequest) {
				req.Messages[0].Content = "变更后的已脱敏画像"
			},
		},
		{
			name: "prompt version",
			afterFirst: func(t *testing.T, fixture *llmTestFixture, req *llmgwv1.ChatRequest) {
				fixture.seedPromptVersion(t, "v2", "你是缓存 v2 系统模板。", "解释{{topic}}。")
				req.PromptTemplateVersion = "v2"
			},
		},
		{
			name: "response schema version",
			first: func(t *testing.T, fixture *llmTestFixture, req *llmgwv1.ChatRequest) {
				fixture.seedSchemaVersion(t, "v1", `{"type":"object"}`)
				fixture.seedSchemaVersion(t, "v2", `{"type":"object","additionalProperties":true}`)
				req.ResponseSchemaKey = "profile_explain_v1"
				req.ResponseSchemaVersion = "v1"
			},
			afterFirst: func(_ *testing.T, _ *llmTestFixture, req *llmgwv1.ChatRequest) {
				req.ResponseSchemaKey = "profile_explain_v1"
				req.ResponseSchemaVersion = "v2"
			},
		},
		{
			name: "route version",
			afterFirst: func(t *testing.T, fixture *llmTestFixture, _ *llmgwv1.ChatRequest) {
				if _, err := fixture.pool.Exec(context.Background(), `
					UPDATE llm_role_route
					SET route_version = 'route_profile_v2'
					WHERE release_id = $1 AND role = 'profile'`,
					fixture.releaseID,
				); err != nil {
					t.Fatalf("更新 route version: %v", err)
				}
			},
		},
		{
			name: "primary model",
			afterFirst: func(t *testing.T, fixture *llmTestFixture, _ *llmgwv1.ChatRequest) {
				fixture.enableFallback(t)
				if _, err := fixture.pool.Exec(context.Background(), `
					UPDATE llm_role_route
					SET primary_model_id = $1, fallback_model_ids = ARRAY[]::TEXT[]
					WHERE release_id = $2 AND role = 'profile'`,
					fixture.fallback, fixture.releaseID,
				); err != nil {
					t.Fatalf("切换 primary model: %v", err)
				}
			},
		},
		{
			name: "fallback model route",
			afterFirst: func(t *testing.T, fixture *llmTestFixture, _ *llmgwv1.ChatRequest) {
				fixture.enableFallback(t)
			},
		},
		{
			name: "temperature",
			afterFirst: func(_ *testing.T, _ *llmTestFixture, req *llmgwv1.ChatRequest) {
				req.Temperature = 0.25
			},
		},
		{
			name: "max output",
			afterFirst: func(_ *testing.T, _ *llmTestFixture, req *llmgwv1.ChatRequest) {
				req.MaxTokens = 700
			},
		},
		{
			name: "data policy and class",
			afterFirst: func(t *testing.T, fixture *llmTestFixture, req *llmgwv1.ChatRequest) {
				if _, err := fixture.pool.Exec(context.Background(), `
					UPDATE llm_role_route
					SET allowed_data_classes = ARRAY['PUBLIC_DATA', 'PRINCIPAL_DATA']::TEXT[]
					WHERE release_id = $1 AND role = 'profile'`,
					fixture.releaseID,
				); err != nil {
					t.Fatalf("扩展 route 数据策略: %v", err)
				}
				req.DataClassification = "PRINCIPAL_DATA"
			},
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			fixture := newLLMTestFixture(t)
			fixture.enableResultCache(t)
			provider := &fakeProvider{response: llmgw.ProviderResponse{
				Content: `{"summary":"缓存键维度"}`, InputTokens: 10, OutputTokens: 5,
				FinishReason: "STOP",
				UsageStatus:  llmgw.ProviderUsageTrusted,
			}}
			client := fixture.chatClient(t, provider)
			first := fixture.chatRequest("logical-cache-key-first")
			if test.first != nil {
				test.first(t, fixture, first)
			}
			if _, err := client.Chat(trustedChatContext(fixture.principal), first); err != nil {
				t.Fatalf("首次 Chat: %v", err)
			}

			second := fixture.chatRequest("logical-cache-key-second")
			test.afterFirst(t, fixture, second)
			response, err := client.Chat(trustedChatContext(fixture.principal), second)
			if err != nil {
				t.Fatalf("变更缓存键维度后 Chat: %v", err)
			}
			if response.GetCacheHit() {
				t.Fatalf("%s 变化后错误命中旧缓存", test.name)
			}
			if calls := provider.callsSnapshot(); len(calls) != 2 {
				t.Fatalf("%s 变化后 provider calls = %d, want 2", test.name, len(calls))
			}
		})
	}
}

func TestResultCacheKeyIsBoundToRelease(t *testing.T) {
	fixture := newLLMTestFixture(t)
	fixture.enableResultCache(t)
	provider := &fakeProvider{response: llmgw.ProviderResponse{
		Content: "release bound", InputTokens: 10, OutputTokens: 5,
		FinishReason: "STOP",
		UsageStatus:  llmgw.ProviderUsageTrusted,
	}}
	client := fixture.chatClient(t, provider)
	if _, err := client.Chat(
		trustedChatContext(fixture.principal),
		fixture.chatRequest("logical-cache-release-first"),
	); err != nil {
		t.Fatalf("首个 release Chat: %v", err)
	}

	nextRelease := "rel_m5a_38_next"
	fixture.moveRunToClonedRelease(t, nextRelease)
	req := fixture.chatRequest("logical-cache-release-second")
	req.ReleaseId = nextRelease
	response, err := client.Chat(trustedChatContext(fixture.principal), req)
	if err != nil {
		t.Fatalf("新 release Chat: %v", err)
	}
	if response.GetCacheHit() || response.GetReleaseId() != nextRelease {
		t.Fatalf("新 release 错误复用旧缓存: %+v", response)
	}
	if calls := provider.callsSnapshot(); len(calls) != 2 {
		t.Fatalf("release 变化 provider calls = %d, want 2", len(calls))
	}
	var releases int
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT count(DISTINCT release_id)
		FROM llm_result_cache`,
	).Scan(&releases); err != nil {
		t.Fatalf("统计 release-bound 缓存: %v", err)
	}
	if releases != 2 {
		t.Fatalf("cache releases = %d, want 2", releases)
	}
}

func TestCacheHitIsDistinctFromIdempotentReplay(t *testing.T) {
	fixture := newLLMTestFixture(t)
	fixture.enableResultCache(t)
	provider := &fakeProvider{response: llmgw.ProviderResponse{
		Content: "同一最终结果", InputTokens: 12, OutputTokens: 6,
		ProviderRequestID: "provider-cache-distinction",
		FinishReason:      "STOP",
		UsageStatus:       llmgw.ProviderUsageTrusted,
	}}
	client := fixture.chatClient(t, provider)
	req := fixture.chatRequest("logical-cache-distinction-miss")

	miss, err := client.Chat(trustedChatContext(fixture.principal), req)
	if err != nil {
		t.Fatalf("缓存 miss: %v", err)
	}
	replay, err := client.Chat(trustedChatContext(fixture.principal), req)
	if err != nil {
		t.Fatalf("幂等重放: %v", err)
	}
	hit, err := client.Chat(
		trustedChatContext(fixture.principal),
		fixture.chatRequest("logical-cache-distinction-hit"),
	)
	if err != nil {
		t.Fatalf("独立 logical call 缓存命中: %v", err)
	}
	hitReplay, err := client.Chat(
		trustedChatContext(fixture.principal),
		fixture.chatRequest("logical-cache-distinction-hit"),
	)
	if err != nil {
		t.Fatalf("缓存命中 logical call 重放: %v", err)
	}

	if miss.GetCacheHit() || replay.GetCacheHit() || !hit.GetCacheHit() {
		t.Fatalf("cache flags = miss %t replay %t hit %t",
			miss.GetCacheHit(), replay.GetCacheHit(), hit.GetCacheHit())
	}
	if replay.GetLedgerEntryId() != miss.GetLedgerEntryId() ||
		len(replay.GetAttempts()) != 1 ||
		replay.GetAttempts()[0].GetAttemptId() != miss.GetAttempts()[0].GetAttemptId() ||
		replay.GetUsage().GetInputTokens() != 12 ||
		replay.GetUsage().GetOutputTokens() != 6 ||
		replay.GetCostDetail().GetActualMicroUsd() != 24 {
		t.Fatalf("幂等重放未返回原始账本与 usage/cost: %+v", replay)
	}
	if hit.GetLedgerEntryId() == miss.GetLedgerEntryId() ||
		len(hit.GetAttempts()) != 0 ||
		hit.GetUsage().GetInputTokens() != 0 ||
		hit.GetUsage().GetOutputTokens() != 0 ||
		hit.GetCostDetail().GetActualMicroUsd() != 0 ||
		hit.GetCostDetail().GetSettlementStatus() != "CACHE_HIT" {
		t.Fatalf("缓存命中未形成独立零费用 logical call: %+v", hit)
	}
	if !hitReplay.GetCacheHit() ||
		hitReplay.GetLedgerEntryId() != hit.GetLedgerEntryId() ||
		len(hitReplay.GetAttempts()) != 0 ||
		hitReplay.GetUsage().GetInputTokens() != 0 ||
		hitReplay.GetCostDetail().GetActualMicroUsd() != 0 {
		t.Fatalf("缓存命中幂等重放未返回原零费用 logical call: %+v", hitReplay)
	}
	if calls := provider.callsSnapshot(); len(calls) != 1 {
		t.Fatalf("miss/replay/hit provider calls = %d, want 1", len(calls))
	}

	var (
		logicalCount  int
		cacheHitCount int
		attemptCount  int
		hitLatency    int64
		hitProjection bool
	)
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT count(*),
		       count(*) FILTER (WHERE cache_hit),
		       (SELECT count(*) FROM llm_provider_attempt)
		FROM llm_logical_call
		WHERE principal_id = $1 AND session_id = $2`,
		fixture.principal, fixture.sessionID,
	).Scan(&logicalCount, &cacheHitCount, &attemptCount); err != nil {
		t.Fatalf("读取 cache hit 与 replay logical call: %v", err)
	}
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT latency_ms, cache_hit
		FROM llm_call_log
		WHERE logical_call_id = 'logical-cache-distinction-hit'`,
	).Scan(&hitLatency, &hitProjection); err != nil {
		t.Fatalf("读取缓存命中本地延迟投影: %v", err)
	}
	if logicalCount != 2 || cacheHitCount != 1 || attemptCount != 1 ||
		hitLatency < 0 || !hitProjection {
		t.Fatalf(
			"cache/replay 账本 = logical %d hit %d attempts %d latency %d projection %t",
			logicalCount, cacheHitCount, attemptCount, hitLatency, hitProjection,
		)
	}
}

func TestConcurrentCacheMissInvokesProviderOnce(t *testing.T) {
	fixture := newLLMTestFixture(t)
	fixture.enableResultCache(t)
	entered := make(chan struct{}, 1)
	releaseProvider := make(chan struct{})
	provider := &fakeProvider{
		response: llmgw.ProviderResponse{
			Content: "并发缓存唯一结果", InputTokens: 10, OutputTokens: 5,
			ProviderRequestID: "provider-cache-concurrent",
			FinishReason:      "STOP",
			UsageStatus:       llmgw.ProviderUsageTrusted,
		},
		onCall: func(ctx context.Context) {
			entered <- struct{}{}
			select {
			case <-releaseProvider:
			case <-ctx.Done():
			}
		},
	}
	client := fixture.chatClient(t, provider)
	firstRequest := fixture.chatRequest("logical-cache-concurrent-owner")
	firstDone := make(chan error, 1)
	go func() {
		_, err := client.Chat(trustedChatContext(fixture.principal), firstRequest)
		firstDone <- err
	}()
	select {
	case <-entered:
	case <-time.After(5 * time.Second):
		t.Fatal("缓存 claim 持有者未到达 provider")
	}

	_, err := client.Chat(
		trustedChatContext(fixture.principal),
		fixture.chatRequest("logical-cache-concurrent-waiter"),
	)
	assertStableError(t, err, codes.Aborted, llmgw.ReasonCallInProgress)
	if calls := provider.callsSnapshot(); len(calls) != 1 {
		t.Fatalf("并发 miss provider calls = %d, want 1", len(calls))
	}
	close(releaseProvider)
	select {
	case err := <-firstDone:
		if err != nil {
			t.Fatalf("缓存 claim 持有者 Chat: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("缓存 claim 持有者未完成")
	}

	hit, err := client.Chat(
		trustedChatContext(fixture.principal),
		fixture.chatRequest("logical-cache-concurrent-hit"),
	)
	if err != nil {
		t.Fatalf("并发 miss 完成后缓存 hit: %v", err)
	}
	if !hit.GetCacheHit() || len(hit.GetAttempts()) != 0 {
		t.Fatalf("并发 miss 完成后未命中缓存: %+v", hit)
	}
	if calls := provider.callsSnapshot(); len(calls) != 1 {
		t.Fatalf("并发 miss/hit provider calls = %d, want 1", len(calls))
	}
	var (
		logicalCount int
		attemptCount int
		waiterCalls  int
		cacheStatus  string
		claimLeaseMS int64
	)
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT (SELECT count(*) FROM llm_logical_call),
		       (SELECT count(*) FROM llm_provider_attempt),
		       (SELECT count(*) FROM llm_logical_call
		        WHERE logical_call_id = 'logical-cache-concurrent-waiter'),
		       (SELECT status FROM llm_result_cache),
		       (SELECT EXTRACT(EPOCH FROM claim_expires_at - claim_started_at) * 1000
		        FROM llm_result_cache)`,
	).Scan(&logicalCount, &attemptCount, &waiterCalls, &cacheStatus, &claimLeaseMS); err != nil {
		t.Fatalf("读取并发 cache 账本: %v", err)
	}
	if logicalCount != 2 || attemptCount != 1 || waiterCalls != 0 ||
		cacheStatus != "READY" || claimLeaseMS < 34_000 {
		t.Fatalf(
			"并发 cache 账本 = logical %d attempts %d waiter %d status %q lease_ms %d",
			logicalCount, attemptCount, waiterCalls, cacheStatus, claimLeaseMS,
		)
	}
}

func TestStructuredResultCacheReplaysValidatedCanonicalOutput(t *testing.T) {
	fixture := newLLMTestFixture(t)
	fixture.enableResultCache(t)
	fixture.seedResponseSchema(t, `{
		"type": "object",
		"required": ["summary"],
		"properties": {"summary": {"type": "string"}},
		"additionalProperties": false
	}`)
	provider := &fakeProvider{response: llmgw.ProviderResponse{
		Content:      `{"summary":"缓存的结构化结果"}`,
		InputTokens:  10,
		OutputTokens: 5,
		FinishReason: "STOP",
		UsageStatus:  llmgw.ProviderUsageTrusted,
	}}
	client := fixture.chatClient(t, provider)
	first := fixture.chatRequest("logical-structured-cache-first")
	first.ResponseSchemaKey = "profile_explain_v1"
	first.ResponseSchemaVersion = "v1"
	if _, err := client.Chat(trustedChatContext(fixture.principal), first); err != nil {
		t.Fatalf("结构化缓存 miss: %v", err)
	}

	second := fixture.chatRequest("logical-structured-cache-hit")
	second.ResponseSchemaKey = "profile_explain_v1"
	second.ResponseSchemaVersion = "v1"
	hit, err := client.Chat(trustedChatContext(fixture.principal), second)
	if err != nil {
		t.Fatalf("结构化缓存 hit: %v", err)
	}
	if !hit.GetCacheHit() ||
		hit.GetStructuredOutput().GetFields()["summary"].GetStringValue() != "缓存的结构化结果" ||
		hit.GetResponseSchemaKey() != "profile_explain_v1" ||
		hit.GetResponseSchemaVersion() != "v1" ||
		hit.GetPromptTemplateKey() != "profile_explain" ||
		hit.GetPromptTemplateVersion() != "v1" ||
		hit.GetUsage().GetInputTokens() != 0 ||
		hit.GetUsage().GetOutputTokens() != 0 ||
		len(hit.GetAttempts()) != 0 {
		t.Fatalf("结构化缓存命中结果不完整: %+v", hit)
	}
	if calls := provider.callsSnapshot(); len(calls) != 1 {
		t.Fatalf("结构化缓存 provider calls = %d, want 1", len(calls))
	}

	var (
		manifestText       string
		payloadText        string
		hasStructuredValue bool
	)
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT request_key_manifest::text, result_payload::text,
		       result_payload ? 'structured_output'
		FROM llm_result_cache`,
	).Scan(&manifestText, &payloadText, &hasStructuredValue); err != nil {
		t.Fatalf("读取结构化缓存 payload 与 key manifest: %v", err)
	}
	if strings.Contains(manifestText, "请概括这份已脱敏画像") ||
		strings.Contains(manifestText, "湖南考生") ||
		!strings.Contains(manifestText, `"messages_sha256"`) {
		t.Fatalf("缓存 key manifest 泄漏 prompt 原文或缺少 messages hash: %s", manifestText)
	}
	if len(payloadText) == 0 || !hasStructuredValue {
		t.Fatalf("缓存未保存结构化结果: %s", payloadText)
	}
}

func TestRevokedReleaseCannotReadExistingCache(t *testing.T) {
	fixture := newLLMTestFixture(t)
	fixture.enableResultCache(t)
	provider := &fakeProvider{response: llmgw.ProviderResponse{
		Content: "撤销前结果", InputTokens: 10, OutputTokens: 5,
		FinishReason: "STOP",
		UsageStatus:  llmgw.ProviderUsageTrusted,
	}}
	client := fixture.chatClient(t, provider)
	if _, err := client.Chat(
		trustedChatContext(fixture.principal),
		fixture.chatRequest("logical-cache-before-revoke"),
	); err != nil {
		t.Fatalf("撤销前 Chat: %v", err)
	}
	if _, err := fixture.pool.Exec(context.Background(), `
		UPDATE release
		SET status = 'REVOKED', revoked_at = NOW()
		WHERE release_id = $1`,
		fixture.releaseID,
	); err != nil {
		t.Fatalf("撤销 release: %v", err)
	}

	_, err := client.Chat(
		trustedChatContext(fixture.principal),
		fixture.chatRequest("logical-cache-after-revoke"),
	)
	assertStableError(t, err, codes.FailedPrecondition, llmgw.ReasonReleaseRevoked)
	if calls := provider.callsSnapshot(); len(calls) != 1 {
		t.Fatalf("撤销 release 后 provider calls = %d, want 1", len(calls))
	}
	var (
		cacheStatus string
		cacheReason string
	)
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT status, invalidation_reason
		FROM llm_result_cache
		WHERE release_id = $1`,
		fixture.releaseID,
	).Scan(&cacheStatus, &cacheReason); err != nil {
		t.Fatalf("读取撤销 release 的缓存行: %v", err)
	}
	if cacheStatus != "INVALIDATED" || cacheReason != "RELEASE_REVOKED" {
		t.Fatalf("release 撤销后缓存状态 = %q/%q, want INVALIDATED/RELEASE_REVOKED",
			cacheStatus, cacheReason)
	}
}

func TestResultCacheCleanupDoesNotMutateAuthoritativeLedger(t *testing.T) {
	fixture := newLLMTestFixture(t)
	fixture.enableResultCache(t)
	provider := &fakeProvider{response: llmgw.ProviderResponse{
		Content: "可清理缓存", InputTokens: 10, OutputTokens: 5,
		FinishReason: "STOP",
		UsageStatus:  llmgw.ProviderUsageTrusted,
	}}
	client := fixture.chatClient(t, provider)
	if _, err := client.Chat(
		trustedChatContext(fixture.principal),
		fixture.chatRequest("logical-cache-cleanup"),
	); err != nil {
		t.Fatalf("缓存清理前 Chat: %v", err)
	}

	var (
		ledgerID      string
		attemptID     string
		settledToken  int64
		settledCost   int64
		reservedToken int64
		reservedCost  int64
	)
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT call.ledger_entry_id::text,
		       attempt.attempt_id::text,
		       budget.provider_tokens_settled,
		       budget.cost_settled_micro_usd,
		       budget.provider_tokens_reserved,
		       budget.cost_reserved_micro_usd
		FROM llm_logical_call call
		JOIN llm_provider_attempt attempt
		  ON attempt.logical_call_pk = call.logical_call_pk
		JOIN llm_budget_scope budget
		  ON budget.budget_scope_id = call.budget_scope_id
		WHERE call.logical_call_id = 'logical-cache-cleanup'`,
	).Scan(
		&ledgerID, &attemptID, &settledToken, &settledCost,
		&reservedToken, &reservedCost,
	); err != nil {
		t.Fatalf("读取清理前权威账本: %v", err)
	}
	if _, err := fixture.pool.Exec(context.Background(), `
		DELETE FROM llm_result_cache
		WHERE release_id = $1`,
		fixture.releaseID,
	); err != nil {
		t.Fatalf("清理结果缓存: %v", err)
	}

	var (
		afterLedgerID      string
		afterAttemptID     string
		afterSettledToken  int64
		afterSettledCost   int64
		afterReservedToken int64
		afterReservedCost  int64
	)
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT call.ledger_entry_id::text,
		       attempt.attempt_id::text,
		       budget.provider_tokens_settled,
		       budget.cost_settled_micro_usd,
		       budget.provider_tokens_reserved,
		       budget.cost_reserved_micro_usd
		FROM llm_logical_call call
		JOIN llm_provider_attempt attempt
		  ON attempt.logical_call_pk = call.logical_call_pk
		JOIN llm_budget_scope budget
		  ON budget.budget_scope_id = call.budget_scope_id
		WHERE call.logical_call_id = 'logical-cache-cleanup'`,
	).Scan(
		&afterLedgerID, &afterAttemptID, &afterSettledToken,
		&afterSettledCost, &afterReservedToken, &afterReservedCost,
	); err != nil {
		t.Fatalf("读取清理后权威账本: %v", err)
	}
	if afterLedgerID != ledgerID ||
		afterAttemptID != attemptID ||
		afterSettledToken != settledToken ||
		afterSettledCost != settledCost ||
		afterReservedToken != reservedToken ||
		afterReservedCost != reservedCost {
		t.Fatalf(
			"缓存清理修改了权威账本: ledger %q/%q attempt %q/%q tokens %d/%d->%d/%d cost %d/%d->%d/%d",
			ledgerID, afterLedgerID, attemptID, afterAttemptID,
			settledToken, reservedToken, afterSettledToken, afterReservedToken,
			settledCost, reservedCost, afterSettledCost, afterReservedCost,
		)
	}
}

func TestSchemaFailureIsNotCachedAndRetryCanRepopulate(t *testing.T) {
	fixture := newLLMTestFixture(t)
	fixture.enableResultCache(t)
	fixture.seedResponseSchema(t, `{
		"type": "object",
		"required": ["summary"],
		"properties": {"summary": {"type": "string"}},
		"additionalProperties": false
	}`)
	provider := &fakeProvider{response: llmgw.ProviderResponse{
		Content: `{"summary":42}`, InputTokens: 10, OutputTokens: 5,
		FinishReason: "STOP",
		UsageStatus:  llmgw.ProviderUsageTrusted,
	}}
	client := fixture.chatClient(t, provider)
	req := fixture.chatRequest("logical-cache-schema-failure")
	req.ResponseSchemaKey = "profile_explain_v1"
	req.ResponseSchemaVersion = "v1"

	_, err := client.Chat(trustedChatContext(fixture.principal), req)
	assertStableError(t, err, codes.FailedPrecondition, llmgw.ReasonSchemaValidationFailed)
	var (
		status      string
		hasPayload  bool
		payloadText string
	)
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT status, result_payload IS NOT NULL,
		       COALESCE(result_payload::text, '')
		FROM llm_result_cache`,
	).Scan(&status, &hasPayload, &payloadText); err != nil {
		t.Fatalf("读取 schema 失败后的缓存行: %v", err)
	}
	if status != "FAILED" || hasPayload || payloadText != "" {
		t.Fatalf("schema 失败缓存 = status %q payload %t/%v", status, hasPayload, payloadText)
	}
	if calls := provider.callsSnapshot(); len(calls) != 1 {
		t.Fatalf("schema 失败 provider calls = %d, want 1", len(calls))
	}

	provider.setBehavior(llmgw.ProviderResponse{
		Content: `{"summary":"修复后结果"}`, InputTokens: 10, OutputTokens: 5,
		FinishReason: "STOP",
		UsageStatus:  llmgw.ProviderUsageTrusted,
	}, nil)
	retry := fixture.chatRequest("logical-cache-schema-retry")
	retry.ResponseSchemaKey = "profile_explain_v1"
	retry.ResponseSchemaVersion = "v1"
	if _, err := client.Chat(trustedChatContext(fixture.principal), retry); err != nil {
		t.Fatalf("schema 修复后重试: %v", err)
	}
	if calls := provider.callsSnapshot(); len(calls) != 2 {
		t.Fatalf("失败 claim 重试 provider calls = %d, want 2", len(calls))
	}
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT status, result_payload IS NOT NULL
		FROM llm_result_cache`,
	).Scan(&status, &hasPayload); err != nil {
		t.Fatalf("读取重试后的缓存行: %v", err)
	}
	if status != "READY" || !hasPayload {
		t.Fatalf("重试后的缓存 = %q payload %t", status, hasPayload)
	}
}

func TestSensitiveContentAndProviderErrorsNeverBecomeCachedResults(t *testing.T) {
	t.Run("sensitive input", func(t *testing.T) {
		fixture := newLLMTestFixture(t)
		fixture.enableResultCache(t)
		provider := &fakeProvider{}
		client := fixture.chatClient(t, provider)
		req := fixture.chatRequest("logical-cache-sensitive-input")
		req.Messages[0].Content = "联系手机号 13800138000"

		_, err := client.Chat(trustedChatContext(fixture.principal), req)
		assertStableError(t, err, codes.FailedPrecondition, llmgw.ReasonSensitiveDataNotAllowed)
		if calls := provider.callsSnapshot(); len(calls) != 0 {
			t.Fatalf("敏感输入触发 provider %d 次", len(calls))
		}
		var cacheRows int
		if err := fixture.pool.QueryRow(context.Background(),
			`SELECT count(*) FROM llm_result_cache`,
		).Scan(&cacheRows); err != nil {
			t.Fatalf("统计敏感输入缓存: %v", err)
		}
		if cacheRows != 0 {
			t.Fatalf("敏感输入创建了 %d 个缓存 claim", cacheRows)
		}
	})

	t.Run("sensitive provider output", func(t *testing.T) {
		fixture := newLLMTestFixture(t)
		fixture.enableResultCache(t)
		provider := &fakeProvider{response: llmgw.ProviderResponse{
			Content: "contact candidate@example.com", InputTokens: 10,
			OutputTokens: 5, FinishReason: "STOP",
			UsageStatus: llmgw.ProviderUsageTrusted,
		}}
		client := fixture.chatClient(t, provider)

		_, err := client.Chat(
			trustedChatContext(fixture.principal),
			fixture.chatRequest("logical-cache-sensitive-output"),
		)
		assertStableError(t, err, codes.FailedPrecondition, llmgw.ReasonSensitiveDataNotAllowed)
		var (
			status     string
			hasPayload bool
			cacheText  string
		)
		if err := fixture.pool.QueryRow(context.Background(), `
			SELECT status, result_payload IS NOT NULL,
			       request_key_manifest::text || COALESCE(result_payload::text, '')
			FROM llm_result_cache`,
		).Scan(&status, &hasPayload, &cacheText); err != nil {
			t.Fatalf("读取敏感输出缓存: %v", err)
		}
		if status != "FAILED" || hasPayload ||
			strings.Contains(cacheText, "candidate@example.com") {
			t.Fatalf("敏感输出进入缓存 = status %q payload %t text %q",
				status, hasPayload, cacheText)
		}
	})

	t.Run("provider error", func(t *testing.T) {
		fixture := newLLMTestFixture(t)
		fixture.enableResultCache(t)
		provider := &fakeProvider{err: errors.New("raw provider body: secret stack")}
		client := fixture.chatClient(t, provider)

		_, err := client.Chat(
			trustedChatContext(fixture.principal),
			fixture.chatRequest("logical-cache-provider-error"),
		)
		assertStableError(t, err, codes.Unavailable, llmgw.ReasonReconciliation)
		var (
			status     string
			hasPayload bool
			cacheText  string
		)
		if err := fixture.pool.QueryRow(context.Background(), `
			SELECT status, result_payload IS NOT NULL,
			       request_key_manifest::text || COALESCE(result_payload::text, '')
			FROM llm_result_cache`,
		).Scan(&status, &hasPayload, &cacheText); err != nil {
			t.Fatalf("读取 provider 错误缓存: %v", err)
		}
		if status != "FAILED" || hasPayload ||
			strings.Contains(cacheText, "raw provider body") {
			t.Fatalf("provider 原始错误进入缓存 = status %q payload %t text %q",
				status, hasPayload, cacheText)
		}
	})
}
