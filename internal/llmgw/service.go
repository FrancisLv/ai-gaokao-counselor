// Package llmgw implements the LLM governance seam.
package llmgw

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"math"
	"regexp"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
	"google.golang.org/grpc/codes"
	"google.golang.org/protobuf/types/known/structpb"

	llmgwv1 "github.com/choucisan/ai-gaokao-counselor/api/llmgw/v1"
)

type Service struct {
	llmgwv1.UnimplementedLLMGatewayServer

	pool     *pgxpool.Pool
	provider Provider
	now      func() time.Time
	newID    func() string
}

const maxProviderAttempts int32 = 2

type Option func(*Service)

func WithClock(now func() time.Time) Option {
	return func(s *Service) {
		if now != nil {
			s.now = now
		}
	}
}

func WithIDGenerator(newID func() string) Option {
	return func(s *Service) {
		if newID != nil {
			s.newID = newID
		}
	}
}

func NewService(pool *pgxpool.Pool, provider Provider, opts ...Option) *Service {
	if provider == nil {
		provider = unavailableProvider{}
	}
	service := &Service{
		pool:     pool,
		provider: provider,
		now:      time.Now,
		newID:    uuid.NewString,
	}
	for _, opt := range opts {
		opt(service)
	}
	return service
}

type governedRequest struct {
	PrincipalID        string
	SessionID          string
	RunID              string
	Role               string
	ReleaseID          string
	LogicalCallID      string
	PromptTemplateKey  string
	PromptVersion      string
	SchemaKey          string
	SchemaVersion      string
	DataClassification string
	Messages           []ProviderMessage
	RawSchema          string
	PromptVariables    map[string]string
	Temperature        float64
	MaxTokens          int32
	TimeoutMS          int32
	TraceID            string
}

type runBinding struct {
	releaseStatus string
	remainingMS   int64
}

type modelPrice struct {
	modelID    string
	provider   string
	inputRate  int64
	outputRate int64
}

type routeConfig struct {
	version                string
	priceVersion           string
	primaryModel           string
	maxOutput              int32
	timeoutMS              int32
	maxAttempts            int32
	requiresResponseSchema bool
	models                 []modelPrice
}

type promptTemplate struct {
	key      string
	version  string
	system   string
	user     string
	digest   string
	messages []ProviderMessage
}

type preparedCall struct {
	logicalCallPK    string
	ledgerEntryID    string
	attemptID        string
	principalID      string
	sessionID        string
	runID            string
	role             string
	releaseID        string
	logicalCallID    string
	requestHash      string
	route            routeConfig
	prompt           promptTemplate
	schema           *responseSchema
	selected         modelPrice
	inputBound       int64
	outputBound      int64
	reservedTokens   int64
	reservedCost     int64
	attemptTokenCap  int64
	attemptCostCap   int64
	providerRequest  ProviderRequest
	traceID          string
	startedAt        time.Time
	attemptStartedAt time.Time
}

type canonicalRequest struct {
	Role               string            `json:"role"`
	Messages           []ProviderMessage `json:"messages"`
	PromptTemplateKey  string            `json:"prompt_template_key"`
	PromptVersion      string            `json:"prompt_template_version"`
	SchemaKey          string            `json:"response_schema_key"`
	SchemaVersion      string            `json:"response_schema_version"`
	DataClassification string            `json:"data_classification"`
	Temperature        float64           `json:"temperature"`
	MaxOutputTokens    int32             `json:"max_output_tokens"`
	TimeoutMS          int32             `json:"timeout_ms"`
	ReleaseID          string            `json:"release_id"`
	RouteVersion       string            `json:"route_version"`
	Models             []string          `json:"models"`
	PromptDigest       string            `json:"prompt_template_sha256"`
	SchemaDigest       string            `json:"response_schema_sha256,omitempty"`
}

type providerResult struct {
	Content           string         `json:"content"`
	StructuredOutput  map[string]any `json:"structured_output,omitempty"`
	InputTokens       int64          `json:"input_tokens"`
	OutputTokens      int64          `json:"output_tokens"`
	ProviderRequestID string         `json:"provider_request_id,omitempty"`
	FinishReason      string         `json:"finish_reason"`
}

// Chat is the sole public behavior seam for one governed logical model call.
func (s *Service) Chat(ctx context.Context, req *llmgwv1.ChatRequest) (*llmgwv1.ChatResponse, error) {
	principalID, err := requirePrincipal(ctx)
	if err != nil {
		return nil, err
	}
	if s.pool == nil {
		return nil, newStableError(codes.Internal, 500, "INTERNAL", "模型网关数据库不可用")
	}
	governed, err := normalizeRequest(principalID, req)
	if err != nil {
		return nil, err
	}

	binding, err := s.loadRunBinding(ctx, governed)
	if err != nil {
		return nil, err
	}
	route, err := s.loadRoute(ctx, governed, binding)
	if err != nil {
		return nil, err
	}
	prompt, err := s.loadPromptTemplate(ctx, governed)
	if err != nil {
		return nil, err
	}
	schema, err := s.loadResponseSchema(ctx, governed, route)
	if err != nil {
		return nil, err
	}
	governed.Messages = prompt.messages
	requestHash, inputBound, err := canonicalHash(governed, route, prompt, schema)
	if err != nil {
		return nil, err
	}
	prepared, replayed, err := s.reserveCall(
		ctx, governed, route, prompt, schema, requestHash, inputBound,
	)
	if err != nil {
		return nil, err
	}
	if replayed != nil {
		return replayed, nil
	}

	return s.executeProviderAttempts(ctx, prepared)
}

func (s *Service) executeProviderAttempts(ctx context.Context, prepared *preparedCall) (*llmgwv1.ChatResponse, error) {
	candidateCount := len(prepared.route.models)
	if int64(candidateCount) > int64(effectiveAttemptLimit(prepared.route)) {
		candidateCount = int(effectiveAttemptLimit(prepared.route))
	}
	for candidateIndex := 0; candidateIndex < candidateCount; candidateIndex++ {
		if candidateIndex > 0 {
			if err := s.startAttempt(ctx, prepared, prepared.route.models[candidateIndex], int32(candidateIndex+1)); err != nil {
				return nil, err
			}
		}

		providerCtx, cancel := context.WithTimeout(ctx, prepared.providerRequest.Timeout)
		providerResponse, providerErr := s.provider.Chat(providerCtx, prepared.providerRequest)
		cancel()
		if providerErr != nil {
			var failure *ProviderFailure
			if errors.As(providerErr, &failure) {
				if failure.Retryable && candidateIndex+1 < candidateCount {
					if err := s.finishRetryableAttempt(ctx, prepared, failure); err != nil {
						return nil, err
					}
					continue
				}
				return nil, s.releaseProviderFailure(ctx, prepared, failure)
			}
			if errors.Is(providerErr, ErrProviderNotCharged) {
				return nil, s.releaseUnchargedCall(ctx, prepared)
			}
			return nil, s.markProviderUnknown(ctx, prepared, providerErr)
		}
		if !trustedProviderUsage(providerResponse) {
			return nil, s.markProviderUnknown(
				ctx, prepared, errors.New("provider usage is missing or untrusted"),
			)
		}

		result := providerResult{
			Content:           providerResponse.Content,
			InputTokens:       providerResponse.InputTokens,
			OutputTokens:      providerResponse.OutputTokens,
			ProviderRequestID: providerResponse.ProviderRequestID,
			FinishReason:      providerResponse.FinishReason,
		}
		if result.FinishReason == "" {
			result.FinishReason = "STOP"
		}
		if result.InputTokens < 0 || result.OutputTokens < 0 {
			return nil, s.markProviderUnknown(ctx, prepared, errors.New("provider returned negative usage"))
		}

		actualCostMicroUSD, err := actualCost(prepared.selected, result.InputTokens, result.OutputTokens)
		if err != nil {
			return nil, s.markProviderUnknown(ctx, prepared, err)
		}
		actualTokens, err := checkedAdd(result.InputTokens, result.OutputTokens)
		if err != nil {
			return nil, s.markProviderUnknown(ctx, prepared, err)
		}
		if actualCostMicroUSD > prepared.reservedCost || actualTokens > prepared.reservedTokens {
			return nil, s.markBudgetOverrun(ctx, prepared, result, actualCostMicroUSD)
		}
		if prepared.schema != nil {
			structuredOutput, err := validateStructuredOutput(prepared.schema, result.Content)
			if err != nil {
				return nil, s.settleSchemaFailure(
					ctx, prepared, result, actualCostMicroUSD, actualTokens,
				)
			}
			result.StructuredOutput = structuredOutput
		}

		response, err := s.settleCall(ctx, prepared, result, actualCostMicroUSD, actualTokens)
		if err != nil {
			return nil, err
		}
		s.insertProjection(ctx, prepared, result, actualCostMicroUSD)
		return response, nil
	}
	return nil, newStableError(codes.Internal, 500, "INTERNAL", "route 未配置可用模型")
}

// StreamChat remains wire-compatible but is intentionally not implemented in M5a-01.
func (s *Service) StreamChat(*llmgwv1.ChatRequest, llmgwv1.LLMGateway_StreamChatServer) error {
	return newStableError(codes.Unimplemented, 501, "STREAM_NOT_IMPLEMENTED", "流式治理将在后续增量实现")
}

func normalizeRequest(principalID string, req *llmgwv1.ChatRequest) (governedRequest, error) {
	if req == nil {
		return governedRequest{}, newStableError(codes.InvalidArgument, 400, ReasonRequestInvalid, "请求不能为空")
	}
	if _, err := uuid.Parse(req.GetSessionId()); err != nil {
		return governedRequest{}, newStableError(codes.InvalidArgument, 400, ReasonRequestInvalid, "session_id 无效")
	}
	if _, err := uuid.Parse(req.GetRunId()); err != nil {
		return governedRequest{}, newStableError(codes.InvalidArgument, 400, ReasonRequestInvalid, "run_id 无效")
	}
	if strings.TrimSpace(req.GetRole()) == "" ||
		strings.TrimSpace(req.GetReleaseId()) == "" ||
		strings.TrimSpace(req.GetLogicalCallId()) == "" ||
		strings.TrimSpace(req.GetDataClassification()) == "" {
		return governedRequest{}, newStableError(codes.InvalidArgument, 400, ReasonRequestInvalid, "缺少必要的治理字段")
	}
	if req.GetMaxTokens() < 0 || req.GetTimeoutMs() < 0 ||
		math.IsNaN(req.GetTemperature()) || math.IsInf(req.GetTemperature(), 0) {
		return governedRequest{}, newStableError(codes.InvalidArgument, 400, ReasonRequestInvalid, "模型参数无效")
	}
	messages := make([]ProviderMessage, 0, len(req.GetMessages()))
	for _, message := range req.GetMessages() {
		if message == nil || strings.TrimSpace(message.GetRole()) == "" || message.GetContent() == "" {
			return governedRequest{}, newStableError(codes.InvalidArgument, 400, ReasonRequestInvalid, "消息内容无效")
		}
		if strings.EqualFold(strings.TrimSpace(message.GetRole()), "system") {
			return governedRequest{}, newStableError(
				codes.InvalidArgument, 400, ReasonPromptNotAllowed,
				"system message 只能由服务端注册模板生成",
			)
		}
		messages = append(messages, ProviderMessage{
			Role:    strings.ToLower(strings.TrimSpace(message.GetRole())),
			Content: message.GetContent(),
		})
	}
	traceID := ""
	if req.GetTrace() != nil {
		traceID = req.GetTrace().GetTraceId()
	}
	return governedRequest{
		PrincipalID:        principalID,
		SessionID:          req.GetSessionId(),
		RunID:              req.GetRunId(),
		Role:               req.GetRole(),
		ReleaseID:          req.GetReleaseId(),
		LogicalCallID:      req.GetLogicalCallId(),
		PromptTemplateKey:  req.GetPromptTemplateKey(),
		PromptVersion:      req.GetPromptTemplateVersion(),
		SchemaKey:          req.GetResponseSchemaKey(),
		SchemaVersion:      req.GetResponseSchemaVersion(),
		DataClassification: req.GetDataClassification(),
		Messages:           messages,
		RawSchema:          req.GetJsonSchema(),
		PromptVariables:    req.GetPromptVariables(),
		Temperature:        req.GetTemperature(),
		MaxTokens:          req.GetMaxTokens(),
		TimeoutMS:          req.GetTimeoutMs(),
		TraceID:            traceID,
	}, nil
}

func (s *Service) loadRunBinding(ctx context.Context, req governedRequest) (runBinding, error) {
	var binding runBinding
	err := s.pool.QueryRow(ctx, `
		SELECT release.status, run.active_execution_remaining_ms
		FROM run
		JOIN chat_session session ON session.id = run.session_id
		JOIN release ON release.release_id = run.release_id
		WHERE run.run_id = $1
		  AND run.session_id = $2
		  AND run.principal_id = $3
		  AND session.principal_id = $3
		  AND run.release_id = $4`,
		req.RunID, req.SessionID, req.PrincipalID, req.ReleaseID,
	).Scan(&binding.releaseStatus, &binding.remainingMS)
	if errors.Is(err, pgx.ErrNoRows) {
		return runBinding{}, newStableError(
			codes.PermissionDenied, 403, ReasonRunNotAllowed,
			"session/run 不属于当前主体或 release 不匹配",
		)
	}
	if err != nil {
		return runBinding{}, newStableError(codes.Internal, 500, "INTERNAL", "读取 run 身份失败")
	}
	switch binding.releaseStatus {
	case "PUBLISHED":
		return binding, nil
	case "REVOKED":
		return runBinding{}, newStableError(codes.FailedPrecondition, 412, ReasonReleaseRevoked, "release 已撤销")
	default:
		return runBinding{}, newStableError(codes.FailedPrecondition, 412, ReasonReleaseUnavailable, "release 尚未发布")
	}
}

func (s *Service) loadRoute(ctx context.Context, req governedRequest, binding runBinding) (routeConfig, error) {
	var (
		route       routeConfig
		fallbacks   []string
		effectiveAt time.Time
		publishedAt *time.Time
	)
	err := s.pool.QueryRow(ctx, `
		SELECT route.route_version,
		       route.primary_model_id,
		       route.fallback_model_ids,
		       route.max_output_tokens,
		       route.timeout_ms,
		       route.max_attempts,
		       route.requires_response_schema,
		       route.price_table_version,
		       price.effective_at,
		       price.published_at
		FROM llm_role_route route
		JOIN llm_price_table price
		  ON price.price_table_version = route.price_table_version
		WHERE route.release_id = $1 AND route.role = $2`,
		req.ReleaseID, req.Role,
	).Scan(
		&route.version,
		&route.primaryModel,
		&fallbacks,
		&route.maxOutput,
		&route.timeoutMS,
		&route.maxAttempts,
		&route.requiresResponseSchema,
		&route.priceVersion,
		&effectiveAt,
		&publishedAt,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return routeConfig{}, newStableError(codes.InvalidArgument, 400, ReasonRoleNotAllowed, "角色未在当前 release 发布")
	}
	if err != nil {
		return routeConfig{}, newStableError(codes.Internal, 500, "INTERNAL", "读取角色路由失败")
	}
	if publishedAt == nil || effectiveAt.After(s.now()) {
		return routeConfig{}, newStableError(
			codes.FailedPrecondition, 412, ReasonPriceUnavailable,
			"route 绑定的价格版本尚未发布或未生效",
		)
	}

	modelIDs := append([]string{route.primaryModel}, fallbacks...)
	seen := make(map[string]struct{}, len(modelIDs))
	for _, modelID := range modelIDs {
		if _, duplicated := seen[modelID]; duplicated {
			return routeConfig{}, newStableError(
				codes.FailedPrecondition, 412, ReasonModelNotAllowed,
				"route 模型顺序包含重复项",
			)
		}
		seen[modelID] = struct{}{}
		var candidate modelPrice
		candidate.modelID = modelID
		err := s.pool.QueryRow(ctx, `
			SELECT allow.provider,
			       price.input_micro_usd_per_million_tokens,
			       price.output_micro_usd_per_million_tokens
			FROM llm_model_allowlist allow
			JOIN llm_price_entry price
			  ON price.model_id = allow.model_id
			 AND price.price_table_version = $3
			WHERE allow.release_id = $1
			  AND allow.model_id = $2
			  AND allow.enabled`,
			req.ReleaseID, modelID, route.priceVersion,
		).Scan(&candidate.provider, &candidate.inputRate, &candidate.outputRate)
		if errors.Is(err, pgx.ErrNoRows) {
			return routeConfig{}, s.classifyModelPriceFailure(ctx, req.ReleaseID, modelID)
		}
		if err != nil {
			return routeConfig{}, newStableError(codes.Internal, 500, "INTERNAL", "读取模型价格失败")
		}
		route.models = append(route.models, candidate)
	}

	maxTokens := req.MaxTokens
	if maxTokens == 0 {
		maxTokens = route.maxOutput
	}
	if maxTokens > route.maxOutput {
		return routeConfig{}, newStableError(codes.InvalidArgument, 400, ReasonLimitExceeded, "max_tokens 超过角色上限")
	}
	timeoutMS := req.TimeoutMS
	if timeoutMS == 0 {
		timeoutMS = route.timeoutMS
	}
	if timeoutMS > route.timeoutMS || int64(timeoutMS) > binding.remainingMS {
		return routeConfig{}, newStableError(
			codes.InvalidArgument, 400, ReasonLimitExceeded,
			"timeout_ms 超过服务端或 run 剩余时间",
		)
	}
	route.maxOutput = maxTokens
	route.timeoutMS = timeoutMS
	return route, nil
}

func (s *Service) classifyModelPriceFailure(ctx context.Context, releaseID, modelID string) error {
	var allowed bool
	err := s.pool.QueryRow(ctx, `
		SELECT EXISTS (
			SELECT 1
			FROM llm_model_allowlist
			WHERE release_id = $1 AND model_id = $2 AND enabled
		)`,
		releaseID, modelID,
	).Scan(&allowed)
	if err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "校验模型白名单失败")
	}
	if !allowed {
		return newStableError(codes.FailedPrecondition, 412, ReasonModelNotAllowed, "route 模型不在服务端白名单")
	}
	return newStableError(codes.FailedPrecondition, 412, ReasonPriceUnavailable, "route 模型在价格版本中缺失")
}

func (s *Service) loadPromptTemplate(ctx context.Context, req governedRequest) (promptTemplate, error) {
	key := strings.TrimSpace(req.PromptTemplateKey)
	version := strings.TrimSpace(req.PromptVersion)
	if key == "" || version == "" {
		return promptTemplate{}, newStableError(
			codes.InvalidArgument, 400, ReasonPromptNotAllowed,
			"prompt template key/version 未注册或未指定",
		)
	}
	var (
		template promptTemplate
		status   string
	)
	err := s.pool.QueryRow(ctx, `
		SELECT prompt_template_key, prompt_template_version,
		       system_template, user_template, template_sha256, status
		FROM llm_prompt_template
		WHERE release_id = $1
		  AND prompt_template_key = $2
		  AND prompt_template_version = $3`,
		req.ReleaseID, key, version,
	).Scan(
		&template.key, &template.version, &template.system,
		&template.user, &template.digest, &status,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return promptTemplate{}, newStableError(
			codes.InvalidArgument, 400, ReasonPromptNotAllowed,
			"prompt template 未注册、已撤销或版本不受支持",
		)
	}
	if err != nil {
		return promptTemplate{}, newStableError(codes.Internal, 500, "INTERNAL", "读取 prompt 模板失败")
	}
	if status != "ACTIVE" {
		return promptTemplate{}, newStableError(
			codes.InvalidArgument, 400, ReasonPromptNotAllowed,
			"prompt template 未注册、已撤销或版本不受支持",
		)
	}
	expectedDigest := promptTemplateDigest(template.system, template.user)
	if template.digest != expectedDigest {
		return promptTemplate{}, newStableError(
			codes.InvalidArgument, 400, ReasonPromptNotAllowed,
			"prompt template 内容校验失败",
		)
	}
	systemContent, err := renderPromptTemplate(template.system, req.PromptVariables)
	if err != nil {
		return promptTemplate{}, err
	}
	userContent, err := renderPromptTemplate(template.user, req.PromptVariables)
	if err != nil {
		return promptTemplate{}, err
	}
	template.messages = make([]ProviderMessage, 0, len(req.Messages)+2)
	template.messages = append(template.messages, ProviderMessage{
		Role: "system", Content: systemContent,
	})
	if strings.TrimSpace(userContent) != "" {
		template.messages = append(template.messages, ProviderMessage{
			Role: "user", Content: userContent,
		})
	}
	template.messages = append(template.messages, req.Messages...)
	return template, nil
}

func promptTemplateDigest(systemTemplate, userTemplate string) string {
	sum := sha256.Sum256([]byte(systemTemplate + "\x00" + userTemplate))
	return hex.EncodeToString(sum[:])
}

func schemaDocument(schema *responseSchema) []byte {
	if schema == nil {
		return nil
	}
	return append([]byte(nil), schema.document...)
}

func schemaKey(schema *responseSchema) string {
	if schema == nil {
		return ""
	}
	return schema.key
}

func schemaVersion(schema *responseSchema) string {
	if schema == nil {
		return ""
	}
	return schema.version
}

func schemaDigest(schema *responseSchema) string {
	if schema == nil {
		return ""
	}
	return schema.digest
}

func nullableText(value string) any {
	if value == "" {
		return nil
	}
	return value
}

var promptVariablePattern = regexp.MustCompile(`\{\{([\pL_][\pL\pN_]*)\}\}`)

func renderPromptTemplate(template string, variables map[string]string) (string, error) {
	if template == "" {
		return "", nil
	}
	unresolvedSyntax := promptVariablePattern.ReplaceAllString(template, "")
	var missing string
	rendered := promptVariablePattern.ReplaceAllStringFunc(template, func(placeholder string) string {
		name := promptVariablePattern.FindStringSubmatch(placeholder)[1]
		value, ok := variables[name]
		if !ok {
			missing = name
			return placeholder
		}
		return value
	})
	if missing != "" || strings.Contains(unresolvedSyntax, "{{") || strings.Contains(unresolvedSyntax, "}}") {
		detail := "prompt template 变量缺失或格式无效"
		if missing != "" {
			detail = "prompt template 缺少变量 " + missing
		}
		return "", newStableError(codes.InvalidArgument, 400, ReasonPromptNotAllowed, detail)
	}
	return rendered, nil
}

func canonicalHash(
	req governedRequest,
	route routeConfig,
	prompt promptTemplate,
	schema *responseSchema,
) (string, int64, error) {
	models := make([]string, 0, len(route.models))
	for _, model := range route.models {
		models = append(models, model.modelID)
	}
	canonical := canonicalRequest{
		Role:               req.Role,
		Messages:           req.Messages,
		PromptTemplateKey:  req.PromptTemplateKey,
		PromptVersion:      req.PromptVersion,
		SchemaKey:          req.SchemaKey,
		SchemaVersion:      req.SchemaVersion,
		DataClassification: req.DataClassification,
		Temperature:        req.Temperature,
		MaxOutputTokens:    route.maxOutput,
		TimeoutMS:          route.timeoutMS,
		ReleaseID:          req.ReleaseID,
		RouteVersion:       route.version,
		Models:             models,
		PromptDigest:       prompt.digest,
	}
	if schema != nil {
		canonical.SchemaDigest = schema.digest
	}
	encoded, err := json.Marshal(canonical)
	if err != nil {
		return "", 0, newStableError(codes.Internal, 500, "INTERNAL", "规范化请求失败")
	}
	sum := sha256.Sum256(encoded)
	return hex.EncodeToString(sum[:]), int64(len(encoded)), nil
}

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

	budgetScopeID := s.newID()
	if _, err := tx.Exec(ctx, `
		INSERT INTO llm_budget_scope (budget_scope_id, principal_id, session_id)
		VALUES ($1, $2, $3)
		ON CONFLICT (principal_id, session_id) DO NOTHING`,
		budgetScopeID, req.PrincipalID, req.SessionID,
	); err != nil {
		return nil, nil, newStableError(codes.Internal, 500, "INTERNAL", "初始化预算作用域失败")
	}
	var budgetStatus string
	if err := tx.QueryRow(ctx, `
		SELECT budget_scope_id::text, budget_status
		FROM llm_budget_scope
		WHERE principal_id = $1 AND session_id = $2`,
		req.PrincipalID, req.SessionID,
	).Scan(&budgetScopeID, &budgetStatus); err != nil {
		return nil, nil, newStableError(codes.Internal, 500, "INTERNAL", "读取预算作用域失败")
	}
	if budgetStatus == "BUDGET_OVERRUN" {
		return nil, nil, newStableError(
			codes.ResourceExhausted, 429, ReasonBudgetOverrun,
			"预算作用域已因实际用量超预留而停止后续模型调用",
		)
	}

	_, err = tx.Exec(ctx, `
		INSERT INTO llm_logical_call (
			logical_call_pk, ledger_entry_id, logical_call_id, principal_id,
			session_id, run_id, role, release_id, route_version,
			price_table_version, model_id, request_hash, budget_scope_id,
			status, reserved_input_tokens, reserved_output_tokens,
			reserved_cost_micro_usd, reserved_provider_tokens,
			prompt_template_key,
			prompt_template_version, prompt_template_sha256,
			response_schema_key, response_schema_version,
			response_schema_sha256
		) VALUES (
			$1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
			'IN_PROGRESS', $14, $15, $16, $17, $18, $19, $20, $21, $22, $23
		)`,
		prepared.logicalCallPK, prepared.ledgerEntryID, req.LogicalCallID,
		req.PrincipalID, req.SessionID, req.RunID, req.Role, req.ReleaseID,
		route.version, route.priceVersion, prepared.selected.modelID,
		requestHash, budgetScopeID, inputBound, outputBound, reservedCost,
		reservedTokens,
		prompt.key, prompt.version, prompt.digest,
		nullableText(schemaKey(schema)), nullableText(schemaVersion(schema)),
		nullableText(schemaDigest(schema)),
	)
	if isUniqueViolation(err) {
		_ = tx.Rollback(ctx)
		return s.resolveExistingCall(ctx, req, requestHash)
	}
	if err != nil {
		return nil, nil, newStableError(codes.Internal, 500, "INTERNAL", "创建逻辑调用账本失败")
	}

	tag, err := tx.Exec(ctx, `
		UPDATE llm_budget_scope
		SET provider_tokens_reserved = provider_tokens_reserved + $1,
		    cost_reserved_micro_usd = cost_reserved_micro_usd + $2,
		    updated_at = NOW()
		WHERE budget_scope_id = $3
		  AND budget_status = 'ACTIVE'
		  AND provider_tokens_settled + provider_tokens_reserved + $1 <= token_limit
		  AND cost_settled_micro_usd + cost_reserved_micro_usd + $2 <= cost_limit_micro_usd`,
		reservedTokens, reservedCost, budgetScopeID,
	)
	if err != nil {
		return nil, nil, newStableError(codes.Internal, 500, "INTERNAL", "原子预占预算失败")
	}
	if tag.RowsAffected() != 1 {
		if err := tx.QueryRow(ctx, `
			SELECT budget_status
			FROM llm_budget_scope
			WHERE budget_scope_id = $1`,
			budgetScopeID,
		).Scan(&budgetStatus); err != nil {
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

	if _, err := tx.Exec(ctx, `
		INSERT INTO llm_provider_attempt (
			attempt_id, logical_call_pk, logical_call_id, attempt_no,
			model_id, provider, status, reserved_tokens,
			reserved_cost_micro_usd, started_at
		) VALUES ($1, $2, $3, 1, $4, $5, 'IN_PROGRESS', $6, $7, $8)`,
		prepared.attemptID, prepared.logicalCallPK, req.LogicalCallID,
		prepared.selected.modelID, prepared.selected.provider,
		attemptTokens, firstAttemptCost, prepared.attemptStartedAt,
	); err != nil {
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
	if _, err := s.pool.Exec(ctx, `
		INSERT INTO llm_provider_attempt (
			attempt_id, logical_call_pk, logical_call_id, attempt_no,
			model_id, provider, status, reserved_tokens,
			reserved_cost_micro_usd, started_at
		) VALUES ($1, $2, $3, $4, $5, $6, 'IN_PROGRESS', $7, $8, $9)`,
		attemptID, prepared.logicalCallPK, prepared.logicalCallID, attemptNo,
		model.modelID, model.provider, attemptTokens, attemptCost, startedAt,
	); err != nil {
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
	tag, err := s.pool.Exec(ctx, `
		UPDATE llm_provider_attempt
		SET status = 'FAILED_NO_CHARGE',
		    cost_micro_usd = 0,
		    provider_request_id = NULLIF($1, ''),
		    error_code = $2,
		    finished_at = $3
		WHERE attempt_id = $4 AND status = 'IN_PROGRESS'`,
		failure.ProviderRequestID, failure.Code, finishedAt, prepared.attemptID,
	)
	if err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "记录可重试供应商失败失败")
	}
	if tag.RowsAffected() != 1 {
		return newStableError(codes.Aborted, 409, ReasonCallInProgress, "attempt 状态不允许可重试失败结算")
	}
	return nil
}

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

	var currentStatus string
	if err := tx.QueryRow(ctx, `
		SELECT status
		FROM llm_logical_call
		WHERE logical_call_pk = $1
		FOR UPDATE`,
		prepared.logicalCallPK,
	).Scan(&currentStatus); err != nil {
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
	tag, err := tx.Exec(ctx, `
		UPDATE llm_budget_scope
		SET provider_tokens_reserved = provider_tokens_reserved - $1,
		    cost_reserved_micro_usd = cost_reserved_micro_usd - $2,
		    provider_tokens_settled = provider_tokens_settled + $3,
		    cost_settled_micro_usd = cost_settled_micro_usd + $4,
		    updated_at = $5
		WHERE budget_scope_id = (
			SELECT budget_scope_id
			FROM llm_logical_call
			WHERE logical_call_pk = $6
		)
		  AND provider_tokens_reserved >= $1
		  AND cost_reserved_micro_usd >= $2`,
		prepared.reservedTokens, prepared.reservedCost,
		actualTokens, actualCostMicroUSD, finishedAt, prepared.logicalCallPK,
	)
	if err != nil {
		return nil, newStableError(codes.Internal, 500, "INTERNAL", "释放预算差额失败")
	}
	if tag.RowsAffected() != 1 {
		return nil, newStableError(codes.Internal, 500, "INTERNAL", "预算预留状态不一致")
	}

	tag, err = tx.Exec(ctx, `
		UPDATE llm_provider_attempt
		SET status = 'SUCCEEDED',
		    input_tokens = $1,
		    output_tokens = $2,
		    cost_micro_usd = $3,
		    provider_request_id = NULLIF($4, ''),
		    error_code = NULL,
		    finished_at = $5
		WHERE attempt_id = $6 AND status = 'IN_PROGRESS'`,
		result.InputTokens, result.OutputTokens, actualCostMicroUSD,
		result.ProviderRequestID, finishedAt, prepared.attemptID,
	)
	if err != nil {
		return nil, newStableError(codes.Internal, 500, "INTERNAL", "结算供应商 attempt 失败")
	}
	if tag.RowsAffected() != 1 {
		return nil, newStableError(codes.Aborted, 409, ReasonCallInProgress, "attempt 状态不允许成功结算")
	}
	tag, err = tx.Exec(ctx, `
		UPDATE llm_logical_call
		SET status = 'SUCCEEDED',
		    actual_input_tokens = $1,
		    actual_output_tokens = $2,
		    actual_cost_micro_usd = $3,
		    response_payload = $4,
		    completed_at = $5,
		    updated_at = $5
		WHERE logical_call_pk = $6 AND status = 'IN_PROGRESS'`,
		result.InputTokens, result.OutputTokens, actualCostMicroUSD,
		payload, finishedAt, prepared.logicalCallPK,
	)
	if err != nil {
		return nil, newStableError(codes.Internal, 500, "INTERNAL", "提交逻辑调用结果失败")
	}
	if tag.RowsAffected() != 1 {
		return nil, newStableError(codes.Aborted, 409, ReasonCallInProgress, "逻辑调用状态不允许成功结算")
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, newStableError(codes.Internal, 500, "INTERNAL", "提交预算结算失败")
	}
	attempts, err := s.loadAttemptSummaries(ctx, prepared.logicalCallPK)
	if err != nil {
		return nil, err
	}
	return buildResponse(prepared, result, actualCostMicroUSD, attempts), nil
}

func (s *Service) settleSchemaFailure(
	ctx context.Context,
	prepared *preparedCall,
	result providerResult,
	actualCostMicroUSD int64,
	actualTokens int64,
) error {
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "启动 schema 失败结算事务失败")
	}
	defer tx.Rollback(ctx)

	var currentStatus string
	if err := tx.QueryRow(ctx, `
		SELECT status
		FROM llm_logical_call
		WHERE logical_call_pk = $1
		FOR UPDATE`,
		prepared.logicalCallPK,
	).Scan(&currentStatus); err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "读取 schema 失败调用状态失败")
	}
	if currentStatus != "IN_PROGRESS" {
		return newStableError(codes.Aborted, 409, ReasonCallInProgress, "逻辑调用已完成或正在结算")
	}

	finishedAt := s.now()
	tag, err := tx.Exec(ctx, `
		UPDATE llm_budget_scope
		SET provider_tokens_reserved = provider_tokens_reserved - $1,
		    cost_reserved_micro_usd = cost_reserved_micro_usd - $2,
		    provider_tokens_settled = provider_tokens_settled + $3,
		    cost_settled_micro_usd = cost_settled_micro_usd + $4,
		    updated_at = $5
		WHERE budget_scope_id = (
			SELECT budget_scope_id
			FROM llm_logical_call
			WHERE logical_call_pk = $6
		)
		  AND provider_tokens_reserved >= $1
		  AND cost_reserved_micro_usd >= $2`,
		prepared.reservedTokens, prepared.reservedCost,
		actualTokens, actualCostMicroUSD, finishedAt, prepared.logicalCallPK,
	)
	if err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "释放 schema 失败预算差额失败")
	}
	if tag.RowsAffected() != 1 {
		return newStableError(codes.Internal, 500, "INTERNAL", "预算预留状态不一致")
	}

	tag, err = tx.Exec(ctx, `
		UPDATE llm_provider_attempt
		SET status = 'SUCCEEDED',
		    input_tokens = $1,
		    output_tokens = $2,
		    cost_micro_usd = $3,
		    provider_request_id = NULLIF($4, ''),
		    error_code = $5,
		    finished_at = $6
		WHERE attempt_id = $7 AND status = 'IN_PROGRESS'`,
		result.InputTokens, result.OutputTokens, actualCostMicroUSD,
		result.ProviderRequestID, ReasonSchemaValidationFailed, finishedAt, prepared.attemptID,
	)
	if err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "结算 schema 失败 attempt 失败")
	}
	if tag.RowsAffected() != 1 {
		return newStableError(codes.Aborted, 409, ReasonCallInProgress, "attempt 状态不允许 schema 失败结算")
	}

	tag, err = tx.Exec(ctx, `
		UPDATE llm_logical_call
		SET status = 'FAILED',
		    actual_input_tokens = $1,
		    actual_output_tokens = $2,
		    actual_cost_micro_usd = $3,
		    response_payload = NULL,
		    error_reason = $4,
		    completed_at = $5,
		    updated_at = $5
		WHERE logical_call_pk = $6 AND status = 'IN_PROGRESS'`,
		result.InputTokens, result.OutputTokens, actualCostMicroUSD,
		ReasonSchemaValidationFailed, finishedAt, prepared.logicalCallPK,
	)
	if err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "提交 schema 失败逻辑调用失败")
	}
	if tag.RowsAffected() != 1 {
		return newStableError(codes.Aborted, 409, ReasonCallInProgress, "逻辑调用状态不允许 schema 失败结算")
	}
	if err := tx.Commit(ctx); err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "提交 schema 失败结算失败")
	}
	s.insertFailedProjection(ctx, prepared, result, actualCostMicroUSD)
	return newStableError(
		codes.FailedPrecondition, 412, ReasonSchemaValidationFailed,
		"供应商输出不符合已注册 response schema",
	)
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
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "启动免计费释放事务失败")
	}
	defer tx.Rollback(ctx)

	var currentStatus string
	if err := tx.QueryRow(ctx, `
		SELECT status
		FROM llm_logical_call
		WHERE logical_call_pk = $1
		FOR UPDATE`,
		prepared.logicalCallPK,
	).Scan(&currentStatus); err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "读取免计费调用状态失败")
	}
	if currentStatus != "IN_PROGRESS" {
		return newStableError(codes.Aborted, 409, ReasonCallInProgress, "逻辑调用已完成或正在结算")
	}

	finishedAt := s.now()
	tag, err := tx.Exec(ctx, `
		UPDATE llm_budget_scope
		SET provider_tokens_reserved = provider_tokens_reserved - $1,
		    cost_reserved_micro_usd = cost_reserved_micro_usd - $2,
		    updated_at = $3
		WHERE budget_scope_id = (
			SELECT budget_scope_id
			FROM llm_logical_call
			WHERE logical_call_pk = $4
		)
		  AND provider_tokens_reserved >= $1
		  AND cost_reserved_micro_usd >= $2`,
		prepared.reservedTokens, prepared.reservedCost, finishedAt, prepared.logicalCallPK,
	)
	if err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "释放免计费预算失败")
	}
	if tag.RowsAffected() != 1 {
		return newStableError(codes.Internal, 500, "INTERNAL", "预算预留状态不一致")
	}
	tag, err = tx.Exec(ctx, `
		UPDATE llm_provider_attempt
		SET status = 'FAILED_NO_CHARGE',
		    cost_micro_usd = 0,
		    provider_request_id = NULLIF($1, ''),
		    error_code = $2,
		    finished_at = $3
		WHERE attempt_id = $4 AND status = 'IN_PROGRESS'`,
		providerRequestID, attemptErrorCode, finishedAt, prepared.attemptID,
	)
	if err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "记录免计费 attempt 失败")
	}
	if tag.RowsAffected() != 1 {
		return newStableError(codes.Aborted, 409, ReasonCallInProgress, "attempt 状态不允许免计费结算")
	}
	tag, err = tx.Exec(ctx, `
		UPDATE llm_logical_call
		SET status = 'FAILED',
		    error_reason = $1,
		    completed_at = $2,
		    updated_at = $2
		WHERE logical_call_pk = $3 AND status = 'IN_PROGRESS'`,
		attemptErrorCode, finishedAt, prepared.logicalCallPK,
	)
	if err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "提交免计费逻辑调用失败")
	}
	if tag.RowsAffected() != 1 {
		return newStableError(codes.Aborted, 409, ReasonCallInProgress, "逻辑调用状态不允许免计费结算")
	}
	if err := tx.Commit(ctx); err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "提交免计费释放失败")
	}
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

	var currentStatus string
	if err := tx.QueryRow(ctx, `
		SELECT status
		FROM llm_logical_call
		WHERE logical_call_pk = $1
		FOR UPDATE`,
		prepared.logicalCallPK,
	).Scan(&currentStatus); err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "读取预算超限调用状态失败")
	}
	if currentStatus != "IN_PROGRESS" {
		return newStableError(codes.Aborted, 409, ReasonCallInProgress, "逻辑调用已完成或正在结算")
	}

	finishedAt := s.now()
	tag, err := tx.Exec(ctx, `
		UPDATE llm_budget_scope
		SET budget_status = 'BUDGET_OVERRUN',
		    overrun_at = COALESCE(overrun_at, $1),
		    overrun_reason = 'ACTUAL_USAGE_EXCEEDS_RESERVATION',
		    updated_at = $1
		WHERE budget_scope_id = (
			SELECT budget_scope_id
			FROM llm_logical_call
			WHERE logical_call_pk = $2
		)`,
		finishedAt, prepared.logicalCallPK,
	)
	if err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "封锁预算超限作用域失败")
	}
	if tag.RowsAffected() != 1 {
		return newStableError(codes.Internal, 500, "INTERNAL", "预算作用域状态不一致")
	}

	payload, err := json.Marshal(result)
	if err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "序列化预算超限结果失败")
	}
	tag, err = tx.Exec(ctx, `
		UPDATE llm_provider_attempt
		SET status = 'PENDING_RECONCILIATION',
		    input_tokens = $1,
		    output_tokens = $2,
		    cost_micro_usd = $3,
		    provider_request_id = NULLIF($4, ''),
		    error_code = $5,
		    finished_at = $6
		WHERE attempt_id = $7 AND status = 'IN_PROGRESS'`,
		result.InputTokens, result.OutputTokens, actualCostMicroUSD,
		result.ProviderRequestID, ReasonBudgetOverrun, finishedAt, prepared.attemptID,
	)
	if err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "记录预算超限 attempt 失败")
	}
	if tag.RowsAffected() != 1 {
		return newStableError(codes.Aborted, 409, ReasonCallInProgress, "attempt 状态不允许预算超限结算")
	}

	tag, err = tx.Exec(ctx, `
		UPDATE llm_logical_call
		SET status = 'BUDGET_OVERRUN',
		    actual_input_tokens = $1,
		    actual_output_tokens = $2,
		    actual_cost_micro_usd = $3,
		    response_payload = $4,
		    error_reason = $5,
		    updated_at = $6
		WHERE logical_call_pk = $7 AND status = 'IN_PROGRESS'`,
		result.InputTokens, result.OutputTokens, actualCostMicroUSD,
		payload, ReasonBudgetOverrun, finishedAt, prepared.logicalCallPK,
	)
	if err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "记录预算超限逻辑调用失败")
	}
	if tag.RowsAffected() != 1 {
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
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "记录待对账状态失败")
	}
	defer tx.Rollback(ctx)
	finishedAt := s.now()
	tag, err := tx.Exec(ctx, `
		UPDATE llm_provider_attempt
		SET status = 'PENDING_RECONCILIATION',
		    error_code = $1,
		    finished_at = $2
		WHERE attempt_id = $3 AND status IN ('RESERVED', 'IN_PROGRESS')`,
		errorCode, finishedAt, prepared.attemptID,
	)
	if err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "更新未知计费 attempt 失败")
	}
	if tag.RowsAffected() != 1 {
		return newStableError(codes.Aborted, 409, ReasonCallInProgress, "attempt 状态不允许进入待对账")
	}
	tag, err = tx.Exec(ctx, `
		UPDATE llm_logical_call
		SET status = 'PARTIAL_UNKNOWN',
		    error_reason = $1,
		    updated_at = $2
		WHERE logical_call_pk = $3 AND status = 'IN_PROGRESS'`,
		errorCode, finishedAt, prepared.logicalCallPK,
	)
	if err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "更新待对账逻辑调用失败")
	}
	if tag.RowsAffected() != 1 {
		return newStableError(codes.Aborted, 409, ReasonCallInProgress, "逻辑调用状态不允许进入待对账")
	}
	if err := tx.Commit(ctx); err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "提交待对账状态失败")
	}
	message := "供应商请求可能已计费，预算预留保持待对账"
	if errorCode == ReasonProviderTimeout {
		message = "供应商请求超时且费用未知，预算预留保持待对账"
	}
	return newStableErrorWithMetadata(
		codes.Unavailable, 503, errorCode, message,
		reconciliationMetadata(prepared.logicalCallID, prepared.attemptID, prepared.ledgerEntryID),
	)
}

func (s *Service) resolveExistingCall(
	ctx context.Context,
	req governedRequest,
	requestHash string,
) (*preparedCall, *llmgwv1.ChatResponse, error) {
	var (
		callPK         string
		ledgerID       string
		storedHash     string
		status         string
		errorReason    *string
		routeVersion   string
		priceVersion   string
		modelID        string
		promptKey      *string
		promptVersion  *string
		promptDigest   *string
		schemaKey      *string
		schemaVersion  *string
		schemaDigest   *string
		reservedCost   int64
		reservedTokens int64
		actualIn       *int64
		actualOut      *int64
		actualCost     *int64
		resultPayload  []byte
		attemptID      string
	)
	err := s.pool.QueryRow(ctx, `
		SELECT logical_call_pk::text,
		       ledger_entry_id::text,
		       request_hash,
		       status,
		       error_reason,
		       route_version,
		       price_table_version,
		       model_id,
		       prompt_template_key,
		       prompt_template_version,
		       prompt_template_sha256,
		       response_schema_key,
		       response_schema_version,
		       response_schema_sha256,
		       reserved_cost_micro_usd,
		       COALESCE(reserved_provider_tokens, reserved_input_tokens + reserved_output_tokens),
		       actual_input_tokens,
		       actual_output_tokens,
		       actual_cost_micro_usd,
		       response_payload,
		       COALESCE((
		           SELECT attempt.attempt_id::text
		           FROM llm_provider_attempt attempt
		           WHERE attempt.logical_call_pk = call.logical_call_pk
		           ORDER BY attempt.attempt_no DESC
		           LIMIT 1
		       ), '')
		FROM llm_logical_call call
		WHERE principal_id = $1
		  AND session_id = $2
		  AND logical_call_id = $3`,
		req.PrincipalID, req.SessionID, req.LogicalCallID,
	).Scan(
		&callPK, &ledgerID, &storedHash, &status, &errorReason, &routeVersion,
		&priceVersion, &modelID, &promptKey, &promptVersion, &promptDigest,
		&schemaKey, &schemaVersion, &schemaDigest, &reservedCost, &reservedTokens,
		&actualIn, &actualOut, &actualCost, &resultPayload, &attemptID,
	)
	if err != nil {
		return nil, nil, newStableError(codes.Internal, 500, "INTERNAL", "读取逻辑调用幂等状态失败")
	}
	if storedHash != requestHash {
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
	if err := json.Unmarshal(resultPayload, &result); err != nil {
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
			version:      routeVersion,
			priceVersion: priceVersion,
		},
		selected:       modelPrice{modelID: modelID},
		reservedTokens: reservedTokens,
		reservedCost:   reservedCost,
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
	rows, err := s.pool.Query(ctx, `
		SELECT attempt_id::text, model_id, provider, status,
		       input_tokens, output_tokens, cost_micro_usd,
		       provider_request_id, error_code, started_at, finished_at
		FROM llm_provider_attempt
		WHERE logical_call_pk = $1
		ORDER BY attempt_no`,
		logicalCallPK,
	)
	if err != nil {
		return nil, newStableError(codes.Internal, 500, "INTERNAL", "读取供应商 attempt 失败")
	}
	defer rows.Close()

	var attempts []*llmgwv1.AttemptSummary
	for rows.Next() {
		var (
			attempt                                 llmgwv1.AttemptSummary
			inputTokens, outputTokens, costMicroUSD *int64
			providerRequestID, errorCode            *string
			startedAt, finishedAt                   *time.Time
		)
		if err := rows.Scan(
			&attempt.AttemptId,
			&attempt.ModelId,
			&attempt.Provider,
			&attempt.Status,
			&inputTokens,
			&outputTokens,
			&costMicroUSD,
			&providerRequestID,
			&errorCode,
			&startedAt,
			&finishedAt,
		); err != nil {
			return nil, newStableError(codes.Internal, 500, "INTERNAL", "解析供应商 attempt 失败")
		}
		if inputTokens != nil || outputTokens != nil {
			attempt.Usage = &llmgwv1.Usage{}
			if inputTokens != nil {
				attempt.Usage.InputTokens = *inputTokens
			}
			if outputTokens != nil {
				attempt.Usage.OutputTokens = *outputTokens
			}
		}
		if costMicroUSD != nil {
			attempt.CostMicroUsd = *costMicroUSD
		}
		attempt.ProviderRequestId = valueOrEmpty(providerRequestID)
		attempt.ErrorCode = valueOrEmpty(errorCode)
		if startedAt != nil && finishedAt != nil {
			attempt.LatencyMs = finishedAt.Sub(*startedAt).Milliseconds()
		}
		attempts = append(attempts, &attempt)
	}
	if err := rows.Err(); err != nil {
		return nil, newStableError(codes.Internal, 500, "INTERNAL", "遍历供应商 attempt 失败")
	}
	return attempts, nil
}

func (s *Service) insertProjection(
	ctx context.Context,
	prepared *preparedCall,
	result providerResult,
	actualCostMicroUSD int64,
) {
	_, _ = s.pool.Exec(ctx, `
		INSERT INTO llm_call_log (
			trace_id, session_id, node, role, model, prompt_hash,
			in_tokens, out_tokens, cost, latency_ms, status, error_message,
			logical_call_id, ledger_entry_id, attempt_id, route_version,
			price_table_version, cost_micro_usd, settlement_status
		) VALUES (
			NULLIF($1, ''), $2, $3, $3, $4, $5,
			$6, $7, $8, $9, 'success', NULL,
			$10, $11, $12, $13, $14, $15, 'SETTLED'
		)`,
		prepared.traceID, prepared.sessionID, prepared.role,
		prepared.selected.modelID, prepared.requestHash,
		result.InputTokens, result.OutputTokens, float64(actualCostMicroUSD)/1_000_000,
		time.Since(prepared.startedAt).Milliseconds(), prepared.logicalCallID,
		prepared.ledgerEntryID, prepared.attemptID, prepared.route.version,
		prepared.route.priceVersion, actualCostMicroUSD,
	)
}

func (s *Service) insertFailedProjection(
	ctx context.Context,
	prepared *preparedCall,
	result providerResult,
	actualCostMicroUSD int64,
) {
	_, _ = s.pool.Exec(ctx, `
		INSERT INTO llm_call_log (
			trace_id, session_id, node, role, model, prompt_hash,
			in_tokens, out_tokens, cost, latency_ms, status, error_message,
			logical_call_id, ledger_entry_id, attempt_id, route_version,
			price_table_version, cost_micro_usd, settlement_status
		) VALUES (
			NULLIF($1, ''), $2, $3, $3, $4, $5,
			$6, $7, $8, $9, 'fail', $10,
			$11, $12, $13, $14, $15, $16, 'SETTLED'
		)`,
		prepared.traceID, prepared.sessionID, prepared.role,
		prepared.selected.modelID, prepared.requestHash,
		result.InputTokens, result.OutputTokens, float64(actualCostMicroUSD)/1_000_000,
		time.Since(prepared.startedAt).Milliseconds(), ReasonSchemaValidationFailed,
		prepared.logicalCallID, prepared.ledgerEntryID, prepared.attemptID,
		prepared.route.version, prepared.route.priceVersion, actualCostMicroUSD,
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
			ReservedMicroUsd: prepared.reservedCost,
			ActualMicroUsd:   actualCostMicroUSD,
			SettlementStatus: "SETTLED",
		},
		Attempts:              attempts,
		LedgerEntryId:         prepared.ledgerEntryID,
		RouteVersion:          prepared.route.version,
		PriceTableVersion:     prepared.route.priceVersion,
		LogicalCallId:         prepared.logicalCallID,
		CacheHit:              false,
		FinishReason:          result.FinishReason,
		ReleaseId:             prepared.releaseID,
		StructuredOutput:      structuredOutput,
		PromptTemplateKey:     prepared.prompt.key,
		PromptTemplateVersion: prepared.prompt.version,
		ResponseSchemaKey:     schemaKey(prepared.schema),
		ResponseSchemaVersion: schemaVersion(prepared.schema),
	}
}

func actualCost(model modelPrice, inputTokens, outputTokens int64) (int64, error) {
	inputCost, err := costForTokens(inputTokens, model.inputRate)
	if err != nil {
		return 0, err
	}
	outputCost, err := costForTokens(outputTokens, model.outputRate)
	if err != nil {
		return 0, err
	}
	return checkedAdd(inputCost, outputCost)
}

func maxAttemptCost(models []modelPrice, inputBound, outputBound int64) (int64, error) {
	var maximum int64
	for _, model := range models {
		cost, err := actualCost(model, inputBound, outputBound)
		if err != nil {
			return 0, err
		}
		if cost > maximum {
			maximum = cost
		}
	}
	return maximum, nil
}

func effectiveAttemptLimit(route routeConfig) int32 {
	if route.maxAttempts < 1 {
		return 1
	}
	if route.maxAttempts > maxProviderAttempts {
		return maxProviderAttempts
	}
	return route.maxAttempts
}

func costForTokens(tokens, microUSDPerMillion int64) (int64, error) {
	if tokens < 0 || microUSDPerMillion < 0 {
		return 0, errors.New("negative token or rate")
	}
	if tokens == 0 || microUSDPerMillion == 0 {
		return 0, nil
	}
	if tokens > (math.MaxInt64-999_999)/microUSDPerMillion {
		return 0, errors.New("token cost overflow")
	}
	return (tokens*microUSDPerMillion + 999_999) / 1_000_000, nil
}

func checkedAdd(left, right int64) (int64, error) {
	if left < 0 || right < 0 || left > math.MaxInt64-right {
		return 0, errors.New("integer addition overflow")
	}
	return left + right, nil
}

func checkedMul(value, multiplier int64) (int64, bool) {
	if value < 0 || multiplier < 0 || (value != 0 && multiplier > math.MaxInt64/value) {
		return 0, false
	}
	return value * multiplier, true
}

func isUniqueViolation(err error) bool {
	var pgErr *pgconn.PgError
	return errors.As(err, &pgErr) && pgErr.Code == "23505"
}

func valueOrEmpty(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}

func trustedProviderUsage(response ProviderResponse) bool {
	switch response.UsageStatus {
	case ProviderUsageTrusted:
		return true
	case ProviderUsageUntrusted:
		return false
	case "":
		// Kept for internal adapters written before the explicit status was
		// introduced. A syntactically missing, all-zero usage payload remains
		// unknown unless the adapter explicitly marks it trusted.
		return response.InputTokens > 0 || response.OutputTokens > 0
	default:
		return false
	}
}

func reconciliationMetadata(logicalCallID, attemptID, ledgerEntryID string) map[string]string {
	return map[string]string{
		"logical_call_id": logicalCallID,
		"attempt_id":      attemptID,
		"ledger_entry_id": ledgerEntryID,
	}
}

func withStableErrorMetadata(err error, errorMetadata map[string]string) error {
	var stable *stableError
	if !errors.As(err, &stable) || len(errorMetadata) == 0 {
		return err
	}
	return &stableError{
		err:  stable.err.WithMetadata(errorMetadata),
		code: stable.code,
	}
}
