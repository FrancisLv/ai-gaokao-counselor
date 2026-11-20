package llmgw

import (
	"context"
	"encoding/json"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/protobuf/types/known/structpb"

	llmgwv1 "github.com/choucisan/ai-gaokao-counselor/api/llmgw/v1"
	llmgwdb "github.com/choucisan/ai-gaokao-counselor/internal/llmgw/dbgen"
)

func (s *Service) resolveExistingCall(
	ctx context.Context,
	req governedRequest,
	requestHash string,
) (*preparedCall, *llmgwv1.ChatResponse, error) {
	row, err := s.queries.GetLogicalCallForReplay(
		ctx,
		llmgwdb.GetLogicalCallForReplayParams{
			PrincipalID:   req.PrincipalID,
			Column2:       databaseUUID(req.SessionID),
			LogicalCallID: req.LogicalCallID,
		},
	)
	if err != nil {
		return nil, nil, newStableError(codes.Internal, 500, "INTERNAL", "读取逻辑调用幂等状态失败")
	}
	callPK := row.LogicalCallPk
	ledgerID := row.LedgerEntryID
	status := row.Status
	attemptID := row.AttemptID
	errorReason := textPointer(row.ErrorReason)
	promptKey := textPointer(row.PromptTemplateKey)
	promptVersion := textPointer(row.PromptTemplateVersion)
	promptDigest := textPointer(row.PromptTemplateSha256)
	schemaKey := textPointer(row.ResponseSchemaKey)
	schemaVersion := textPointer(row.ResponseSchemaVersion)
	schemaDigest := textPointer(row.ResponseSchemaSha256)
	actualIn := int8Pointer(row.ActualInputTokens)
	actualOut := int8Pointer(row.ActualOutputTokens)
	actualCost := int8Pointer(row.ActualCostMicroUsd)
	cacheKeyHash := textPointer(row.CacheKeyHash)
	if row.RequestHash != requestHash {
		return nil, nil, newStableError(codes.Aborted, 409, ReasonIdempotencyConflict, "logical_call_id 已绑定不同请求")
	}
	if status == "RESERVED" || status == "IN_PROGRESS" {
		return nil, nil, newStableError(codes.Aborted, 409, ReasonCallInProgress, "逻辑调用正在执行或等待对账")
	}
	if status == "PARTIAL_UNKNOWN" {
		reason := ReasonReconciliation
		message := "逻辑调用仍有供应商费用待对账"
		if errorReason != nil && *errorReason == ReasonProviderTimeout {
			reason = ReasonProviderTimeout
			message = "供应商请求超时且费用仍待对账"
		}
		return nil, nil, newStableErrorWithMetadata(
			codes.Unavailable, 503, reason, message,
			reconciliationMetadata(req.LogicalCallID, attemptID, ledgerID),
		)
	}
	if status == "FAILED" && errorReason != nil &&
		*errorReason == ReasonSchemaValidationFailed {
		return nil, nil, newStableError(
			codes.FailedPrecondition, 412, ReasonSchemaValidationFailed,
			"供应商输出不符合已注册 response schema",
		)
	}
	if status == "FAILED" && errorReason != nil &&
		*errorReason == ReasonSensitiveDataNotAllowed {
		return nil, nil, newStableError(
			codes.FailedPrecondition, 412, ReasonSensitiveDataNotAllowed,
			"供应商输出包含不得返回或持久化的敏感原文",
		)
	}
	if status == "FAILED" || status == "BUDGET_OVERRUN" {
		return nil, nil, withStableErrorMetadata(
			stableStoredFailure(status, errorReason),
			reconciliationMetadata(req.LogicalCallID, attemptID, ledgerID),
		)
	}
	if status != "SUCCEEDED" || actualIn == nil || actualOut == nil || actualCost == nil {
		return nil, nil, newStableError(codes.Internal, 500, "INTERNAL", "逻辑调用状态缺少可重放结果")
	}
	var result providerResult
	if err := json.Unmarshal(row.ResponsePayload, &result); err != nil {
		return nil, nil, newStableError(codes.Internal, 500, "INTERNAL", "读取已结算结果失败")
	}
	prepared := &preparedCall{
		logicalCallPK: callPK,
		ledgerEntryID: ledgerID,
		principalID:   req.PrincipalID,
		sessionID:     req.SessionID,
		runID:         req.RunID,
		role:          req.Role,
		releaseID:     req.ReleaseID,
		logicalCallID: req.LogicalCallID,
		requestHash:   requestHash,
		route: routeConfig{
			version:      row.RouteVersion,
			priceVersion: row.PriceTableVersion,
		},
		selected:       modelPrice{modelID: row.ModelID},
		reservedTokens: row.ReservedProviderTokens.Int64,
		reservedCost:   row.ReservedCostMicroUsd,
		cacheHit:       row.CacheHit,
		cacheKeyHash:   valueOrEmpty(cacheKeyHash),
	}
	if promptKey != nil && promptVersion != nil {
		prepared.prompt.key = *promptKey
		prepared.prompt.version = *promptVersion
		if promptDigest != nil {
			prepared.prompt.digest = *promptDigest
		}
	}
	if schemaKey != nil && schemaVersion != nil {
		prepared.schema = &responseSchema{
			key:     *schemaKey,
			version: *schemaVersion,
		}
		if schemaDigest != nil {
			prepared.schema.digest = *schemaDigest
		}
	}
	response, err := s.replayResponse(ctx, prepared, result, *actualCost)
	if err != nil {
		return nil, nil, err
	}
	return nil, response, nil
}

func (s *Service) replayResponse(
	ctx context.Context,
	prepared *preparedCall,
	result providerResult,
	actualCostMicroUSD int64,
) (*llmgwv1.ChatResponse, error) {
	attempts, err := s.loadAttemptSummaries(ctx, prepared.logicalCallPK)
	if err != nil {
		return nil, err
	}
	return buildResponse(prepared, result, actualCostMicroUSD, attempts), nil
}

func (s *Service) loadAttemptSummaries(
	ctx context.Context,
	logicalCallPK string,
) ([]*llmgwv1.AttemptSummary, error) {
	rows, err := s.queries.ListAttemptSummaries(ctx, databaseUUID(logicalCallPK))
	if err != nil {
		return nil, newStableError(codes.Internal, 500, "INTERNAL", "读取供应商 attempt 失败")
	}

	var attempts []*llmgwv1.AttemptSummary
	for _, row := range rows {
		attempt := &llmgwv1.AttemptSummary{
			AttemptId:         row.AttemptID,
			ModelId:           row.ModelID,
			Provider:          row.Provider,
			Status:            row.Status,
			ProviderRequestId: row.ProviderRequestID.String,
			ErrorCode:         row.ErrorCode.String,
		}
		inputTokens := int8Pointer(row.InputTokens)
		outputTokens := int8Pointer(row.OutputTokens)
		if inputTokens != nil || outputTokens != nil {
			attempt.Usage = &llmgwv1.Usage{}
			if inputTokens != nil {
				attempt.Usage.InputTokens = *inputTokens
			}
			if outputTokens != nil {
				attempt.Usage.OutputTokens = *outputTokens
			}
		}
		if row.CostMicroUsd.Valid {
			attempt.CostMicroUsd = row.CostMicroUsd.Int64
		}
		if row.StartedAt.Valid && row.FinishedAt.Valid {
			attempt.LatencyMs = row.FinishedAt.Time.Sub(row.StartedAt.Time).Milliseconds()
		}
		attempts = append(attempts, attempt)
	}
	return attempts, nil
}

func (s *Service) insertProjection(
	ctx context.Context,
	prepared *preparedCall,
	result providerResult,
	actualCostMicroUSD int64,
) {
	_ = s.queries.InsertSuccessProjection(
		ctx,
		llmgwdb.InsertSuccessProjectionParams{
			Column1:           nullableText(prepared.traceID),
			Column2:           databaseUUID(prepared.sessionID),
			Node:              databaseText(prepared.role),
			Model:             databaseText(prepared.selected.modelID),
			PromptHash:        databaseText(prepared.requestHash),
			InTokens:          databaseInt4(int32(result.InputTokens)),
			OutTokens:         databaseInt4(int32(result.OutputTokens)),
			Cost:              databaseNumericMicroUSD(actualCostMicroUSD),
			LatencyMs:         databaseInt4(int32(time.Since(prepared.startedAt).Milliseconds())),
			LogicalCallID:     databaseText(prepared.logicalCallID),
			Column11:          databaseUUID(prepared.ledgerEntryID),
			Column12:          nullableText(prepared.attemptID),
			RouteVersion:      databaseText(prepared.route.version),
			PriceTableVersion: databaseText(prepared.route.priceVersion),
			CostMicroUsd:      databaseInt8(actualCostMicroUSD),
			SettlementStatus:  databaseText(settlementStatus(prepared)),
			CacheHit:          prepared.cacheHit,
			CacheKeyHash:      databaseText(prepared.cacheKeyHash),
		},
	)
}

func (s *Service) insertFailedProjection(
	ctx context.Context,
	prepared *preparedCall,
	result providerResult,
	actualCostMicroUSD int64,
	errorReason string,
) {
	_ = s.queries.InsertFailedProjection(
		ctx,
		llmgwdb.InsertFailedProjectionParams{
			Column1:           nullableText(prepared.traceID),
			Column2:           databaseUUID(prepared.sessionID),
			Node:              databaseText(prepared.role),
			Model:             databaseText(prepared.selected.modelID),
			PromptHash:        databaseText(prepared.requestHash),
			InTokens:          databaseInt4(int32(result.InputTokens)),
			OutTokens:         databaseInt4(int32(result.OutputTokens)),
			Cost:              databaseNumericMicroUSD(actualCostMicroUSD),
			LatencyMs:         databaseInt4(int32(time.Since(prepared.startedAt).Milliseconds())),
			ErrorMessage:      databaseText(errorReason),
			LogicalCallID:     databaseText(prepared.logicalCallID),
			Column12:          databaseUUID(prepared.ledgerEntryID),
			Column13:          databaseUUID(prepared.attemptID),
			RouteVersion:      databaseText(prepared.route.version),
			PriceTableVersion: databaseText(prepared.route.priceVersion),
			CostMicroUsd:      databaseInt8(actualCostMicroUSD),
		},
	)
}

func buildResponse(
	prepared *preparedCall,
	result providerResult,
	actualCostMicroUSD int64,
	attempts []*llmgwv1.AttemptSummary,
) *llmgwv1.ChatResponse {
	modelID := prepared.selected.modelID
	for _, attempt := range attempts {
		if attempt.GetStatus() == "SUCCEEDED" {
			modelID = attempt.GetModelId()
			break
		}
	}
	var structuredOutput *structpb.Struct
	if len(result.StructuredOutput) > 0 {
		structuredOutput, _ = structpb.NewStruct(result.StructuredOutput)
	}
	return &llmgwv1.ChatResponse{
		Content:   result.Content,
		Model:     modelID,
		InTokens:  int32(result.InputTokens),
		OutTokens: int32(result.OutputTokens),
		Cost:      float64(actualCostMicroUSD) / 1_000_000,
		Usage: &llmgwv1.Usage{
			InputTokens:  result.InputTokens,
			OutputTokens: result.OutputTokens,
		},
		CostDetail: &llmgwv1.Cost{
			CurrencyUnit:     "USD_MICRO",
			ReservedMicroUsd: reservedCostForResponse(prepared),
			ActualMicroUsd:   actualCostMicroUSD,
			SettlementStatus: settlementStatus(prepared),
		},
		Attempts:              attempts,
		LedgerEntryId:         prepared.ledgerEntryID,
		RouteVersion:          prepared.route.version,
		PriceTableVersion:     prepared.route.priceVersion,
		LogicalCallId:         prepared.logicalCallID,
		CacheHit:              prepared.cacheHit,
		FinishReason:          result.FinishReason,
		ReleaseId:             prepared.releaseID,
		StructuredOutput:      structuredOutput,
		PromptTemplateKey:     prepared.prompt.key,
		PromptTemplateVersion: prepared.prompt.version,
		ResponseSchemaKey:     schemaKey(prepared.schema),
		ResponseSchemaVersion: schemaVersion(prepared.schema),
	}
}

func settlementStatus(prepared *preparedCall) string {
	if prepared.cacheHit {
		return "CACHE_HIT"
	}
	return "SETTLED"
}

func reservedCostForResponse(prepared *preparedCall) int64 {
	if prepared.cacheHit {
		return 0
	}
	return prepared.reservedCost
}
