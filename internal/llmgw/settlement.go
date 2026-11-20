package llmgw

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"time"

	llmgwv1 "github.com/choucisan/ai-gaokao-counselor/api/llmgw/v1"
	llmgwdb "github.com/choucisan/ai-gaokao-counselor/internal/llmgw/dbgen"
	"github.com/jackc/pgx/v5"
	"google.golang.org/grpc/codes"
)

func (s *Service) settleCall(
	ctx context.Context,
	prepared *preparedCall,
	result providerResult,
	actualCostMicroUSD int64,
	actualTokens int64,
) (*llmgwv1.ChatResponse, error) {
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return nil, newStableError(codes.Internal, 500, "INTERNAL", "启动结算事务失败")
	}
	defer tx.Rollback(ctx)
	queries := s.queries.WithTx(tx)

	currentStatus, err := queries.LockLogicalCallStatus(
		ctx,
		databaseUUID(prepared.logicalCallPK),
	)
	if err != nil {
		return nil, newStableError(codes.Internal, 500, "INTERNAL", "读取逻辑调用结算状态失败")
	}
	if currentStatus != "IN_PROGRESS" {
		return nil, newStableError(codes.Aborted, 409, ReasonCallInProgress, "逻辑调用已完成或正在结算")
	}

	finishedAt := s.now()
	payload, err := json.Marshal(result)
	if err != nil {
		return nil, newStableError(codes.Internal, 500, "INTERNAL", "序列化调用结果失败")
	}
	affected, err := queries.SettleBudget(ctx, llmgwdb.SettleBudgetParams{
		ProviderTokensReserved: prepared.reservedTokens,
		CostReservedMicroUsd:   prepared.reservedCost,
		ProviderTokensSettled:  actualTokens,
		CostSettledMicroUsd:    actualCostMicroUSD,
		UpdatedAt:              databaseTimestamp(finishedAt),
		Column6:                databaseUUID(prepared.logicalCallPK),
	})
	if err != nil {
		return nil, newStableError(codes.Internal, 500, "INTERNAL", "释放预算差额失败")
	}
	if affected != 1 {
		return nil, newStableError(codes.Internal, 500, "INTERNAL", "预算预留状态不一致")
	}

	affected, err = queries.MarkAttemptSucceeded(
		ctx,
		llmgwdb.MarkAttemptSucceededParams{
			InputTokens:  databaseInt8(result.InputTokens),
			OutputTokens: databaseInt8(result.OutputTokens),
			CostMicroUsd: databaseInt8(actualCostMicroUSD),
			Column4:      nullableText(result.ProviderRequestID),
			FinishedAt:   databaseTimestamp(finishedAt),
			Column6:      databaseUUID(prepared.attemptID),
		},
	)
	if err != nil {
		return nil, newStableError(codes.Internal, 500, "INTERNAL", "结算供应商 attempt 失败")
	}
	if affected != 1 {
		return nil, newStableError(codes.Aborted, 409, ReasonCallInProgress, "attempt 状态不允许成功结算")
	}
	affected, err = queries.CompleteLogicalCall(
		ctx,
		llmgwdb.CompleteLogicalCallParams{
			ActualInputTokens:  databaseInt8(result.InputTokens),
			ActualOutputTokens: databaseInt8(result.OutputTokens),
			ActualCostMicroUsd: databaseInt8(actualCostMicroUSD),
			ResponsePayload:    payload,
			CompletedAt:        databaseTimestamp(finishedAt),
			Column6:            databaseUUID(prepared.logicalCallPK),
		},
	)
	if err != nil {
		return nil, newStableError(codes.Internal, 500, "INTERNAL", "提交逻辑调用结果失败")
	}
	if affected != 1 {
		return nil, newStableError(codes.Aborted, 409, ReasonCallInProgress, "逻辑调用状态不允许成功结算")
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, newStableError(codes.Internal, 500, "INTERNAL", "提交预算结算失败")
	}
	s.storeResultCache(prepared, result)
	attempts, err := s.loadAttemptSummaries(ctx, prepared.logicalCallPK)
	if err != nil {
		return nil, err
	}
	return buildResponse(prepared, result, actualCostMicroUSD, attempts), nil
}

func (s *Service) releaseUnchargedCall(ctx context.Context, prepared *preparedCall) error {
	return s.releaseKnownUnchargedFailure(
		ctx,
		prepared,
		"",
		"PROVIDER_NOT_CHARGED",
		newStableError(codes.Unavailable, 503, ReasonProviderUnavailable, "供应商明确未受理且未计费"),
	)
}

func (s *Service) releaseProviderFailure(
	ctx context.Context,
	prepared *preparedCall,
	failure *ProviderFailure,
) error {
	return s.releaseKnownUnchargedFailure(
		ctx,
		prepared,
		failure.ProviderRequestID,
		failure.Code,
		stableProviderFailure(failure),
	)
}

func (s *Service) releaseKnownUnchargedFailure(
	ctx context.Context,
	prepared *preparedCall,
	providerRequestID string,
	attemptErrorCode string,
	returnError error,
) error {
	persistenceCtx, cancel := persistenceContext(ctx)
	defer cancel()
	tx, err := s.pool.BeginTx(persistenceCtx, pgx.TxOptions{})
	if err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "启动免计费释放事务失败")
	}
	defer tx.Rollback(persistenceCtx)
	queries := s.queries.WithTx(tx)

	currentStatus, err := queries.LockLogicalCallStatus(
		persistenceCtx,
		databaseUUID(prepared.logicalCallPK),
	)
	if err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "读取免计费调用状态失败")
	}
	if currentStatus != "IN_PROGRESS" {
		return newStableError(codes.Aborted, 409, ReasonCallInProgress, "逻辑调用已完成或正在结算")
	}

	finishedAt := s.now()
	affected, err := queries.ReleaseBudgetReservation(
		persistenceCtx,
		llmgwdb.ReleaseBudgetReservationParams{
			ProviderTokensReserved: prepared.reservedTokens,
			CostReservedMicroUsd:   prepared.reservedCost,
			UpdatedAt:              databaseTimestamp(finishedAt),
			Column4:                databaseUUID(prepared.logicalCallPK),
		},
	)
	if err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "释放免计费预算失败")
	}
	if affected != 1 {
		return newStableError(codes.Internal, 500, "INTERNAL", "预算预留状态不一致")
	}
	affected, err = queries.MarkAttemptNoCharge(
		persistenceCtx,
		llmgwdb.MarkAttemptNoChargeParams{
			Column1:    nullableText(providerRequestID),
			ErrorCode:  databaseText(attemptErrorCode),
			FinishedAt: databaseTimestamp(finishedAt),
			Column4:    databaseUUID(prepared.attemptID),
		},
	)
	if err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "记录免计费 attempt 失败")
	}
	if affected != 1 {
		return newStableError(codes.Aborted, 409, ReasonCallInProgress, "attempt 状态不允许免计费结算")
	}
	affected, err = queries.FailLogicalCallWithoutCharge(
		persistenceCtx,
		llmgwdb.FailLogicalCallWithoutChargeParams{
			ErrorReason: databaseText(attemptErrorCode),
			CompletedAt: databaseTimestamp(finishedAt),
			Column3:     databaseUUID(prepared.logicalCallPK),
		},
	)
	if err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "提交免计费逻辑调用失败")
	}
	if affected != 1 {
		return newStableError(codes.Aborted, 409, ReasonCallInProgress, "逻辑调用状态不允许免计费结算")
	}
	if err := tx.Commit(persistenceCtx); err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "提交免计费释放失败")
	}
	projectionCtx, projectionCancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer projectionCancel()
	s.insertFailedProjection(
		projectionCtx,
		prepared,
		providerResult{},
		0,
		attemptErrorCode,
	)
	return returnError
}

func stableProviderFailure(failure *ProviderFailure) error {
	switch failure.Code {
	case ReasonProviderRateLimited:
		return newStableError(codes.ResourceExhausted, 429, ReasonProviderRateLimited, "供应商限流且确认未计费")
	case ReasonProviderBadRequest:
		return newStableError(codes.InvalidArgument, 400, ReasonProviderBadRequest, "供应商拒绝请求参数且确认未计费")
	case ReasonProviderAuthFailed:
		return newStableError(codes.PermissionDenied, 403, ReasonProviderAuthFailed, "供应商认证失败且确认未计费")
	case ReasonSchemaUnsupported:
		return newStableError(codes.FailedPrecondition, 412, ReasonSchemaUnsupported, "供应商不支持请求的输出 schema")
	default:
		return newStableError(codes.Unavailable, 503, ReasonProviderUnavailable, "供应商请求失败且确认未计费")
	}
}

func stableStoredFailure(status string, errorReason *string) error {
	if errorReason == nil || strings.TrimSpace(*errorReason) == "" {
		return newStableError(codes.Internal, 500, "INTERNAL", "逻辑调用失败但缺少稳定原因")
	}
	reason := *errorReason
	switch reason {
	case ReasonProviderRateLimited:
		return newStableError(codes.ResourceExhausted, 429, reason, "供应商限流且确认未计费")
	case ReasonProviderBadRequest:
		return newStableError(codes.InvalidArgument, 400, reason, "供应商拒绝请求参数且确认未计费")
	case ReasonProviderAuthFailed:
		return newStableError(codes.PermissionDenied, 403, reason, "供应商认证失败且确认未计费")
	case ReasonSchemaUnsupported:
		return newStableError(codes.FailedPrecondition, 412, reason, "供应商不支持请求的输出 schema")
	case ReasonBudgetOverrun:
		return newStableError(codes.ResourceExhausted, 429, reason, "调用实际用量超过预算预留")
	case ReasonProviderUnavailable, "PROVIDER_NOT_CHARGED":
		return newStableError(codes.Unavailable, 503, reason, "供应商请求失败且确认未计费")
	case ReasonReconciliation:
		return newStableError(codes.Unavailable, 503, reason, "供应商费用仍待对账")
	default:
		if status == "BUDGET_OVERRUN" {
			return newStableError(codes.ResourceExhausted, 429, ReasonBudgetOverrun, "调用实际用量超过预算预留")
		}
		return newStableError(codes.FailedPrecondition, 412, reason, "逻辑调用已稳定失败")
	}
}

func (s *Service) markBudgetOverrun(
	ctx context.Context,
	prepared *preparedCall,
	result providerResult,
	actualCostMicroUSD int64,
) error {
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "启动预算超限事务失败")
	}
	defer tx.Rollback(ctx)
	queries := s.queries.WithTx(tx)

	currentStatus, err := queries.LockLogicalCallStatus(
		ctx,
		databaseUUID(prepared.logicalCallPK),
	)
	if err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "读取预算超限调用状态失败")
	}
	if currentStatus != "IN_PROGRESS" {
		return newStableError(codes.Aborted, 409, ReasonCallInProgress, "逻辑调用已完成或正在结算")
	}

	finishedAt := s.now()
	affected, err := queries.MarkBudgetScopeOverrun(
		ctx,
		llmgwdb.MarkBudgetScopeOverrunParams{
			UpdatedAt:     databaseTimestamp(finishedAt),
			OverrunReason: databaseText("ACTUAL_USAGE_EXCEEDS_RESERVATION"),
			Column3:       databaseUUID(prepared.logicalCallPK),
		},
	)
	if err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "封锁预算超限作用域失败")
	}
	if affected != 1 {
		return newStableError(codes.Internal, 500, "INTERNAL", "预算作用域状态不一致")
	}

	affected, err = queries.MarkAttemptBudgetOverrun(
		ctx,
		llmgwdb.MarkAttemptBudgetOverrunParams{
			InputTokens:  databaseInt8(result.InputTokens),
			OutputTokens: databaseInt8(result.OutputTokens),
			CostMicroUsd: databaseInt8(actualCostMicroUSD),
			Column4:      nullableText(result.ProviderRequestID),
			ErrorCode:    databaseText(ReasonBudgetOverrun),
			FinishedAt:   databaseTimestamp(finishedAt),
			Column7:      databaseUUID(prepared.attemptID),
		},
	)
	if err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "记录预算超限 attempt 失败")
	}
	if affected != 1 {
		return newStableError(codes.Aborted, 409, ReasonCallInProgress, "attempt 状态不允许预算超限结算")
	}

	affected, err = queries.MarkLogicalCallBudgetOverrun(
		ctx,
		llmgwdb.MarkLogicalCallBudgetOverrunParams{
			ActualInputTokens:  databaseInt8(result.InputTokens),
			ActualOutputTokens: databaseInt8(result.OutputTokens),
			ActualCostMicroUsd: databaseInt8(actualCostMicroUSD),
			ErrorReason:        databaseText(ReasonBudgetOverrun),
			UpdatedAt:          databaseTimestamp(finishedAt),
			Column6:            databaseUUID(prepared.logicalCallPK),
		},
	)
	if err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "记录预算超限逻辑调用失败")
	}
	if affected != 1 {
		return newStableError(codes.Aborted, 409, ReasonCallInProgress, "逻辑调用状态不允许预算超限结算")
	}
	if err := tx.Commit(ctx); err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "提交预算超限封锁失败")
	}
	return newStableError(
		codes.ResourceExhausted, 429, ReasonBudgetOverrun,
		"调用实际用量超过预算预留，已停止该作用域的后续模型调用",
	)
}

func (s *Service) markProviderUnknown(ctx context.Context, prepared *preparedCall, cause error) error {
	errorCode := ReasonReconciliation
	if errors.Is(cause, context.DeadlineExceeded) {
		errorCode = ReasonProviderTimeout
	}
	return s.markProviderUnknownWithReason(ctx, prepared, cause, errorCode)
}

func (s *Service) markProviderUnknownWithReason(
	ctx context.Context,
	prepared *preparedCall,
	_ error,
	errorCode string,
) error {
	persistenceCtx, cancel := persistenceContext(ctx)
	defer cancel()
	tx, err := s.pool.BeginTx(persistenceCtx, pgx.TxOptions{})
	if err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "记录待对账状态失败")
	}
	defer tx.Rollback(persistenceCtx)
	queries := s.queries.WithTx(tx)
	finishedAt := s.now()
	affected, err := queries.MarkAttemptPendingReconciliation(
		persistenceCtx,
		llmgwdb.MarkAttemptPendingReconciliationParams{
			ErrorCode:  databaseText(errorCode),
			FinishedAt: databaseTimestamp(finishedAt),
			Column3:    databaseUUID(prepared.attemptID),
		},
	)
	if err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "更新未知计费 attempt 失败")
	}
	if affected != 1 {
		return newStableError(codes.Aborted, 409, ReasonCallInProgress, "attempt 状态不允许进入待对账")
	}
	affected, err = queries.MarkLogicalCallPartialUnknown(
		persistenceCtx,
		llmgwdb.MarkLogicalCallPartialUnknownParams{
			ErrorReason: databaseText(errorCode),
			UpdatedAt:   databaseTimestamp(finishedAt),
			Column3:     databaseUUID(prepared.logicalCallPK),
		},
	)
	if err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "更新待对账逻辑调用失败")
	}
	if affected != 1 {
		return newStableError(codes.Aborted, 409, ReasonCallInProgress, "逻辑调用状态不允许进入待对账")
	}
	if err := tx.Commit(persistenceCtx); err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "提交待对账状态失败")
	}
	message := "供应商请求可能已计费，预算预留保持待对账"
	if errorCode == ReasonProviderTimeout {
		message = "供应商请求超时且费用未知，预算预留保持待对账"
	} else if errorCode == ReasonStreamInterrupted {
		message = "客户端断流且供应商费用未知，预算预留保持待对账"
	}
	return newStableErrorWithMetadata(
		codes.Unavailable, 503, errorCode, message,
		reconciliationMetadata(prepared.logicalCallID, prepared.attemptID, prepared.ledgerEntryID),
	)
}

func persistenceContext(ctx context.Context) (context.Context, context.CancelFunc) {
	if ctx.Err() == nil {
		return ctx, func() {}
	}
	return context.WithTimeout(context.Background(), 5*time.Second)
}
