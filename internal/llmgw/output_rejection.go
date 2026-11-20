package llmgw

import (
	"context"

	llmgwdb "github.com/choucisan/ai-gaokao-counselor/internal/llmgw/dbgen"
	"github.com/jackc/pgx/v5"
	"google.golang.org/grpc/codes"
)

func (s *Service) settleSchemaFailure(
	ctx context.Context,
	prepared *preparedCall,
	result providerResult,
	actualCostMicroUSD int64,
	actualTokens int64,
) error {
	return s.settleRejectedProviderResult(
		ctx, prepared, result, actualCostMicroUSD, actualTokens,
		ReasonSchemaValidationFailed,
		"供应商输出不符合已注册 response schema",
	)
}

func (s *Service) settleSensitiveOutputFailure(
	ctx context.Context,
	prepared *preparedCall,
	result providerResult,
	actualCostMicroUSD int64,
	actualTokens int64,
) error {
	return s.settleRejectedProviderResult(
		ctx, prepared, result, actualCostMicroUSD, actualTokens,
		ReasonSensitiveDataNotAllowed,
		"供应商输出包含不得返回或持久化的敏感原文",
	)
}

func (s *Service) settleRejectedProviderResult(
	ctx context.Context,
	prepared *preparedCall,
	result providerResult,
	actualCostMicroUSD int64,
	actualTokens int64,
	errorReason string,
	errorMessage string,
) error {
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "启动被拒输出结算事务失败")
	}
	defer tx.Rollback(ctx)
	queries := s.queries.WithTx(tx)

	currentStatus, err := queries.LockLogicalCallStatus(
		ctx,
		databaseUUID(prepared.logicalCallPK),
	)
	if err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "读取被拒输出调用状态失败")
	}
	if currentStatus != "IN_PROGRESS" {
		return newStableError(codes.Aborted, 409, ReasonCallInProgress, "逻辑调用已完成或正在结算")
	}

	finishedAt := s.now()
	affected, err := queries.SettleBudget(ctx, llmgwdb.SettleBudgetParams{
		ProviderTokensReserved: prepared.reservedTokens,
		CostReservedMicroUsd:   prepared.reservedCost,
		ProviderTokensSettled:  actualTokens,
		CostSettledMicroUsd:    actualCostMicroUSD,
		UpdatedAt:              databaseTimestamp(finishedAt),
		Column6:                databaseUUID(prepared.logicalCallPK),
	})
	if err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "释放被拒输出预算差额失败")
	}
	if affected != 1 {
		return newStableError(codes.Internal, 500, "INTERNAL", "预算预留状态不一致")
	}

	affected, err = queries.MarkAttemptRejectedSucceeded(
		ctx,
		llmgwdb.MarkAttemptRejectedSucceededParams{
			InputTokens:  databaseInt8(result.InputTokens),
			OutputTokens: databaseInt8(result.OutputTokens),
			CostMicroUsd: databaseInt8(actualCostMicroUSD),
			Column4:      nullableText(result.ProviderRequestID),
			ErrorCode:    databaseText(errorReason),
			FinishedAt:   databaseTimestamp(finishedAt),
			Column7:      databaseUUID(prepared.attemptID),
		},
	)
	if err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "结算被拒输出 attempt 失败")
	}
	if affected != 1 {
		return newStableError(codes.Aborted, 409, ReasonCallInProgress, "attempt 状态不允许被拒输出结算")
	}

	affected, err = queries.RejectLogicalCall(
		ctx,
		llmgwdb.RejectLogicalCallParams{
			ActualInputTokens:  databaseInt8(result.InputTokens),
			ActualOutputTokens: databaseInt8(result.OutputTokens),
			ActualCostMicroUsd: databaseInt8(actualCostMicroUSD),
			ErrorReason:        databaseText(errorReason),
			CompletedAt:        databaseTimestamp(finishedAt),
			Column6:            databaseUUID(prepared.logicalCallPK),
		},
	)
	if err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "提交被拒输出逻辑调用失败")
	}
	if affected != 1 {
		return newStableError(codes.Aborted, 409, ReasonCallInProgress, "逻辑调用状态不允许被拒输出结算")
	}
	if err := tx.Commit(ctx); err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "提交被拒输出结算失败")
	}
	s.insertFailedProjection(ctx, prepared, result, actualCostMicroUSD, errorReason)
	return newStableError(
		codes.FailedPrecondition, 412, errorReason, errorMessage,
	)
}
