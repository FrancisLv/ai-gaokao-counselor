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
	timeoutMS int32,
) (*resultCacheClaim, *resultCacheEntry, error) {
	claimToken := s.newID()
	now := s.now()
	claimExpiresAt := now.Add(time.Duration(timeoutMS)*time.Millisecond + 30*time.Second)
	tag, err := s.pool.Exec(ctx, `
		INSERT INTO llm_result_cache (
			cache_key_hash, cache_schema_version, release_id, role,
			route_version, model_id, prompt_template_key,
			prompt_template_version, prompt_template_sha256,
			response_schema_key, response_schema_version,
			response_schema_sha256, data_classification,
			max_output_tokens, temperature, request_key_manifest,
			status, claim_token, claim_started_at, claim_expires_at
		) VALUES (
			$1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
			$12, $13, $14, 0, $15::jsonb, 'IN_PROGRESS', $16, $17, $18
		)
		ON CONFLICT (cache_key_hash) DO NOTHING`,
		identity.keyHash, resultCacheSchemaVersion, identity.releaseID,
		identity.role, identity.routeVersion, identity.modelID,
		identity.promptKey, identity.promptVersion, identity.promptDigest,
		nullableText(identity.schemaKey), nullableText(identity.schemaVersion),
		nullableText(identity.schemaDigest), identity.dataClass,
		identity.maxOutput, identity.manifest, claimToken, now, claimExpiresAt,
	)
	if err != nil {
		return nil, nil, newStableError(codes.Internal, 500, "INTERNAL", "创建结果缓存 claim 失败")
	}
	if tag.RowsAffected() == 1 {
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

		tag, err := s.pool.Exec(ctx, `
			UPDATE llm_result_cache
			SET cache_schema_version = $2,
			    release_id = $3,
			    role = $4,
			    route_version = $5,
			    model_id = $6,
			    prompt_template_key = $7,
			    prompt_template_version = $8,
			    prompt_template_sha256 = $9,
			    response_schema_key = $10,
			    response_schema_version = $11,
			    response_schema_sha256 = $12,
			    data_classification = $13,
			    max_output_tokens = $14,
			    temperature = 0,
			    request_key_manifest = $15::jsonb,
			    status = 'IN_PROGRESS',
			    claim_token = $16,
			    claim_started_at = $17,
			    claim_expires_at = $18,
			    result_payload = NULL,
			    invalidation_reason = NULL,
			    invalidated_at = NULL,
			    completed_at = NULL,
			    updated_at = $17
			WHERE cache_key_hash = $1
			  AND (
			      status IN ('FAILED', 'INVALIDATED')
			      OR (status = 'IN_PROGRESS' AND claim_expires_at <= $17)
			  )`,
			identity.keyHash, resultCacheSchemaVersion, identity.releaseID,
			identity.role, identity.routeVersion, identity.modelID,
			identity.promptKey, identity.promptVersion, identity.promptDigest,
			nullableText(identity.schemaKey), nullableText(identity.schemaVersion),
			nullableText(identity.schemaDigest), identity.dataClass,
			identity.maxOutput, identity.manifest, claimToken, now, claimExpiresAt,
		)
		if err != nil {
			return nil, nil, newStableError(codes.Internal, 500, "INTERNAL", "接管结果缓存 claim 失败")
		}
		if tag.RowsAffected() == 1 {
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
	var (
		entry                    resultCacheEntry
		status, claimToken       string
		claimExpiresAt           time.Time
		schemaKey, schemaVersion *string
		schemaDigest             *string
		payload                  []byte
	)
	err := s.pool.QueryRow(ctx, `
		SELECT release_id, role, route_version, model_id,
		       prompt_template_key, prompt_template_version,
		       prompt_template_sha256, response_schema_key,
		       response_schema_version, response_schema_sha256,
		       data_classification, max_output_tokens, temperature,
		       status, claim_token::text, claim_expires_at,
		       result_payload
		FROM llm_result_cache
		WHERE cache_key_hash = $1`,
		keyHash,
	).Scan(
		&entry.releaseID, &entry.role, &entry.routeVersion, &entry.modelID,
		&entry.promptKey, &entry.promptVersion, &entry.promptDigest,
		&schemaKey, &schemaVersion, &schemaDigest,
		&entry.dataClassification, &entry.maxOutputTokens, &entry.temperature,
		&status, &claimToken, &claimExpiresAt, &payload,
	)
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
	entry.schemaKey = valueOrEmpty(schemaKey)
	entry.schemaVersion = valueOrEmpty(schemaVersion)
	entry.schemaDigest = valueOrEmpty(schemaDigest)
	return entry, status, claimToken, claimExpiresAt, payload, nil
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

	budgetScopeID := s.newID()
	if _, err := tx.Exec(ctx, `
		INSERT INTO llm_budget_scope (budget_scope_id, principal_id, session_id)
		VALUES ($1, $2, $3)
		ON CONFLICT (principal_id, session_id) DO NOTHING`,
		budgetScopeID, req.PrincipalID, req.SessionID,
	); err != nil {
		return nil, newStableError(codes.Internal, 500, "INTERNAL", "初始化缓存命中预算作用域失败")
	}
	if err := tx.QueryRow(ctx, `
		SELECT budget_scope_id::text
		FROM llm_budget_scope
		WHERE principal_id = $1 AND session_id = $2`,
		req.PrincipalID, req.SessionID,
	).Scan(&budgetScopeID); err != nil {
		return nil, newStableError(codes.Internal, 500, "INTERNAL", "读取缓存命中预算作用域失败")
	}

	completedAt := s.now()
	_, err = tx.Exec(ctx, `
		INSERT INTO llm_logical_call (
			logical_call_pk, ledger_entry_id, logical_call_id, principal_id,
			session_id, run_id, role, release_id, route_version,
			price_table_version, model_id, request_hash, budget_scope_id,
			status, data_classification, reserved_input_tokens,
			reserved_output_tokens, reserved_cost_micro_usd,
			reserved_provider_tokens, actual_input_tokens,
			actual_output_tokens, actual_cost_micro_usd, response_payload,
			prompt_template_key, prompt_template_version,
			prompt_template_sha256, response_schema_key,
			response_schema_version, response_schema_sha256,
			cache_hit, cache_key_hash, completed_at
		) VALUES (
			$1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
			'SUCCEEDED', $14, 0, 0, 0, 0, 0, 0, 0, $15,
			$16, $17, $18, $19, $20, $21, TRUE, $22, $23
		)`,
		prepared.logicalCallPK, prepared.ledgerEntryID, req.LogicalCallID,
		req.PrincipalID, req.SessionID, req.RunID, req.Role, req.ReleaseID,
		route.version, route.priceVersion, entry.modelID, requestHash,
		budgetScopeID, req.DataClassification, payload,
		entry.promptKey, entry.promptVersion, entry.promptDigest,
		nullableText(entry.schemaKey), nullableText(entry.schemaVersion),
		nullableText(entry.schemaDigest), prepared.cacheKeyHash, completedAt,
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
	_, _ = s.pool.Exec(ctx, `
		UPDATE llm_result_cache
		SET status = 'READY',
		    result_payload = $1::jsonb,
		    completed_at = $2,
		    updated_at = $2
		WHERE cache_key_hash = $3
		  AND status = 'IN_PROGRESS'
		  AND claim_token = $4`,
		payload, s.now(), prepared.cacheKeyHash, prepared.cacheClaimToken,
	)
}

func (s *Service) failResultCache(claim *resultCacheClaim) {
	if claim == nil || claim.keyHash == "" || claim.token == "" {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	_, _ = s.pool.Exec(ctx, `
		UPDATE llm_result_cache
		SET status = 'FAILED',
		    result_payload = NULL,
		    completed_at = $1,
		    updated_at = $1
		WHERE cache_key_hash = $2
		  AND status = 'IN_PROGRESS'
		  AND claim_token = $3`,
		s.now(), claim.keyHash, claim.token,
	)
}
