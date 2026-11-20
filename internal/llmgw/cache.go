package llmgw

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"time"

	"github.com/jackc/pgx/v5"
	"google.golang.org/grpc/codes"

	llmgwv1 "github.com/choucisan/ai-gaokao-counselor/api/llmgw/v1"
	llmgwdb "github.com/choucisan/ai-gaokao-counselor/internal/llmgw/dbgen"
)

const (
	resultCacheSchemaVersion = "llm-result-cache-v1"
	resultCachePolicy        = "SHARED_PUBLIC_DATA_V1"
)

type resultCacheIdentity struct {
	keyHash       string
	manifest      []byte
	releaseID     string
	role          string
	routeVersion  string
	modelID       string
	promptKey     string
	promptVersion string
	promptDigest  string
	schemaKey     string
	schemaVersion string
	schemaDigest  string
	dataClass     string
	maxOutput     int32
}

type resultCacheKeyManifest struct {
	CacheSchemaVersion string   `json:"cache_schema_version"`
	CachePolicy        string   `json:"cache_policy"`
	ReleaseID          string   `json:"release_id"`
	Role               string   `json:"role"`
	RouteVersion       string   `json:"route_version"`
	ModelID            string   `json:"model_id"`
	Models             []string `json:"model_ids"`
	PromptTemplateKey  string   `json:"prompt_template_key"`
	PromptVersion      string   `json:"prompt_template_version"`
	PromptDigest       string   `json:"prompt_template_sha256"`
	SchemaKey          string   `json:"response_schema_key,omitempty"`
	SchemaVersion      string   `json:"response_schema_version,omitempty"`
	SchemaDigest       string   `json:"response_schema_sha256,omitempty"`
	DataClassification string   `json:"data_classification"`
	AllowedDataClasses []string `json:"allowed_data_classes"`
	Temperature        float64  `json:"temperature"`
	MaxOutputTokens    int32    `json:"max_output_tokens"`
	MessagesSHA256     string   `json:"messages_sha256"`
}

type canonicalResultCacheKey struct {
	resultCacheKeyManifest
	Messages []ProviderMessage `json:"messages"`
}

type cachedChatResult struct {
	Content          string         `json:"content"`
	StructuredOutput map[string]any `json:"structured_output,omitempty"`
	FinishReason     string         `json:"finish_reason"`
}

type resultCacheClaim struct {
	keyHash string
	token   string
}

type resultCacheEntry struct {
	releaseID          string
	role               string
	routeVersion       string
	modelID            string
	promptKey          string
	promptVersion      string
	promptDigest       string
	schemaKey          string
	schemaVersion      string
	schemaDigest       string
	dataClassification string
	maxOutputTokens    int32
	temperature        float64
	result             cachedChatResult
}

func resultCacheEligible(req governedRequest, route routeConfig) bool {
	return route.allowResultCache &&
		req.Temperature == 0 &&
		req.DataClassification == dataClassPublicData
}

func buildResultCacheIdentity(
	req governedRequest,
	route routeConfig,
	prompt promptTemplate,
	schema *responseSchema,
) (*resultCacheIdentity, error) {
	modelID := route.models[0].modelID
	modelIDs := make([]string, 0, len(route.models))
	for _, model := range route.models {
		modelIDs = append(modelIDs, model.modelID)
	}
	allowedDataClasses := append([]string(nil), route.allowedDataClasses...)
	messagesPayload, err := json.Marshal(req.Messages)
	if err != nil {
		return nil, newStableError(codes.Internal, 500, "INTERNAL", "规范化缓存 messages 失败")
	}
	messagesSum := sha256.Sum256(messagesPayload)
	manifest := resultCacheKeyManifest{
		CacheSchemaVersion: resultCacheSchemaVersion,
		CachePolicy:        resultCachePolicy,
		ReleaseID:          req.ReleaseID,
		Role:               req.Role,
		RouteVersion:       route.version,
		ModelID:            modelID,
		Models:             modelIDs,
		PromptTemplateKey:  prompt.key,
		PromptVersion:      prompt.version,
		PromptDigest:       prompt.digest,
		SchemaKey:          schemaKey(schema),
		SchemaVersion:      schemaVersion(schema),
		SchemaDigest:       schemaDigest(schema),
		DataClassification: req.DataClassification,
		AllowedDataClasses: allowedDataClasses,
		Temperature:        req.Temperature,
		MaxOutputTokens:    route.maxOutput,
		MessagesSHA256:     hex.EncodeToString(messagesSum[:]),
	}
	canonicalBytes, err := json.Marshal(canonicalResultCacheKey{
		resultCacheKeyManifest: manifest,
		Messages:               req.Messages,
	})
	if err != nil {
		return nil, newStableError(codes.Internal, 500, "INTERNAL", "规范化缓存键失败")
	}
	keySum := sha256.Sum256(canonicalBytes)
	manifestBytes, err := json.Marshal(manifest)
	if err != nil {
		return nil, newStableError(codes.Internal, 500, "INTERNAL", "序列化缓存键摘要失败")
	}
	return &resultCacheIdentity{
		keyHash:       hex.EncodeToString(keySum[:]),
		manifest:      manifestBytes,
		releaseID:     req.ReleaseID,
		role:          req.Role,
		routeVersion:  route.version,
		modelID:       modelID,
		promptKey:     prompt.key,
		promptVersion: prompt.version,
		promptDigest:  prompt.digest,
		schemaKey:     schemaKey(schema),
		schemaVersion: schemaVersion(schema),
		schemaDigest:  schemaDigest(schema),
		dataClass:     req.DataClassification,
		maxOutput:     route.maxOutput,
	}, nil
}

func (s *Service) claimResultCache(
	ctx context.Context,
	identity *resultCacheIdentity,
	claimLease time.Duration,
) (*resultCacheClaim, *resultCacheEntry, error) {
	claimToken := s.newID()
	now := s.now()
	claimExpiresAt := now.Add(claimLease)
	affected, err := s.queries.InsertCacheClaim(ctx, llmgwdb.InsertCacheClaimParams{
		CacheKeyHash:          identity.keyHash,
		CacheSchemaVersion:    resultCacheSchemaVersion,
		ReleaseID:             identity.releaseID,
		Role:                  identity.role,
		RouteVersion:          identity.routeVersion,
		ModelID:               identity.modelID,
		PromptTemplateKey:     identity.promptKey,
		PromptTemplateVersion: identity.promptVersion,
		PromptTemplateSha256:  identity.promptDigest,
		ResponseSchemaKey:     databaseText(identity.schemaKey),
		ResponseSchemaVersion: databaseText(identity.schemaVersion),
		ResponseSchemaSha256:  databaseText(identity.schemaDigest),
		DataClassification:    identity.dataClass,
		MaxOutputTokens:       identity.maxOutput,
		Column15:              identity.manifest,
		Column16:              databaseUUID(claimToken),
		ClaimStartedAt:        databaseTimestamp(now),
		ClaimExpiresAt:        databaseTimestamp(claimExpiresAt),
	})
	if err != nil {
		return nil, nil, newStableError(codes.Internal, 500, "INTERNAL", "创建结果缓存 claim 失败")
	}
	if affected == 1 {
		return &resultCacheClaim{keyHash: identity.keyHash, token: claimToken}, nil, nil
	}

	for lookup := 0; lookup < 2; lookup++ {
		entry, status, currentToken, currentExpiry, payload, err := s.loadResultCache(
			ctx, identity.keyHash,
		)
		if err != nil {
			return nil, nil, err
		}
		if status == "READY" {
			if entry.schemaKey != identity.schemaKey ||
				entry.schemaVersion != identity.schemaVersion ||
				entry.schemaDigest != identity.schemaDigest ||
				entry.role != identity.role ||
				entry.routeVersion != identity.routeVersion ||
				entry.modelID != identity.modelID ||
				entry.releaseID != identity.releaseID ||
				entry.promptKey != identity.promptKey ||
				entry.promptVersion != identity.promptVersion ||
				entry.promptDigest != identity.promptDigest ||
				entry.dataClassification != identity.dataClass ||
				entry.maxOutputTokens != identity.maxOutput ||
				entry.temperature != 0 {
				return nil, nil, newStableError(
					codes.Internal, 500, "INTERNAL", "结果缓存治理身份不一致",
				)
			}
			if err := json.Unmarshal(payload, &entry.result); err != nil {
				return nil, nil, newStableError(
					codes.Internal, 500, "INTERNAL", "读取结果缓存内容失败",
				)
			}
			return nil, &entry, nil
		}
		if status == "IN_PROGRESS" && currentToken != claimToken && currentExpiry.After(now) {
			return nil, nil, newStableError(
				codes.Aborted, 409, ReasonCallInProgress,
				"相同结果缓存键的首次调用仍在执行",
			)
		}

		affected, err := s.queries.TakeOverCacheClaim(
			ctx,
			llmgwdb.TakeOverCacheClaimParams{
				CacheKeyHash:          identity.keyHash,
				CacheSchemaVersion:    resultCacheSchemaVersion,
				ReleaseID:             identity.releaseID,
				Role:                  identity.role,
				RouteVersion:          identity.routeVersion,
				ModelID:               identity.modelID,
				PromptTemplateKey:     identity.promptKey,
				PromptTemplateVersion: identity.promptVersion,
				PromptTemplateSha256:  identity.promptDigest,
				ResponseSchemaKey:     databaseText(identity.schemaKey),
				ResponseSchemaVersion: databaseText(identity.schemaVersion),
				ResponseSchemaSha256:  databaseText(identity.schemaDigest),
				DataClassification:    identity.dataClass,
				MaxOutputTokens:       identity.maxOutput,
				Column15:              identity.manifest,
				Column16:              databaseUUID(claimToken),
				ClaimStartedAt:        databaseTimestamp(now),
				ClaimExpiresAt:        databaseTimestamp(claimExpiresAt),
			},
		)
		if err != nil {
			return nil, nil, newStableError(codes.Internal, 500, "INTERNAL", "接管结果缓存 claim 失败")
		}
		if affected == 1 {
			return &resultCacheClaim{keyHash: identity.keyHash, token: claimToken}, nil, nil
		}
	}
	return nil, nil, newStableError(
		codes.Aborted, 409, ReasonCallInProgress,
		"相同结果缓存键的状态正在变化",
	)
}

func (s *Service) loadResultCache(
	ctx context.Context,
	keyHash string,
) (resultCacheEntry, string, string, time.Time, []byte, error) {
	row, err := s.queries.GetCacheEntry(ctx, keyHash)
	if errors.Is(err, pgx.ErrNoRows) {
		return resultCacheEntry{}, "", "", time.Time{}, nil, newStableError(
			codes.Internal, 500, "INTERNAL", "结果缓存 claim 缺失",
		)
	}
	if err != nil {
		return resultCacheEntry{}, "", "", time.Time{}, nil, newStableError(
			codes.Internal, 500, "INTERNAL", "读取结果缓存状态失败",
		)
	}
	entry := resultCacheEntry{
		releaseID:          row.ReleaseID,
		role:               row.Role,
		routeVersion:       row.RouteVersion,
		modelID:            row.ModelID,
		promptKey:          row.PromptTemplateKey,
		promptVersion:      row.PromptTemplateVersion,
		promptDigest:       row.PromptTemplateSha256,
		schemaKey:          row.ResponseSchemaKey.String,
		schemaVersion:      row.ResponseSchemaVersion.String,
		schemaDigest:       row.ResponseSchemaSha256.String,
		dataClassification: row.DataClassification,
		maxOutputTokens:    row.MaxOutputTokens,
		temperature:        row.Temperature,
	}
	return entry, row.Status, row.ClaimToken, row.ClaimExpiresAt.Time, row.ResultPayload, nil
}

func (s *Service) recordResultCacheHit(
	ctx context.Context,
	req governedRequest,
	route routeConfig,
	prompt promptTemplate,
	requestHash string,
	cacheKeyHash string,
	entry *resultCacheEntry,
) (*llmgwv1.ChatResponse, error) {
	result := providerResult{
		Content:          entry.result.Content,
		StructuredOutput: entry.result.StructuredOutput,
		FinishReason:     entry.result.FinishReason,
	}
	payload, err := json.Marshal(result)
	if err != nil {
		return nil, newStableError(codes.Internal, 500, "INTERNAL", "序列化缓存命中结果失败")
	}
	prepared := &preparedCall{
		logicalCallPK: s.newID(),
		ledgerEntryID: s.newID(),
		principalID:   req.PrincipalID,
		sessionID:     req.SessionID,
		runID:         req.RunID,
		role:          req.Role,
		releaseID:     req.ReleaseID,
		logicalCallID: req.LogicalCallID,
		requestHash:   requestHash,
		route:         route,
		prompt:        promptTemplate{key: entry.promptKey, version: entry.promptVersion, digest: entry.promptDigest},
		selected:      modelPrice{modelID: entry.modelID},
		cacheKeyHash:  cacheKeyHash,
		cacheHit:      true,
		traceID:       req.TraceID,
		startedAt:     s.now(),
	}
	if entry.schemaKey != "" {
		prepared.schema = &responseSchema{
			key: entry.schemaKey, version: entry.schemaVersion, digest: entry.schemaDigest,
		}
	}

	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return nil, newStableError(codes.Internal, 500, "INTERNAL", "启动缓存命中事务失败")
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
		return nil, newStableError(codes.Internal, 500, "INTERNAL", "初始化缓存命中预算作用域失败")
	}
	budgetRow, err := queries.LockBudgetScopeForCacheHit(
		ctx,
		llmgwdb.LockBudgetScopeForCacheHitParams{
			PrincipalID: req.PrincipalID,
			Column2:     databaseUUID(req.SessionID),
		},
	)
	if err != nil {
		return nil, newStableError(codes.Internal, 500, "INTERNAL", "读取缓存命中预算作用域失败")
	}
	budgetScopeID = budgetRow.BudgetScopeID
	if budgetRow.BudgetStatus == "BUDGET_OVERRUN" {
		return nil, newStableError(
			codes.ResourceExhausted, 429, ReasonBudgetOverrun,
			"预算作用域已因实际用量超预留而停止后续模型调用",
		)
	}

	completedAt := s.now()
	_, err = queries.InsertCacheHitLogicalCall(
		ctx,
		llmgwdb.InsertCacheHitLogicalCallParams{
			Column1:               databaseUUID(prepared.logicalCallPK),
			Column2:               databaseUUID(prepared.ledgerEntryID),
			LogicalCallID:         req.LogicalCallID,
			PrincipalID:           req.PrincipalID,
			Column5:               databaseUUID(req.SessionID),
			Column6:               databaseUUID(req.RunID),
			Role:                  req.Role,
			ReleaseID:             req.ReleaseID,
			RouteVersion:          route.version,
			PriceTableVersion:     route.priceVersion,
			ModelID:               entry.modelID,
			RequestHash:           requestHash,
			Column13:              databaseUUID(budgetScopeID),
			DataClassification:    req.DataClassification,
			ResponsePayload:       payload,
			PromptTemplateKey:     databaseText(entry.promptKey),
			PromptTemplateVersion: databaseText(entry.promptVersion),
			PromptTemplateSha256:  databaseText(entry.promptDigest),
			ResponseSchemaKey:     databaseText(entry.schemaKey),
			ResponseSchemaVersion: databaseText(entry.schemaVersion),
			ResponseSchemaSha256:  databaseText(entry.schemaDigest),
			CacheKeyHash:          databaseText(prepared.cacheKeyHash),
			CompletedAt:           databaseTimestamp(completedAt),
		},
	)
	if isUniqueViolation(err) {
		_ = tx.Rollback(ctx)
		_, replayed, replayErr := s.resolveExistingCall(ctx, req, requestHash)
		return replayed, replayErr
	}
	if err != nil {
		return nil, newStableError(codes.Internal, 500, "INTERNAL", "创建缓存命中 logical call 失败")
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, newStableError(codes.Internal, 500, "INTERNAL", "提交缓存命中 logical call 失败")
	}
	s.insertProjection(ctx, prepared, result, 0)
	return buildResponse(prepared, result, 0, nil), nil
}

func (s *Service) storeResultCache(
	prepared *preparedCall,
	result providerResult,
) {
	if prepared.cacheClaimToken == "" {
		return
	}
	if len(prepared.route.models) == 0 ||
		prepared.selected.modelID != prepared.route.models[0].modelID {
		s.failResultCache(&resultCacheClaim{
			keyHash: prepared.cacheKeyHash,
			token:   prepared.cacheClaimToken,
		})
		return
	}
	payload, err := json.Marshal(cachedChatResult{
		Content:          result.Content,
		StructuredOutput: result.StructuredOutput,
		FinishReason:     result.FinishReason,
	})
	if err != nil {
		s.failResultCache(&resultCacheClaim{
			keyHash: prepared.cacheKeyHash,
			token:   prepared.cacheClaimToken,
		})
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	_, _ = s.queries.MarkCacheReady(ctx, llmgwdb.MarkCacheReadyParams{
		Column1:      payload,
		CompletedAt:  databaseTimestamp(s.now()),
		CacheKeyHash: prepared.cacheKeyHash,
		Column4:      databaseUUID(prepared.cacheClaimToken),
	})
}

func (s *Service) failResultCache(claim *resultCacheClaim) {
	if claim == nil || claim.keyHash == "" || claim.token == "" {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	_, _ = s.queries.MarkCacheFailed(ctx, llmgwdb.MarkCacheFailedParams{
		CompletedAt:  databaseTimestamp(s.now()),
		CacheKeyHash: claim.keyHash,
		Column3:      databaseUUID(claim.token),
	})
}
