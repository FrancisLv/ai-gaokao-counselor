package llmgw

import (
	"context"
	"time"

	llmgwv1 "github.com/choucisan/ai-gaokao-counselor/api/llmgw/v1"
	llmgwdb "github.com/choucisan/ai-gaokao-counselor/internal/llmgw/dbgen"
	"github.com/jackc/pgx/v5"
	"google.golang.org/grpc/codes"
)

func (s *Service) reserveCall(
	ctx context.Context,
	req governedRequest,
	route routeConfig,
	prompt promptTemplate,
	schema *responseSchema,
	requestHash string,
	inputBound int64,
) (*preparedCall, *llmgwv1.ChatResponse, error) {
	outputBound := int64(route.maxOutput)
	attemptTokens, err := checkedAdd(inputBound, outputBound)
	if err != nil {
		return nil, nil, newStableError(codes.InvalidArgument, 400, ReasonLimitExceeded, "请求 token 上界溢出")
	}
	attemptLimit := int64(effectiveAttemptLimit(route))
	reservedTokens, ok := checkedMul(attemptTokens, attemptLimit)
	if !ok {
		return nil, nil, newStableError(codes.InvalidArgument, 400, ReasonLimitExceeded, "请求 token 预留溢出")
	}
	attemptCost, err := maxAttemptCost(route.models, inputBound, outputBound)
	if err != nil {
		return nil, nil, newStableError(codes.Internal, 500, "INTERNAL", "价格计算失败")
	}
	reservedCost, ok := checkedMul(attemptCost, attemptLimit)
	if !ok {
		return nil, nil, newStableError(codes.Internal, 500, "INTERNAL", "预算预留溢出")
	}

	prepared := &preparedCall{
		logicalCallPK:   s.newID(),
		ledgerEntryID:   s.newID(),
		attemptID:       s.newID(),
		principalID:     req.PrincipalID,
		sessionID:       req.SessionID,
		runID:           req.RunID,
		role:            req.Role,
		releaseID:       req.ReleaseID,
		logicalCallID:   req.LogicalCallID,
		requestHash:     requestHash,
		route:           route,
		prompt:          prompt,
		schema:          schema,
		selected:        route.models[0],
		inputBound:      inputBound,
		outputBound:     outputBound,
		reservedTokens:  reservedTokens,
		reservedCost:    reservedCost,
		attemptTokenCap: attemptTokens,
		attemptCostCap:  attemptCost,
		traceID:         req.TraceID,
		startedAt:       s.now(),
	}
	prepared.attemptStartedAt = prepared.startedAt
	prepared.providerRequest = ProviderRequest{
		LogicalCallID:   req.LogicalCallID,
		AttemptID:       prepared.attemptID,
		Model:           prepared.selected.modelID,
		Provider:        prepared.selected.provider,
		Messages:        append([]ProviderMessage(nil), req.Messages...),
		ResponseSchema:  schemaDocument(schema),
		Temperature:     req.Temperature,
		MaxOutputTokens: route.maxOutput,
		Timeout:         time.Duration(route.timeoutMS) * time.Millisecond,
	}
	firstAttemptCost, err := actualCost(prepared.selected, inputBound, outputBound)
	if err != nil {
		return nil, nil, newStableError(codes.Internal, 500, "INTERNAL", "供应商 attempt 成本计算失败")
	}

	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return nil, nil, newStableError(codes.Internal, 500, "INTERNAL", "启动预算事务失败")
	}
	defer tx.Rollback(ctx)
	queries := s.queries.WithTx(tx)

	budgetScopeID := s.newID()
	if err := queries.InsertBudgetScopeIfAbsent(
		ctx,
		llmgwdb.InsertBudgetScopeIfAbsentParams{
			Column1:     databaseUUID(budgetScopeID),
			PrincipalID: req.PrincipalID,
			Column3:     databaseUUID(req.SessionID),
		},
	); err != nil {
		return nil, nil, newStableError(codes.Internal, 500, "INTERNAL", "初始化预算作用域失败")
	}
	budgetRow, err := queries.GetBudgetScopeBySecurityScope(
		ctx,
		llmgwdb.GetBudgetScopeBySecurityScopeParams{
			PrincipalID: req.PrincipalID,
			Column2:     databaseUUID(req.SessionID),
		},
	)
	if err != nil {
		return nil, nil, newStableError(codes.Internal, 500, "INTERNAL", "读取预算作用域失败")
	}
	budgetScopeID = budgetRow.BudgetScopeID
	budgetStatus := budgetRow.BudgetStatus
	if budgetStatus == "BUDGET_OVERRUN" {
		return nil, nil, newStableError(
			codes.ResourceExhausted, 429, ReasonBudgetOverrun,
			"预算作用域已因实际用量超预留而停止后续模型调用",
		)
	}

	err = queries.InsertLogicalCall(ctx, llmgwdb.InsertLogicalCallParams{
		Column1:                databaseUUID(prepared.logicalCallPK),
		Column2:                databaseUUID(prepared.ledgerEntryID),
		LogicalCallID:          req.LogicalCallID,
		PrincipalID:            req.PrincipalID,
		Column5:                databaseUUID(req.SessionID),
		Column6:                databaseUUID(req.RunID),
		Role:                   req.Role,
		ReleaseID:              req.ReleaseID,
		RouteVersion:           route.version,
		PriceTableVersion:      route.priceVersion,
		ModelID:                prepared.selected.modelID,
		RequestHash:            requestHash,
		Column13:               databaseUUID(budgetScopeID),
		DataClassification:     req.DataClassification,
		ReservedInputTokens:    inputBound,
		ReservedOutputTokens:   outputBound,
		ReservedCostMicroUsd:   reservedCost,
		ReservedProviderTokens: databaseInt8(reservedTokens),
		PromptTemplateKey:      databaseText(prompt.key),
		PromptTemplateVersion:  databaseText(prompt.version),
		PromptTemplateSha256:   databaseText(prompt.digest),
		ResponseSchemaKey:      databaseText(schemaKey(schema)),
		ResponseSchemaVersion:  databaseText(schemaVersion(schema)),
		ResponseSchemaSha256:   databaseText(schemaDigest(schema)),
	})
	if isUniqueViolation(err) {
		_ = tx.Rollback(ctx)
		return s.resolveExistingCall(ctx, req, requestHash)
	}
	if err != nil {
		return nil, nil, newStableError(codes.Internal, 500, "INTERNAL", "创建逻辑调用账本失败")
	}

	affected, err := queries.ReserveBudget(ctx, llmgwdb.ReserveBudgetParams{
		ProviderTokensReserved: reservedTokens,
		CostReservedMicroUsd:   reservedCost,
		Column3:                databaseUUID(budgetScopeID),
	})
	if err != nil {
		return nil, nil, newStableError(codes.Internal, 500, "INTERNAL", "原子预占预算失败")
	}
	if affected != 1 {
		budgetStatus, err = queries.GetBudgetStatusByID(
			ctx,
			databaseUUID(budgetScopeID),
		)
		if err != nil {
			return nil, nil, newStableError(codes.Internal, 500, "INTERNAL", "读取预算拒绝原因失败")
		}
		if budgetStatus == "BUDGET_OVERRUN" {
			return nil, nil, newStableError(
				codes.ResourceExhausted, 429, ReasonBudgetOverrun,
				"预算作用域已因实际用量超预留而停止后续模型调用",
			)
		}
		return nil, nil, newStableError(codes.ResourceExhausted, 429, ReasonBudgetExceeded, "会话预算不足")
	}

	if err := queries.InsertFirstAttempt(ctx, llmgwdb.InsertFirstAttemptParams{
		Column1:              databaseUUID(prepared.attemptID),
		Column2:              databaseUUID(prepared.logicalCallPK),
		LogicalCallID:        req.LogicalCallID,
		ModelID:              prepared.selected.modelID,
		Provider:             prepared.selected.provider,
		ReservedTokens:       attemptTokens,
		ReservedCostMicroUsd: firstAttemptCost,
		StartedAt:            databaseTimestamp(prepared.attemptStartedAt),
	}); err != nil {
		return nil, nil, newStableError(codes.Internal, 500, "INTERNAL", "创建供应商 attempt 失败")
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, nil, newStableError(codes.Internal, 500, "INTERNAL", "提交预算预占失败")
	}
	return prepared, nil, nil
}

func (s *Service) startAttempt(
	ctx context.Context,
	prepared *preparedCall,
	model modelPrice,
	attemptNo int32,
) error {
	attemptTokens, err := checkedAdd(prepared.inputBound, prepared.outputBound)
	if err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "供应商 attempt token 上界溢出")
	}
	attemptCost, err := actualCost(model, prepared.inputBound, prepared.outputBound)
	if err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "供应商 attempt 成本计算失败")
	}
	attemptID := s.newID()
	startedAt := s.now()
	if err := s.queries.InsertAttempt(ctx, llmgwdb.InsertAttemptParams{
		Column1:              databaseUUID(attemptID),
		Column2:              databaseUUID(prepared.logicalCallPK),
		LogicalCallID:        prepared.logicalCallID,
		AttemptNo:            attemptNo,
		ModelID:              model.modelID,
		Provider:             model.provider,
		ReservedTokens:       attemptTokens,
		ReservedCostMicroUsd: attemptCost,
		StartedAt:            databaseTimestamp(startedAt),
	}); err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "创建供应商 attempt 失败")
	}
	prepared.attemptID = attemptID
	prepared.selected = model
	prepared.attemptStartedAt = startedAt
	prepared.providerRequest.AttemptID = attemptID
	prepared.providerRequest.Model = model.modelID
	prepared.providerRequest.Provider = model.provider
	return nil
}

func (s *Service) finishRetryableAttempt(
	ctx context.Context,
	prepared *preparedCall,
	failure *ProviderFailure,
) error {
	finishedAt := s.now()
	affected, err := s.queries.FinishRetryableAttempt(
		ctx,
		llmgwdb.FinishRetryableAttemptParams{
			Column1:    nullableText(failure.ProviderRequestID),
			ErrorCode:  databaseText(failure.Code),
			FinishedAt: databaseTimestamp(finishedAt),
			Column4:    databaseUUID(prepared.attemptID),
		},
	)
	if err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "记录可重试供应商失败失败")
	}
	if affected != 1 {
		return newStableError(codes.Aborted, 409, ReasonCallInProgress, "attempt 状态不允许可重试失败结算")
	}
	return nil
}
