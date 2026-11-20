//go:build integration

package llmgw_test

import (
	"context"
	"fmt"
	"strings"
	"testing"

	"google.golang.org/grpc/codes"

	llmgwv1 "github.com/choucisan/ai-gaokao-counselor/api/llmgw/v1"
	"github.com/choucisan/ai-gaokao-counselor/internal/llmgw"
)

func TestChatRequiresDataClassAllowedByRoute(t *testing.T) {
	fixture := newLLMTestFixture(t)
	provider := &fakeProvider{response: llmgw.ProviderResponse{
		Content: "按脱敏分类生成", InputTokens: 1, OutputTokens: 1, FinishReason: "STOP",
		UsageStatus: llmgw.ProviderUsageTrusted,
	}}
	client := fixture.chatClient(t, provider)

	rejected := fixture.chatRequest("logical-principal-not-allowed")
	rejected.DataClassification = ""
	rejected.DataClass = llmgwv1.DataClassification_PRINCIPAL_DATA
	_, err := client.Chat(trustedChatContext(fixture.principal), rejected)
	assertStableError(t, err, codes.FailedPrecondition, llmgw.ReasonSensitiveDataNotAllowed)
	if calls := provider.callsSnapshot(); len(calls) != 0 {
		t.Fatalf("route 未允许的 data class 仍触发 provider %d 次", len(calls))
	}

	safeClasses := []llmgwv1.DataClassification{
		llmgwv1.DataClassification_PUBLIC_DATA,
		llmgwv1.DataClassification_PRINCIPAL_DATA,
		llmgwv1.DataClassification_INQUIRY_DATA,
		llmgwv1.DataClassification_OPERATIONAL_DATA,
	}
	for _, dataClass := range safeClasses {
		className := dataClass.String()
		if _, err := fixture.pool.Exec(context.Background(), `
			UPDATE llm_role_route
			SET allowed_data_classes = ARRAY[$1]::TEXT[]
			WHERE release_id = $2 AND role = 'profile'`,
			className, fixture.releaseID,
		); err != nil {
			t.Fatalf("允许 route data class %s: %v", className, err)
		}
		req := fixture.chatRequest("logical-class-" + strings.ToLower(className))
		req.DataClassification = ""
		req.DataClass = dataClass
		if _, err := client.Chat(trustedChatContext(fixture.principal), req); err != nil {
			t.Fatalf("route 允许的 data class %s 被拒绝: %v", className, err)
		}
		var storedClass string
		if err := fixture.pool.QueryRow(context.Background(), `
			SELECT data_classification
			FROM llm_logical_call
			WHERE logical_call_id = $1`,
			req.LogicalCallId,
		).Scan(&storedClass); err != nil {
			t.Fatalf("读取 logical call data class: %v", err)
		}
		if storedClass != className {
			t.Fatalf("stored data class = %q, want %q", storedClass, className)
		}
	}
	if calls := provider.callsSnapshot(); len(calls) != len(safeClasses) {
		t.Fatalf("provider calls = %d, want %d", len(calls), len(safeClasses))
	}
}

func TestChatAlwaysRejectsSensitiveAndSecretDataClasses(t *testing.T) {
	fixture := newLLMTestFixture(t)
	provider := &fakeProvider{}
	client := fixture.chatClient(t, provider)

	for _, dataClass := range []llmgwv1.DataClassification{
		llmgwv1.DataClassification_SENSITIVE_ELIGIBILITY_DATA,
		llmgwv1.DataClassification_SECRET,
	} {
		req := fixture.chatRequest("logical-blocked-" + strings.ToLower(dataClass.String()))
		req.DataClassification = ""
		req.DataClass = dataClass
		_, err := client.Chat(trustedChatContext(fixture.principal), req)
		assertStableError(t, err, codes.FailedPrecondition, llmgw.ReasonSensitiveDataNotAllowed)
	}
	if calls := provider.callsSnapshot(); len(calls) != 0 {
		t.Fatalf("敏感/机密分类触发 provider %d 次", len(calls))
	}

	if _, err := fixture.pool.Exec(context.Background(), `
		UPDATE llm_role_route
		SET allowed_data_classes = ARRAY['PUBLIC_DATA', 'SENSITIVE_ELIGIBILITY_DATA']::TEXT[]
		WHERE release_id = $1 AND role = 'profile'`,
		fixture.releaseID,
	); err == nil {
		t.Fatal("route 不应允许 SENSITIVE_ELIGIBILITY_DATA")
	}
	if _, err := fixture.pool.Exec(context.Background(), `
		UPDATE llm_role_route
		SET allowed_data_classes = ARRAY['PUBLIC_DATA', 'SECRET']::TEXT[]
		WHERE release_id = $1 AND role = 'profile'`,
		fixture.releaseID,
	); err == nil {
		t.Fatal("route 不应允许 SECRET")
	}
}

func TestChatMapsLegacyDataClassesWithoutExpandingCloudAccess(t *testing.T) {
	tests := []struct {
		name         string
		legacy       string
		wantCode     codes.Code
		wantReason   string
		allowedRoute []string
		wantProvider int
	}{
		{
			name: "public maps to public data", legacy: "PUBLIC",
			allowedRoute: []string{"PUBLIC_DATA"}, wantProvider: 1,
		},
		{
			name: "internal does not become public", legacy: "INTERNAL",
			wantCode: codes.FailedPrecondition, wantReason: llmgw.ReasonSensitiveDataNotAllowed,
			allowedRoute: []string{"PUBLIC_DATA"},
		},
		{
			name: "internal keeps explicit principal permission", legacy: "INTERNAL",
			allowedRoute: []string{"PRINCIPAL_DATA"}, wantProvider: 1,
		},
		{
			name: "sensitive stays blocked", legacy: "SENSITIVE",
			wantCode: codes.FailedPrecondition, wantReason: llmgw.ReasonSensitiveDataNotAllowed,
			allowedRoute: []string{"PUBLIC_DATA"},
		},
		{
			name: "unknown legacy value fails closed", legacy: "EXTERNAL",
			wantCode: codes.InvalidArgument, wantReason: llmgw.ReasonRequestInvalid,
			allowedRoute: []string{"PUBLIC_DATA"},
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			fixture := newLLMTestFixture(t)
			if _, err := fixture.pool.Exec(context.Background(), `
				UPDATE llm_role_route
				SET allowed_data_classes = $1::TEXT[]
				WHERE release_id = $2 AND role = 'profile'`,
				tt.allowedRoute, fixture.releaseID,
			); err != nil {
				t.Fatalf("设置 route allowed_data_classes: %v", err)
			}
			provider := &fakeProvider{response: llmgw.ProviderResponse{
				Content: "兼容分类结果", InputTokens: 1, OutputTokens: 1, FinishReason: "STOP",
				UsageStatus: llmgw.ProviderUsageTrusted,
			}}
			client := fixture.chatClient(t, provider)
			req := fixture.chatRequest("logical-legacy-" + strings.ToLower(strings.ReplaceAll(tt.name, " ", "-")))
			req.DataClass = llmgwv1.DataClassification_DATA_CLASSIFICATION_UNSPECIFIED
			req.DataClassification = tt.legacy

			_, err := client.Chat(trustedChatContext(fixture.principal), req)
			if tt.wantReason != "" {
				assertStableError(t, err, tt.wantCode, tt.wantReason)
			} else if err != nil {
				t.Fatalf("Chat: %v", err)
			}
			if calls := provider.callsSnapshot(); len(calls) != tt.wantProvider {
				t.Fatalf("provider calls = %d, want %d", len(calls), tt.wantProvider)
			}
		})
	}
}

func TestChatRejectsUnknownTypedDataClassBeforeProvider(t *testing.T) {
	fixture := newLLMTestFixture(t)
	provider := &fakeProvider{}
	client := fixture.chatClient(t, provider)
	req := fixture.chatRequest("logical-unknown-data-class")
	req.DataClassification = ""
	req.DataClass = llmgwv1.DataClassification(99)

	_, err := client.Chat(trustedChatContext(fixture.principal), req)
	assertStableError(t, err, codes.InvalidArgument, llmgw.ReasonRequestInvalid)
	if calls := provider.callsSnapshot(); len(calls) != 0 {
		t.Fatalf("未知 data class 触发 provider %d 次", len(calls))
	}

	conflicting := fixture.chatRequest("logical-conflicting-data-class")
	conflicting.DataClassification = "PUBLIC_DATA"
	conflicting.DataClass = llmgwv1.DataClassification_SECRET
	_, err = client.Chat(trustedChatContext(fixture.principal), conflicting)
	assertStableError(t, err, codes.InvalidArgument, llmgw.ReasonRequestInvalid)
	if calls := provider.callsSnapshot(); len(calls) != 0 {
		t.Fatalf("冲突 data class 触发 provider %d 次", len(calls))
	}
}

func TestChatRejectsDirectIdentifiersAndRawSensitiveFactsBeforePersistence(t *testing.T) {
	fixture := newLLMTestFixture(t)
	provider := &fakeProvider{}
	client := fixture.chatClient(t, provider)

	tests := []struct {
		name   string
		mutate func(*llmgwv1.ChatRequest)
		secret string
	}{
		{
			name: "mobile number",
			mutate: func(req *llmgwv1.ChatRequest) {
				req.Messages[0].Content = "手机号：13800138000"
			},
			secret: "13800138000",
		},
		{
			name: "formatted mobile number",
			mutate: func(req *llmgwv1.ChatRequest) {
				req.Messages[0].Content = "手机号：138-0013-8000"
			},
			secret: "138-0013-8000",
		},
		{
			name: "resident identity number",
			mutate: func(req *llmgwv1.ChatRequest) {
				req.Messages[0].Content = "身份证 11010519491231002X"
			},
			secret: "11010519491231002X",
		},
		{
			name: "email address",
			mutate: func(req *llmgwv1.ChatRequest) {
				req.Messages[0].Content = "联系 candidate@example.com"
			},
			secret: "candidate@example.com",
		},
		{
			name: "raw colour vision fact",
			mutate: func(req *llmgwv1.ChatRequest) {
				req.Messages[0].Content = "我有色弱"
			},
			secret: "色弱",
		},
		{
			name: "raw visual acuity fact",
			mutate: func(req *llmgwv1.ChatRequest) {
				req.Messages[0].Content = "裸眼视力4.8"
			},
			secret: "4.8",
		},
		{
			name: "raw gender fact",
			mutate: func(req *llmgwv1.ChatRequest) {
				req.Messages[0].Content = "性别：女"
			},
			secret: "性别：女",
		},
		{
			name: "raw household registration fact",
			mutate: func(req *llmgwv1.ChatRequest) {
				req.Messages[0].Content = "户籍：湖南长沙"
			},
			secret: "湖南长沙",
		},
		{
			name: "raw school registration in prompt variable",
			mutate: func(req *llmgwv1.ChatRequest) {
				req.PromptVariables["candidate_name"] = "学籍：长沙一中"
			},
			secret: "长沙一中",
		},
		{
			name: "direct identifier in trace",
			mutate: func(req *llmgwv1.ChatRequest) {
				req.Trace.TraceId = "trace-13912345678"
			},
			secret: "13912345678",
		},
		{
			name: "model secret token",
			mutate: func(req *llmgwv1.ChatRequest) {
				req.Messages[0].Content = "api key sk-live-1234567890abcdef"
			},
			secret: "sk-live-1234567890abcdef",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			logicalCallID := "logical-sensitive-" + strings.ReplaceAll(tt.name, " ", "-")
			req := fixture.chatRequest(logicalCallID)
			tt.mutate(req)

			_, err := client.Chat(trustedChatContext(fixture.principal), req)
			assertStableError(t, err, codes.FailedPrecondition, llmgw.ReasonSensitiveDataNotAllowed)
			if strings.Contains(err.Error(), tt.secret) {
				t.Fatalf("错误泄漏敏感原文 %q: %v", tt.secret, err)
			}
			var logicalCount, projectionCount int
			if err := fixture.pool.QueryRow(context.Background(), `
				SELECT
					(SELECT count(*) FROM llm_logical_call WHERE logical_call_id = $1),
					(SELECT count(*) FROM llm_call_log WHERE logical_call_id = $1)`,
				logicalCallID,
			).Scan(&logicalCount, &projectionCount); err != nil {
				t.Fatalf("检查敏感拒绝持久化: %v", err)
			}
			if logicalCount != 0 || projectionCount != 0 {
				t.Fatalf("敏感拒绝仍写入 logical/projection = %d/%d", logicalCount, projectionCount)
			}
		})
	}
	if calls := provider.callsSnapshot(); len(calls) != 0 {
		t.Fatalf("敏感原文触发 provider %d 次", len(calls))
	}
}

func TestChatDoesNotPersistUnsafeProviderOutput(t *testing.T) {
	tests := []struct {
		name    string
		content string
		secret  string
	}{
		{
			name:    "mobile number echoed",
			content: "联系手机号 13800138000",
			secret:  "13800138000",
		},
		{
			name:    "raw colour vision fact echoed",
			content: "该考生色弱，限制规则为 FAIL",
			secret:  "色弱",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			fixture := newLLMTestFixture(t)
			provider := &fakeProvider{response: llmgw.ProviderResponse{
				Content: tt.content, InputTokens: 4, OutputTokens: 2,
				FinishReason: "STOP", ProviderRequestID: "provider-safe-id",
				UsageStatus: llmgw.ProviderUsageTrusted,
			}}
			client := fixture.chatClient(t, provider)
			logicalCallID := "logical-unsafe-output-" + strings.ReplaceAll(tt.name, " ", "-")
			req := fixture.chatRequest(logicalCallID)

			resp, err := client.Chat(trustedChatContext(fixture.principal), req)
			assertStableError(t, err, codes.FailedPrecondition, llmgw.ReasonSensitiveDataNotAllowed)
			if resp != nil {
				t.Fatalf("不安全供应商输出返回了响应: %+v", resp)
			}
			if strings.Contains(err.Error(), tt.secret) {
				t.Fatalf("错误泄漏供应商敏感原文 %q: %v", tt.secret, err)
			}
			if calls := provider.callsSnapshot(); len(calls) != 1 {
				t.Fatalf("不安全输出 provider calls = %d, want 1", len(calls))
			}

			var (
				logicalStatus    string
				errorReason      *string
				responsePayload  *string
				attemptStatus    string
				attemptErrorCode *string
				settledTokens    int64
				settledCost      int64
				reservedTokens   int64
				reservedCost     int64
				projectionStatus string
				projectionError  string
			)
			if err := fixture.pool.QueryRow(context.Background(), `
				SELECT logical.status,
				       logical.error_reason,
				       logical.response_payload::text,
				       attempt.status,
				       attempt.error_code,
				       budget.provider_tokens_settled,
				       budget.cost_settled_micro_usd,
				       budget.provider_tokens_reserved,
				       budget.cost_reserved_micro_usd
				FROM llm_logical_call logical
				JOIN llm_provider_attempt attempt
				  ON attempt.logical_call_pk = logical.logical_call_pk
				JOIN llm_budget_scope budget
				  ON budget.budget_scope_id = logical.budget_scope_id
				WHERE logical.logical_call_id = $1`,
				logicalCallID,
			).Scan(
				&logicalStatus, &errorReason, &responsePayload,
				&attemptStatus, &attemptErrorCode, &settledTokens,
				&settledCost, &reservedTokens, &reservedCost,
			); err != nil {
				t.Fatalf("读取不安全输出账本: %v", err)
			}
			if logicalStatus != "FAILED" || attemptStatus != "SUCCEEDED" {
				t.Fatalf("不安全输出状态 = logical %q attempt %q", logicalStatus, attemptStatus)
			}
			if errorReason == nil || *errorReason != llmgw.ReasonSensitiveDataNotAllowed ||
				attemptErrorCode == nil || *attemptErrorCode != llmgw.ReasonSensitiveDataNotAllowed {
				t.Fatalf("不安全输出原因 = logical %v attempt %v", errorReason, attemptErrorCode)
			}
			if responsePayload != nil {
				t.Fatalf("不安全供应商响应写入账本: %q", *responsePayload)
			}
			if settledTokens != 6 || settledCost != 8 || reservedTokens != 0 || reservedCost != 0 {
				t.Fatalf("不安全输出结算 = settled %d/%d reserved %d/%d",
					settledTokens, settledCost, reservedTokens, reservedCost)
			}
			if err := fixture.pool.QueryRow(context.Background(), `
				SELECT status, error_message
				FROM llm_call_log
				WHERE logical_call_id = $1`,
				logicalCallID,
			).Scan(&projectionStatus, &projectionError); err != nil {
				t.Fatalf("读取不安全输出投影: %v", err)
			}
			if projectionStatus != "fail" || projectionError != llmgw.ReasonSensitiveDataNotAllowed {
				t.Fatalf("不安全输出投影 = %q/%q", projectionStatus, projectionError)
			}
			if strings.Contains(projectionError, tt.secret) {
				t.Fatalf("投影泄漏敏感原文 %q", projectionError)
			}

			_, replayErr := client.Chat(trustedChatContext(fixture.principal), req)
			assertStableError(t, replayErr, codes.FailedPrecondition, llmgw.ReasonSensitiveDataNotAllowed)
			if calls := provider.callsSnapshot(); len(calls) != 1 {
				t.Fatalf("不安全输出重放 provider calls = %d, want 1", len(calls))
			}
		})
	}
}

func TestChatAllowsSanitizedEligibilityConclusionInProviderOutput(t *testing.T) {
	fixture := newLLMTestFixture(t)
	provider := &fakeProvider{response: llmgw.ProviderResponse{
		Content:     "色觉要求结论：FAIL；健康限制核验为 UNKNOWN",
		InputTokens: 3, OutputTokens: 2, FinishReason: "STOP",
		UsageStatus: llmgw.ProviderUsageTrusted,
	}}
	client := fixture.chatClient(t, provider)

	resp, err := client.Chat(
		trustedChatContext(fixture.principal),
		fixture.chatRequest("logical-safe-sanitized-output"),
	)
	if err != nil {
		t.Fatalf("合法脱敏供应商输出被拒绝: %v", err)
	}
	if !strings.Contains(resp.GetContent(), "PASS") &&
		!strings.Contains(resp.GetContent(), "FAIL") &&
		!strings.Contains(resp.GetContent(), "UNKNOWN") {
		t.Fatalf("合法脱敏供应商输出内容异常: %q", resp.GetContent())
	}
}

func TestChatAllowsSanitizedEligibilityRuleConclusions(t *testing.T) {
	fixture := newLLMTestFixture(t)
	provider := &fakeProvider{response: llmgw.ProviderResponse{
		Content: "已按脱敏规则结论生成", InputTokens: 1, OutputTokens: 1, FinishReason: "STOP",
		UsageStatus: llmgw.ProviderUsageTrusted,
	}}
	client := fixture.chatClient(t, provider)
	conclusions := []string{
		"色觉要求结论：FAIL",
		"健康限制核验为 UNKNOWN",
		"户籍规则结论：PASS",
		"视力条件：FAIL",
	}
	for index, conclusion := range conclusions {
		req := fixture.chatRequest(fmt.Sprintf("logical-sanitized-%d", index))
		req.Messages[0].Content = conclusion
		if _, err := client.Chat(trustedChatContext(fixture.principal), req); err != nil {
			t.Fatalf("合法脱敏结论 [%d] %q 被拒绝: %v", index, conclusion, err)
		}
	}
	calls := provider.callsSnapshot()
	if len(calls) != len(conclusions) {
		t.Fatalf("合法脱敏结论 provider calls = %d, want %d", len(calls), len(conclusions))
	}
	for index, call := range calls {
		contents := make([]string, 0, len(call.messages))
		for _, message := range call.messages {
			contents = append(contents, message.Content)
		}
		joined := strings.Join(contents, "\n")
		if !strings.Contains(joined, conclusions[index]) {
			t.Fatalf("provider 未收到脱敏结论 %q: %s", conclusions[index], joined)
		}
	}
}

func TestChatRejectsDirectIdentifierFormedByPromptRendering(t *testing.T) {
	fixture := newLLMTestFixture(t)
	fixture.seedPromptVersion(t, "v2", "请处理{{candidate_name}}{{topic}}", "")
	provider := &fakeProvider{}
	client := fixture.chatClient(t, provider)
	req := fixture.chatRequest("logical-rendered-identifier")
	req.PromptTemplateVersion = "v2"
	req.PromptVariables["candidate_name"] = "1380013"
	req.PromptVariables["topic"] = "8000"

	_, err := client.Chat(trustedChatContext(fixture.principal), req)
	assertStableError(t, err, codes.FailedPrecondition, llmgw.ReasonSensitiveDataNotAllowed)
	if calls := provider.callsSnapshot(); len(calls) != 0 {
		t.Fatalf("模板渲染后形成的直接标识触发 provider %d 次", len(calls))
	}
	var logicalCount int
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT count(*) FROM llm_logical_call WHERE logical_call_id = $1`,
		req.LogicalCallId,
	).Scan(&logicalCount); err != nil {
		t.Fatalf("检查模板渲染拒绝持久化: %v", err)
	}
	if logicalCount != 0 {
		t.Fatalf("模板渲染后形成的直接标识仍写入 logical call %d 条", logicalCount)
	}
}

func TestChatDoesNotPersistEscapedSensitiveStructuredOutput(t *testing.T) {
	fixture := newLLMTestFixture(t)
	fixture.seedResponseSchema(t, `{
		"type": "object",
		"additionalProperties": false,
		"required": ["summary"],
		"properties": {"summary": {"type": "string"}}
	}`)
	provider := &fakeProvider{response: llmgw.ProviderResponse{
		Content:     `{"summary":"\u0031\u0033\u0038\u0030\u0030\u0031\u0033\u0038\u0030\u0030\u0030"}`,
		InputTokens: 4, OutputTokens: 2, FinishReason: "STOP",
		UsageStatus: llmgw.ProviderUsageTrusted,
	}}
	client := fixture.chatClient(t, provider)
	req := fixture.chatRequest("logical-escaped-sensitive-output")
	req.ResponseSchemaKey = "profile_explain_v1"
	req.ResponseSchemaVersion = "v1"

	resp, err := client.Chat(trustedChatContext(fixture.principal), req)
	assertStableError(t, err, codes.FailedPrecondition, llmgw.ReasonSensitiveDataNotAllowed)
	if resp != nil {
		t.Fatalf("转义后的敏感结构化输出返回了响应: %+v", resp)
	}
	var (
		logicalStatus string
		payload       []byte
	)
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT status, response_payload
		FROM llm_logical_call
		WHERE logical_call_id = $1`,
		req.LogicalCallId,
	).Scan(&logicalStatus, &payload); err != nil {
		t.Fatalf("读取转义敏感输出账本: %v", err)
	}
	if logicalStatus != "FAILED" {
		t.Fatalf("转义敏感输出 logical status = %q", logicalStatus)
	}
	if payload != nil {
		t.Fatalf("转义敏感输出写入了 response_payload: %q", payload)
	}
	if calls := provider.callsSnapshot(); len(calls) != 1 {
		t.Fatalf("转义敏感输出 provider calls = %d, want 1", len(calls))
	}
}
