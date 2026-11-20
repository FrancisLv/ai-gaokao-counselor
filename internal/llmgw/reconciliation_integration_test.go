//go:build integration

package llmgw_test

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"

	kerrors "github.com/go-kratos/kratos/v2/errors"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/choucisan/ai-gaokao-counselor/internal/llmgw"
)

func TestProviderTimeoutPreservesReservationAndReturnsReconciliationIdentity(t *testing.T) {
	fixture := newLLMTestFixture(t)
	provider := &fakeProvider{
		err: fmt.Errorf("upstream body must stay private: %w", context.DeadlineExceeded),
	}
	client := fixture.chatClient(t, provider)
	req := fixture.chatRequest("logical-timeout-pending")

	_, err := client.Chat(trustedChatContext(fixture.principal), req)
	assertStableError(t, err, codes.Unavailable, llmgw.ReasonProviderTimeout)
	if strings.Contains(status.Convert(err).Message(), "upstream body") {
		t.Fatalf("稳定错误泄露供应商正文: %v", status.Convert(err).Message())
	}
	errorMetadata := kerrors.FromError(err).GetMetadata()
	if errorMetadata["logical_call_id"] != req.GetLogicalCallId() ||
		errorMetadata["attempt_id"] == "" ||
		errorMetadata["ledger_entry_id"] == "" {
		t.Fatalf("待对账错误身份 metadata = %+v", errorMetadata)
	}

	var (
		callStatus       string
		attemptStatus    string
		attemptID        string
		ledgerEntryID    string
		reservedTokens   int64
		reservedCost     int64
		attemptReserved  int64
		attemptReservedC int64
	)
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT call.status,
		       attempt.status,
		       attempt.attempt_id::text,
		       call.ledger_entry_id::text,
		       budget.provider_tokens_reserved,
		       budget.cost_reserved_micro_usd,
		       attempt.reserved_tokens,
		       attempt.reserved_cost_micro_usd
		FROM llm_logical_call call
		JOIN llm_budget_scope budget
		  ON budget.budget_scope_id = call.budget_scope_id
		JOIN llm_provider_attempt attempt
		  ON attempt.logical_call_pk = call.logical_call_pk
		WHERE call.logical_call_id = $1`,
		req.GetLogicalCallId(),
	).Scan(
		&callStatus, &attemptStatus, &attemptID, &ledgerEntryID,
		&reservedTokens, &reservedCost, &attemptReserved, &attemptReservedC,
	); err != nil {
		t.Fatalf("读取未知计费账本: %v", err)
	}
	if callStatus != "PARTIAL_UNKNOWN" ||
		attemptStatus != "PENDING_RECONCILIATION" ||
		attemptID != errorMetadata["attempt_id"] ||
		ledgerEntryID != errorMetadata["ledger_entry_id"] ||
		reservedTokens <= 0 ||
		reservedCost <= 0 ||
		attemptReserved <= 0 ||
		attemptReservedC <= 0 {
		t.Fatalf(
			"未知计费账本 = call %q attempt %q/%q metadata %+v reserved %d/%d/%d/%d",
			callStatus, attemptStatus, attemptID, errorMetadata,
			reservedTokens, reservedCost, attemptReserved, attemptReservedC,
		)
	}

	_, replayErr := client.Chat(trustedChatContext(fixture.principal), req)
	assertStableError(t, replayErr, codes.Unavailable, llmgw.ReasonProviderTimeout)
	replayMetadata := kerrors.FromError(replayErr).GetMetadata()
	if replayMetadata["attempt_id"] != attemptID ||
		replayMetadata["ledger_entry_id"] != ledgerEntryID {
		t.Fatalf("重复待对账调用未返回原身份: %+v", replayMetadata)
	}
	if calls := provider.callsSnapshot(); len(calls) != 1 {
		t.Fatalf("长期未知调用新增 provider calls = %d, want 1", len(calls))
	}
}

func TestUntrustedProviderUsagePreservesReservationAsUnknownCharge(t *testing.T) {
	fixture := newLLMTestFixture(t)
	provider := &fakeProvider{response: llmgw.ProviderResponse{
		Content:           "供应商正文",
		InputTokens:       100,
		OutputTokens:      50,
		ProviderRequestID: "provider-untrusted-usage",
		FinishReason:      "STOP",
		UsageStatus:       llmgw.ProviderUsageUntrusted,
	}}
	client := fixture.chatClient(t, provider)
	req := fixture.chatRequest("logical-untrusted-usage")

	_, err := client.Chat(trustedChatContext(fixture.principal), req)
	assertStableError(t, err, codes.Unavailable, llmgw.ReasonReconciliation)
	errorMetadata := kerrors.FromError(err).GetMetadata()
	if errorMetadata["logical_call_id"] != req.GetLogicalCallId() ||
		errorMetadata["attempt_id"] == "" ||
		errorMetadata["ledger_entry_id"] == "" {
		t.Fatalf("usage 不可信错误身份 metadata = %+v", errorMetadata)
	}

	var (
		callStatus       string
		attemptStatus    string
		inputTokens      *int64
		outputTokens     *int64
		costMicroUSD     *int64
		reservedTokens   int64
		reservedCost     int64
		settledTokens    int64
		settledCost      int64
		attemptErrorCode string
	)
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT call.status,
		       attempt.status,
		       attempt.input_tokens,
		       attempt.output_tokens,
		       attempt.cost_micro_usd,
		       budget.provider_tokens_reserved,
		       budget.cost_reserved_micro_usd,
		       budget.provider_tokens_settled,
		       budget.cost_settled_micro_usd,
		       attempt.error_code
		FROM llm_logical_call call
		JOIN llm_budget_scope budget
		  ON budget.budget_scope_id = call.budget_scope_id
		JOIN llm_provider_attempt attempt
		  ON attempt.logical_call_pk = call.logical_call_pk
		WHERE call.logical_call_id = $1`,
		req.GetLogicalCallId(),
	).Scan(
		&callStatus, &attemptStatus, &inputTokens, &outputTokens,
		&costMicroUSD, &reservedTokens, &reservedCost, &settledTokens,
		&settledCost, &attemptErrorCode,
	); err != nil {
		t.Fatalf("读取 usage 不可信账本: %v", err)
	}
	if callStatus != "PARTIAL_UNKNOWN" ||
		attemptStatus != "PENDING_RECONCILIATION" ||
		attemptErrorCode != llmgw.ReasonReconciliation ||
		inputTokens != nil ||
		outputTokens != nil ||
		costMicroUSD != nil ||
		reservedTokens <= 0 ||
		reservedCost <= 0 ||
		settledTokens != 0 ||
		settledCost != 0 {
		t.Fatalf(
			"usage 不可信账本 = call %q attempt %q/%q usage %v/%v cost %v reserved %d/%d settled %d/%d",
			callStatus, attemptStatus, attemptErrorCode, inputTokens, outputTokens,
			costMicroUSD, reservedTokens, reservedCost, settledTokens, settledCost,
		)
	}
}

func TestMissingProviderUsagePreservesReservationAsUnknownCharge(t *testing.T) {
	fixture := newLLMTestFixture(t)
	provider := &fakeProvider{response: llmgw.ProviderResponse{
		Content:      "响应可用但供应商没有返回 usage",
		FinishReason: "STOP",
	}}
	client := fixture.chatClient(t, provider)

	_, err := client.Chat(
		trustedChatContext(fixture.principal),
		fixture.chatRequest("logical-missing-usage"),
	)
	assertStableError(t, err, codes.Unavailable, llmgw.ReasonReconciliation)
	errorMetadata := kerrors.FromError(err).GetMetadata()
	if errorMetadata["attempt_id"] == "" ||
		errorMetadata["ledger_entry_id"] == "" {
		t.Fatalf("缺失 usage 错误身份 metadata = %+v", errorMetadata)
	}
	var (
		callStatus      string
		attemptStatus   string
		reservedTokens  int64
		reservedCost    int64
		settledTokens   int64
		settledCost     int64
		attemptInput    *int64
		attemptOutput   *int64
		attemptCost     *int64
		attemptError    string
		attemptSummaryN int
	)
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT call.status,
		       attempt.status,
		       budget.provider_tokens_reserved,
		       budget.cost_reserved_micro_usd,
		       budget.provider_tokens_settled,
		       budget.cost_settled_micro_usd,
		       attempt.input_tokens,
		       attempt.output_tokens,
		       attempt.cost_micro_usd,
		       attempt.error_code,
		       count(*) OVER ()
		FROM llm_logical_call call
		JOIN llm_budget_scope budget
		  ON budget.budget_scope_id = call.budget_scope_id
		JOIN llm_provider_attempt attempt
		  ON attempt.logical_call_pk = call.logical_call_pk
		WHERE call.logical_call_id = 'logical-missing-usage'`,
	).Scan(
		&callStatus, &attemptStatus, &reservedTokens, &reservedCost,
		&settledTokens, &settledCost, &attemptInput, &attemptOutput,
		&attemptCost, &attemptError, &attemptSummaryN,
	); err != nil {
		t.Fatalf("读取缺失 usage 账本: %v", err)
	}
	if callStatus != "PARTIAL_UNKNOWN" ||
		attemptStatus != "PENDING_RECONCILIATION" ||
		reservedTokens <= 0 ||
		reservedCost <= 0 ||
		settledTokens != 0 ||
		settledCost != 0 ||
		attemptInput != nil ||
		attemptOutput != nil ||
		attemptCost != nil ||
		attemptError != llmgw.ReasonReconciliation ||
		attemptSummaryN != 1 {
		t.Fatalf(
			"缺失 usage 账本 = call %q attempt %q/%q reserve %d/%d settled %d/%d usage %v/%v cost %v attempts %d",
			callStatus, attemptStatus, attemptError,
			reservedTokens, reservedCost, settledTokens, settledCost,
			attemptInput, attemptOutput, attemptCost, attemptSummaryN,
		)
	}
}

func TestReconciliationSettlesKnownUsageOnceAndPreservesAuditBasis(t *testing.T) {
	fixture := newLLMTestFixture(t)
	provider := &fakeProvider{
		err: fmt.Errorf("connection dropped after request: %w", context.DeadlineExceeded),
	}
	client := fixture.chatClient(t, provider)
	req := fixture.chatRequest("logical-reconcile-charged")

	_, err := client.Chat(trustedChatContext(fixture.principal), req)
	assertStableError(t, err, codes.Unavailable, llmgw.ReasonProviderTimeout)
	errorMetadata := kerrors.FromError(err).GetMetadata()
	attemptID := errorMetadata["attempt_id"]
	ledgerEntryID := errorMetadata["ledger_entry_id"]

	var (
		attemptReservedTokens int64
		attemptReservedCost   int64
		logicalReservedTokens int64
		logicalReservedCost   int64
		originalInputRate     int64
		originalOutputRate    int64
	)
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT attempt.reserved_tokens,
		       attempt.reserved_cost_micro_usd,
		       call.reserved_input_tokens + call.reserved_output_tokens,
		       call.reserved_cost_micro_usd,
		       price.input_micro_usd_per_million_tokens,
		       price.output_micro_usd_per_million_tokens
		FROM llm_logical_call call
		JOIN llm_provider_attempt attempt
		  ON attempt.logical_call_pk = call.logical_call_pk
		JOIN llm_price_entry price
		  ON price.price_table_version = call.price_table_version
		 AND price.model_id = attempt.model_id
		WHERE call.logical_call_id = $1
		  AND attempt.attempt_id = $2`,
		req.GetLogicalCallId(), attemptID,
	).Scan(
		&attemptReservedTokens, &attemptReservedCost,
		&logicalReservedTokens, &logicalReservedCost,
		&originalInputRate, &originalOutputRate,
	); err != nil {
		t.Fatalf("读取对账前预留与价格: %v", err)
	}

	reconciler := llmgw.NewReconciler(fixture.pool)
	reconciliation := llmgw.ReconciliationRequest{
		LogicalCallID: req.GetLogicalCallId(),
		AttemptID:     attemptID,
		Outcome:       llmgw.ReconciliationCharged,
		InputTokens:   10,
		OutputTokens:  5,
		EvidenceRef:   "provider-bill:invoice-2026-11-20:line-1",
	}
	result, err := reconciler.Reconcile(context.Background(), reconciliation)
	if err != nil {
		t.Fatalf("按明确账单对账: %v", err)
	}
	if result.LogicalCallID != req.GetLogicalCallId() ||
		result.AttemptID != attemptID ||
		result.LedgerEntryID != ledgerEntryID ||
		result.Outcome != llmgw.ReconciliationCharged ||
		result.LogicalStatus != "FAILED" ||
		result.AttemptStatus != "SUCCEEDED" ||
		result.BudgetStatus != "ACTIVE" ||
		result.InputTokens != 10 ||
		result.OutputTokens != 5 ||
		result.CostMicroUSD != 20 {
		t.Fatalf("明确计费对账结果 = %+v", result)
	}

	var (
		budgetTokensReserved int64
		budgetCostReserved   int64
		budgetTokensSettled  int64
		budgetCostSettled    int64
		callStatus           string
		callError            string
		callInput            int64
		callOutput           int64
		callCost             int64
		attemptStatus        string
		attemptInput         int64
		attemptOutput        int64
		attemptCost          int64
		attemptTokensHeld    int64
		attemptCostHeld      int64
		priceInputRate       int64
		priceOutputRate      int64
	)
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT budget.provider_tokens_reserved,
		       budget.cost_reserved_micro_usd,
		       budget.provider_tokens_settled,
		       budget.cost_settled_micro_usd,
		       call.status,
		       call.error_reason,
		       call.actual_input_tokens,
		       call.actual_output_tokens,
		       call.actual_cost_micro_usd,
		       attempt.status,
		       attempt.input_tokens,
		       attempt.output_tokens,
		       attempt.cost_micro_usd,
		       attempt.reserved_tokens,
		       attempt.reserved_cost_micro_usd,
		       price.input_micro_usd_per_million_tokens,
		       price.output_micro_usd_per_million_tokens
		FROM llm_logical_call call
		JOIN llm_budget_scope budget
		  ON budget.budget_scope_id = call.budget_scope_id
		JOIN llm_provider_attempt attempt
		  ON attempt.logical_call_pk = call.logical_call_pk
		JOIN llm_price_entry price
		  ON price.price_table_version = call.price_table_version
		 AND price.model_id = attempt.model_id
		WHERE call.logical_call_id = $1 AND attempt.attempt_id = $2`,
		req.GetLogicalCallId(), attemptID,
	).Scan(
		&budgetTokensReserved, &budgetCostReserved,
		&budgetTokensSettled, &budgetCostSettled,
		&callStatus, &callError, &callInput, &callOutput, &callCost,
		&attemptStatus, &attemptInput, &attemptOutput, &attemptCost,
		&attemptTokensHeld, &attemptCostHeld,
		&priceInputRate, &priceOutputRate,
	); err != nil {
		t.Fatalf("读取明确计费对账账本: %v", err)
	}
	if budgetTokensReserved != 0 ||
		budgetCostReserved != 0 ||
		budgetTokensSettled != 15 ||
		budgetCostSettled != 20 ||
		callStatus != "FAILED" ||
		callError != "RECONCILED_CHARGED" ||
		callInput != 10 ||
		callOutput != 5 ||
		callCost != 20 ||
		attemptStatus != "SUCCEEDED" ||
		attemptInput != 10 ||
		attemptOutput != 5 ||
		attemptCost != 20 ||
		attemptTokensHeld != attemptReservedTokens ||
		attemptCostHeld != attemptReservedCost ||
		logicalReservedTokens <= 0 ||
		logicalReservedCost <= 0 ||
		priceInputRate != originalInputRate ||
		priceOutputRate != originalOutputRate {
		t.Fatalf(
			"明确计费账本 = budget reserve %d/%d settled %d/%d call %q/%q %d/%d/%d attempt %q %d/%d/%d held %d/%d rates %d/%d",
			budgetTokensReserved, budgetCostReserved,
			budgetTokensSettled, budgetCostSettled,
			callStatus, callError, callInput, callOutput, callCost,
			attemptStatus, attemptInput, attemptOutput, attemptCost,
			attemptTokensHeld, attemptCostHeld, priceInputRate, priceOutputRate,
		)
	}

	var (
		auditCount       int
		auditOutcome     string
		auditFromStatus  string
		auditFromError   string
		auditEvidenceRef string
		auditReservedTok int64
		auditReservedCst int64
	)
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT count(*) OVER (),
		       outcome,
		       previous_attempt_status,
		       previous_error_code,
		       evidence_ref,
		       previous_reserved_tokens,
		       previous_reserved_cost_micro_usd
		FROM llm_attempt_reconciliation
		WHERE attempt_id = $1`,
		attemptID,
	).Scan(
		&auditCount, &auditOutcome, &auditFromStatus, &auditFromError,
		&auditEvidenceRef, &auditReservedTok, &auditReservedCst,
	); err != nil {
		t.Fatalf("读取对账审计依据: %v", err)
	}
	if auditCount != 1 ||
		auditOutcome != "CHARGED" ||
		auditFromStatus != "PENDING_RECONCILIATION" ||
		auditFromError != llmgw.ReasonProviderTimeout ||
		auditEvidenceRef != reconciliation.EvidenceRef ||
		auditReservedTok != attemptReservedTokens ||
		auditReservedCst != attemptReservedCost {
		t.Fatalf(
			"对账审计 = count %d outcome %q from %q/%q evidence %q reserved %d/%d",
			auditCount, auditOutcome, auditFromStatus, auditFromError,
			auditEvidenceRef, auditReservedTok, auditReservedCst,
		)
	}
	if _, err := fixture.pool.Exec(context.Background(), `
		UPDATE llm_attempt_reconciliation
		SET evidence_ref = 'rewritten-bill'
		WHERE attempt_id = $1`,
		attemptID,
	); err == nil {
		t.Fatal("已发布的对账审计不应允许原地改写")
	}

	repeated, err := reconciler.Reconcile(context.Background(), reconciliation)
	if err != nil {
		t.Fatalf("重复明确计费对账: %v", err)
	}
	if repeated != result {
		t.Fatalf("重复对账结果 = %+v, want %+v", repeated, result)
	}
	var (
		reconciliationCount int
		finalTokensSettled  int64
		finalCostSettled    int64
	)
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT (SELECT count(*) FROM llm_attempt_reconciliation WHERE attempt_id = $1),
		       budget.provider_tokens_settled,
		       budget.cost_settled_micro_usd
		FROM llm_logical_call call
		JOIN llm_budget_scope budget
		  ON budget.budget_scope_id = call.budget_scope_id
		WHERE call.logical_call_id = $2`,
		attemptID, req.GetLogicalCallId(),
	).Scan(&reconciliationCount, &finalTokensSettled, &finalCostSettled); err != nil {
		t.Fatalf("读取重复对账结果: %v", err)
	}
	if reconciliationCount != 1 ||
		finalTokensSettled != 15 ||
		finalCostSettled != 20 {
		t.Fatalf(
			"重复对账 = rows %d settled %d/%d",
			reconciliationCount, finalTokensSettled, finalCostSettled,
		)
	}

	conflicting := reconciliation
	conflicting.Outcome = llmgw.ReconciliationNoCharge
	conflicting.InputTokens = 0
	conflicting.OutputTokens = 0
	conflicting.EvidenceRef = "provider-bill:contradictory-free-line"
	_, err = reconciler.Reconcile(context.Background(), conflicting)
	assertStableError(t, err, codes.Aborted, llmgw.ReasonReconciliationConflict)
	var (
		conflictAuditCount int
		conflictSettledTok int64
		conflictSettledCst int64
	)
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT (SELECT count(*) FROM llm_attempt_reconciliation WHERE attempt_id = $1),
		       budget.provider_tokens_settled,
		       budget.cost_settled_micro_usd
		FROM llm_logical_call call
		JOIN llm_budget_scope budget
		  ON budget.budget_scope_id = call.budget_scope_id
		WHERE call.logical_call_id = $2`,
		attemptID, req.GetLogicalCallId(),
	).Scan(&conflictAuditCount, &conflictSettledTok, &conflictSettledCst); err != nil {
		t.Fatalf("读取冲突对账结果: %v", err)
	}
	if conflictAuditCount != 1 ||
		conflictSettledTok != 15 ||
		conflictSettledCst != 20 {
		t.Fatalf(
			"冲突对账改写了权威结果 = audit %d settled %d/%d",
			conflictAuditCount, conflictSettledTok, conflictSettledCst,
		)
	}
}

func TestReconciliationReleasesReservationOnceAfterConfirmedNoCharge(t *testing.T) {
	fixture := newLLMTestFixture(t)
	provider := &fakeProvider{err: errors.New("connection reset before response")}
	client := fixture.chatClient(t, provider)
	req := fixture.chatRequest("logical-reconcile-no-charge")

	_, err := client.Chat(trustedChatContext(fixture.principal), req)
	assertStableError(t, err, codes.Unavailable, llmgw.ReasonReconciliation)
	errorMetadata := kerrors.FromError(err).GetMetadata()

	reconciler := llmgw.NewReconciler(fixture.pool)
	reconciliation := llmgw.ReconciliationRequest{
		LogicalCallID: req.GetLogicalCallId(),
		AttemptID:     errorMetadata["attempt_id"],
		Outcome:       llmgw.ReconciliationNoCharge,
		EvidenceRef:   "provider-rejection:request-not-accepted",
	}
	result, err := reconciler.Reconcile(context.Background(), reconciliation)
	if err != nil {
		t.Fatalf("按明确未受理证据对账: %v", err)
	}
	if result.Outcome != llmgw.ReconciliationNoCharge ||
		result.LogicalStatus != "FAILED" ||
		result.AttemptStatus != "FAILED_NO_CHARGE" ||
		result.BudgetStatus != "ACTIVE" ||
		result.InputTokens != 0 ||
		result.OutputTokens != 0 ||
		result.CostMicroUSD != 0 {
		t.Fatalf("明确免计费对账结果 = %+v", result)
	}

	var (
		callStatus      string
		callError       string
		attemptStatus   string
		attemptError    string
		attemptInput    *int64
		attemptOutput   *int64
		attemptCost     *int64
		reservedTokens  int64
		reservedCost    int64
		settledTokens   int64
		settledCost     int64
		auditOutcome    string
		auditEvidence   string
		auditFromStatus string
		auditFromError  string
	)
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT call.status,
		       call.error_reason,
		       attempt.status,
		       attempt.error_code,
		       attempt.input_tokens,
		       attempt.output_tokens,
		       attempt.cost_micro_usd,
		       budget.provider_tokens_reserved,
		       budget.cost_reserved_micro_usd,
		       budget.provider_tokens_settled,
		       budget.cost_settled_micro_usd,
		       audit.outcome,
		       audit.evidence_ref,
		       audit.previous_attempt_status,
		       audit.previous_error_code
		FROM llm_logical_call call
		JOIN llm_budget_scope budget
		  ON budget.budget_scope_id = call.budget_scope_id
		JOIN llm_provider_attempt attempt
		  ON attempt.logical_call_pk = call.logical_call_pk
		JOIN llm_attempt_reconciliation audit
		  ON audit.attempt_id = attempt.attempt_id
		WHERE call.logical_call_id = $1`,
		req.GetLogicalCallId(),
	).Scan(
		&callStatus, &callError, &attemptStatus, &attemptError,
		&attemptInput, &attemptOutput, &attemptCost,
		&reservedTokens, &reservedCost, &settledTokens, &settledCost,
		&auditOutcome, &auditEvidence, &auditFromStatus, &auditFromError,
	); err != nil {
		t.Fatalf("读取明确免计费账本: %v", err)
	}
	if callStatus != "FAILED" ||
		callError != "RECONCILED_NO_CHARGE" ||
		attemptStatus != "FAILED_NO_CHARGE" ||
		attemptError != "RECONCILED_NO_CHARGE" ||
		attemptInput != nil ||
		attemptOutput != nil ||
		attemptCost != nil ||
		reservedTokens != 0 ||
		reservedCost != 0 ||
		settledTokens != 0 ||
		settledCost != 0 ||
		auditOutcome != "NO_CHARGE" ||
		auditEvidence != reconciliation.EvidenceRef ||
		auditFromStatus != "PENDING_RECONCILIATION" ||
		auditFromError != llmgw.ReasonReconciliation {
		t.Fatalf(
			"免计费账本 = call %q/%q attempt %q/%q usage %v/%v cost %v reserve %d/%d settled %d/%d audit %q/%q %q/%q",
			callStatus, callError, attemptStatus, attemptError,
			attemptInput, attemptOutput, attemptCost,
			reservedTokens, reservedCost, settledTokens, settledCost,
			auditOutcome, auditEvidence, auditFromStatus, auditFromError,
		)
	}

	repeated, err := reconciler.Reconcile(context.Background(), reconciliation)
	if err != nil {
		t.Fatalf("重复明确免计费对账: %v", err)
	}
	if repeated != result {
		t.Fatalf("重复免计费结果 = %+v, want %+v", repeated, result)
	}
	var auditCount int
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT count(*)
		FROM llm_attempt_reconciliation
		WHERE attempt_id = $1`,
		errorMetadata["attempt_id"],
	).Scan(&auditCount); err != nil {
		t.Fatalf("统计免计费对账审计: %v", err)
	}
	if auditCount != 1 {
		t.Fatalf("重复免计费对账新增审计 %d 行", auditCount)
	}
}

func TestReconciliationUsageAboveReservationLocksBudgetAndBlocksNextChat(t *testing.T) {
	fixture := newLLMTestFixture(t)
	provider := &fakeProvider{err: errors.New("usage unavailable after disconnect")}
	client := fixture.chatClient(t, provider)
	req := fixture.chatRequest("logical-reconcile-overrun")

	_, err := client.Chat(trustedChatContext(fixture.principal), req)
	assertStableError(t, err, codes.Unavailable, llmgw.ReasonReconciliation)
	errorMetadata := kerrors.FromError(err).GetMetadata()
	attemptID := errorMetadata["attempt_id"]

	reconciler := llmgw.NewReconciler(fixture.pool)
	result, err := reconciler.Reconcile(context.Background(), llmgw.ReconciliationRequest{
		LogicalCallID: req.GetLogicalCallId(),
		AttemptID:     attemptID,
		Outcome:       llmgw.ReconciliationCharged,
		InputTokens:   1_000_000,
		OutputTokens:  0,
		EvidenceRef:   "provider-bill:overrun-line",
	})
	if err != nil {
		t.Fatalf("按明确超预留账单对账: %v", err)
	}
	if result.LogicalStatus != "BUDGET_OVERRUN" ||
		result.AttemptStatus != "PENDING_RECONCILIATION" ||
		result.BudgetStatus != "BUDGET_OVERRUN" ||
		result.InputTokens != 1_000_000 ||
		result.CostMicroUSD != 1_000_000 {
		t.Fatalf("超预留对账结果 = %+v", result)
	}

	var (
		budgetStatus      string
		budgetReason      string
		reservedTokens    int64
		reservedCost      int64
		settledTokens     int64
		settledCost       int64
		callStatus        string
		callInput         int64
		callCost          int64
		attemptStatus     string
		attemptInput      int64
		attemptCost       int64
		auditOutcome      string
		attemptErrorCode  string
		originalReserve   int64
		originalReserveCs int64
	)
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT budget.budget_status,
		       budget.overrun_reason,
		       budget.provider_tokens_reserved,
		       budget.cost_reserved_micro_usd,
		       budget.provider_tokens_settled,
		       budget.cost_settled_micro_usd,
		       call.status,
		       call.actual_input_tokens,
		       call.actual_cost_micro_usd,
		       attempt.status,
		       attempt.input_tokens,
		       attempt.cost_micro_usd,
		       audit.outcome,
		       attempt.error_code,
		       attempt.reserved_tokens,
		       attempt.reserved_cost_micro_usd
		FROM llm_logical_call call
		JOIN llm_budget_scope budget
		  ON budget.budget_scope_id = call.budget_scope_id
		JOIN llm_provider_attempt attempt
		  ON attempt.logical_call_pk = call.logical_call_pk
		JOIN llm_attempt_reconciliation audit
		  ON audit.attempt_id = attempt.attempt_id
		WHERE call.logical_call_id = $1`,
		req.GetLogicalCallId(),
	).Scan(
		&budgetStatus, &budgetReason, &reservedTokens, &reservedCost,
		&settledTokens, &settledCost, &callStatus, &callInput, &callCost,
		&attemptStatus, &attemptInput, &attemptCost, &auditOutcome,
		&attemptErrorCode, &originalReserve, &originalReserveCs,
	); err != nil {
		t.Fatalf("读取超预留对账账本: %v", err)
	}
	if budgetStatus != "BUDGET_OVERRUN" ||
		budgetReason != "RECONCILIATION_EXCEEDS_RESERVATION" ||
		reservedTokens <= 0 ||
		reservedCost <= 0 ||
		settledTokens != 0 ||
		settledCost != 0 ||
		callStatus != "BUDGET_OVERRUN" ||
		callInput != 1_000_000 ||
		callCost != 1_000_000 ||
		attemptStatus != "PENDING_RECONCILIATION" ||
		attemptInput != 1_000_000 ||
		attemptCost != 1_000_000 ||
		auditOutcome != "CHARGED_OVERRUN" ||
		attemptErrorCode != llmgw.ReasonBudgetOverrun ||
		originalReserve <= 0 ||
		originalReserveCs <= 0 {
		t.Fatalf(
			"超预留账本 = budget %q/%q reserve %d/%d settled %d/%d call %q %d/%d attempt %q %d/%d audit %q/%q held %d/%d",
			budgetStatus, budgetReason, reservedTokens, reservedCost,
			settledTokens, settledCost, callStatus, callInput, callCost,
			attemptStatus, attemptInput, attemptCost, auditOutcome,
			attemptErrorCode, originalReserve, originalReserveCs,
		)
	}

	overrunRequest := llmgw.ReconciliationRequest{
		LogicalCallID: req.GetLogicalCallId(),
		AttemptID:     attemptID,
		Outcome:       llmgw.ReconciliationCharged,
		InputTokens:   1_000_000,
		OutputTokens:  0,
		EvidenceRef:   "provider-bill:overrun-line",
	}
	repeated, err := reconciler.Reconcile(context.Background(), overrunRequest)
	if err != nil {
		t.Fatalf("重复超预留对账: %v", err)
	}
	if repeated != result {
		t.Fatalf("重复超预留结果 = %+v, want %+v", repeated, result)
	}
	var overrunAuditCount int
	if err := fixture.pool.QueryRow(context.Background(), `
		SELECT count(*)
		FROM llm_attempt_reconciliation
		WHERE attempt_id = $1`,
		attemptID,
	).Scan(&overrunAuditCount); err != nil {
		t.Fatalf("统计重复超预留审计: %v", err)
	}
	if overrunAuditCount != 1 {
		t.Fatalf("重复超预留对账新增审计 %d 行", overrunAuditCount)
	}

	_, nextErr := client.Chat(
		trustedChatContext(fixture.principal),
		fixture.chatRequest("logical-after-reconciled-overrun"),
	)
	assertStableError(t, nextErr, codes.ResourceExhausted, llmgw.ReasonBudgetOverrun)
	if calls := provider.callsSnapshot(); len(calls) != 1 {
		t.Fatalf("超限对账后新调用 provider calls = %d, want 1", len(calls))
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
