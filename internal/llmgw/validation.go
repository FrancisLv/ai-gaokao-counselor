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

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"google.golang.org/grpc/codes"

	llmgwv1 "github.com/choucisan/ai-gaokao-counselor/api/llmgw/v1"
	llmgwdb "github.com/choucisan/ai-gaokao-counselor/internal/llmgw/dbgen"
)

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
	role := strings.ToLower(strings.TrimSpace(req.GetRole()))
	if _, allowed := allowedM5aRoles[role]; !allowed {
		return governedRequest{}, newStableError(
			codes.InvalidArgument, 400, ReasonRoleNotAllowed,
			"M5a 仅允许 profile、report、verify 与 policy_qa 角色",
		)
	}
	if strings.TrimSpace(req.GetReleaseId()) == "" ||
		strings.TrimSpace(req.GetLogicalCallId()) == "" {
		return governedRequest{}, newStableError(codes.InvalidArgument, 400, ReasonRequestInvalid, "缺少必要的治理字段")
	}
	dataClass, err := resolveDataClassification(
		req.GetDataClassification(), req.GetDataClass(),
	)
	if err != nil {
		return governedRequest{}, err
	}
	if req.GetMaxTokens() < 0 || req.GetTimeoutMs() < 0 ||
		math.IsNaN(req.GetTemperature()) || math.IsInf(req.GetTemperature(), 0) {
		return governedRequest{}, newStableError(codes.InvalidArgument, 400, ReasonRequestInvalid, "模型参数无效")
	}
	messages := make([]ProviderMessage, 0, len(req.GetMessages()))
	for _, message := range req.GetMessages() {
		if message == nil || message.GetContent() == "" {
			return governedRequest{}, newStableError(codes.InvalidArgument, 400, ReasonRequestInvalid, "消息内容无效")
		}
		messageRole := strings.ToLower(strings.TrimSpace(message.GetRole()))
		if containsUnsafeStringValues([]string{messageRole}) {
			return governedRequest{}, sensitiveInputNotAllowed()
		}
		if messageRole == "system" {
			return governedRequest{}, newStableError(
				codes.InvalidArgument, 400, ReasonPromptNotAllowed,
				"system message 只能由服务端注册模板生成",
			)
		}
		if _, allowed := allowedCallerMessageRoles[messageRole]; !allowed {
			return governedRequest{}, newStableError(
				codes.InvalidArgument, 400, ReasonRequestInvalid,
				"消息角色只允许 user 或 assistant",
			)
		}
		messages = append(messages, ProviderMessage{
			Role:    messageRole,
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
		Role:               role,
		ReleaseID:          req.GetReleaseId(),
		LogicalCallID:      req.GetLogicalCallId(),
		PromptTemplateKey:  req.GetPromptTemplateKey(),
		PromptVersion:      req.GetPromptTemplateVersion(),
		SchemaKey:          req.GetResponseSchemaKey(),
		SchemaVersion:      req.GetResponseSchemaVersion(),
		DataClassification: dataClass,
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
	row, err := s.queries.GetRunBinding(ctx, llmgwdb.GetRunBindingParams{
		Column1:     databaseUUID(req.RunID),
		Column2:     databaseUUID(req.SessionID),
		PrincipalID: req.PrincipalID,
		ReleaseID:   req.ReleaseID,
	})
	if errors.Is(err, pgx.ErrNoRows) {
		return runBinding{}, newStableError(
			codes.PermissionDenied, 403, ReasonRunNotAllowed,
			"session/run 不属于当前主体或 release 不匹配",
		)
	}
	if err != nil {
		return runBinding{}, newStableError(codes.Internal, 500, "INTERNAL", "读取 run 身份失败")
	}
	binding := runBinding{
		releaseStatus: row.ReleaseStatus,
		remainingMS:   row.ActiveExecutionRemainingMs,
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

func (s *Service) ensureBudgetScopeActive(ctx context.Context, req governedRequest) error {
	budgetStatus, err := s.queries.GetBudgetStatusBySecurityScope(
		ctx,
		llmgwdb.GetBudgetStatusBySecurityScopeParams{
			PrincipalID: req.PrincipalID,
			Column2:     databaseUUID(req.SessionID),
		},
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil
	}
	if err != nil {
		return newStableError(codes.Internal, 500, "INTERNAL", "读取预算作用域状态失败")
	}
	if budgetStatus == "BUDGET_OVERRUN" {
		return newStableError(
			codes.ResourceExhausted, 429, ReasonBudgetOverrun,
			"预算作用域已因实际用量超预留而停止后续模型调用",
		)
	}
	return nil
}

func (s *Service) loadRoute(ctx context.Context, req governedRequest, binding runBinding) (routeConfig, error) {
	routeRow, err := s.queries.GetRoute(ctx, llmgwdb.GetRouteParams{
		ReleaseID: req.ReleaseID,
		Role:      req.Role,
	})
	if errors.Is(err, pgx.ErrNoRows) {
		return routeConfig{}, newStableError(codes.InvalidArgument, 400, ReasonRoleNotAllowed, "角色未在当前 release 发布")
	}
	if err != nil {
		return routeConfig{}, newStableError(codes.Internal, 500, "INTERNAL", "读取角色路由失败")
	}
	route := routeConfig{
		version:                routeRow.RouteVersion,
		priceVersion:           routeRow.PriceTableVersion,
		primaryModel:           routeRow.PrimaryModelID,
		allowedDataClasses:     routeRow.AllowedDataClasses,
		maxOutput:              routeRow.MaxOutputTokens,
		timeoutMS:              routeRow.TimeoutMs,
		maxAttempts:            routeRow.MaxAttempts,
		allowResultCache:       routeRow.AllowResultCache,
		requiresResponseSchema: routeRow.RequiresResponseSchema,
	}
	if !routeRow.PublishedAt.Valid || routeRow.EffectiveAt.Time.After(s.now()) {
		return routeConfig{}, newStableError(
			codes.FailedPrecondition, 412, ReasonPriceUnavailable,
			"route 绑定的价格版本尚未发布或未生效",
		)
	}

	modelIDs := append([]string{route.primaryModel}, routeRow.FallbackModelIds...)
	seen := make(map[string]struct{}, len(modelIDs))
	for _, modelID := range modelIDs {
		if _, duplicated := seen[modelID]; duplicated {
			return routeConfig{}, newStableError(
				codes.FailedPrecondition, 412, ReasonModelNotAllowed,
				"route 模型顺序包含重复项",
			)
		}
		seen[modelID] = struct{}{}
		priceRow, err := s.queries.GetModelPrice(ctx, llmgwdb.GetModelPriceParams{
			ReleaseID:         req.ReleaseID,
			ModelID:           modelID,
			PriceTableVersion: route.priceVersion,
		})
		if errors.Is(err, pgx.ErrNoRows) {
			return routeConfig{}, s.classifyModelPriceFailure(ctx, req.ReleaseID, modelID)
		}
		if err != nil {
			return routeConfig{}, newStableError(codes.Internal, 500, "INTERNAL", "读取模型价格失败")
		}
		route.models = append(route.models, modelPrice{
			modelID:    modelID,
			provider:   priceRow.Provider,
			inputRate:  priceRow.InputMicroUsdPerMillionTokens,
			outputRate: priceRow.OutputMicroUsdPerMillionTokens,
		})
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
	allowed, err := s.queries.ModelAllowedExists(ctx, llmgwdb.ModelAllowedExistsParams{
		ReleaseID: releaseID,
		ModelID:   modelID,
	})
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
	row, err := s.queries.GetPromptTemplate(ctx, llmgwdb.GetPromptTemplateParams{
		ReleaseID:             req.ReleaseID,
		PromptTemplateKey:     key,
		PromptTemplateVersion: version,
	})
	if errors.Is(err, pgx.ErrNoRows) {
		return promptTemplate{}, newStableError(
			codes.InvalidArgument, 400, ReasonPromptNotAllowed,
			"prompt template 未注册、已撤销或版本不受支持",
		)
	}
	if err != nil {
		return promptTemplate{}, newStableError(codes.Internal, 500, "INTERNAL", "读取 prompt 模板失败")
	}
	if row.Status != "ACTIVE" {
		return promptTemplate{}, newStableError(
			codes.InvalidArgument, 400, ReasonPromptNotAllowed,
			"prompt template 未注册、已撤销或版本不受支持",
		)
	}
	template := promptTemplate{
		key:     row.PromptTemplateKey,
		version: row.PromptTemplateVersion,
		system:  row.SystemTemplate,
		user:    row.UserTemplate,
		digest:  row.TemplateSha256,
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

var allowedM5aRoles = map[string]struct{}{
	"profile":   {},
	"report":    {},
	"verify":    {},
	"policy_qa": {},
}

var allowedCallerMessageRoles = map[string]struct{}{
	"user":      {},
	"assistant": {},
}

var allowedRenderedMessageRoles = map[string]struct{}{
	"system":    {},
	"user":      {},
	"assistant": {},
}

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
