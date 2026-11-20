package llmgw

import (
	"context"
	"strings"

	kerrors "github.com/go-kratos/kratos/v2/errors"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

const (
	ReasonPrincipalRequired      = "PRINCIPAL_REQUIRED"
	ReasonRunNotAllowed          = "RUN_NOT_ALLOWED"
	ReasonRequestInvalid         = "REQUEST_INVALID"
	ReasonRoleNotAllowed         = "ROLE_NOT_ALLOWED"
	ReasonModelNotAllowed        = "MODEL_NOT_ALLOWED"
	ReasonReleaseUnavailable     = "RELEASE_UNAVAILABLE"
	ReasonReleaseRevoked         = "RELEASE_REVOKED"
	ReasonPriceUnavailable       = "PRICE_UNAVAILABLE"
	ReasonPromptNotAllowed       = "PROMPT_TEMPLATE_NOT_ALLOWED"
	ReasonSchemaNotAllowed       = "RESPONSE_SCHEMA_NOT_ALLOWED"
	ReasonSchemaValidationFailed = "SCHEMA_VALIDATION_FAILED"
	ReasonLimitExceeded          = "REQUEST_LIMIT_EXCEEDED"
	ReasonBudgetExceeded         = "BUDGET_EXCEEDED"
	ReasonBudgetOverrun          = "BUDGET_OVERRUN"
	ReasonIdempotencyConflict    = "IDEMPOTENCY_CONFLICT"
	ReasonCallInProgress         = "CALL_IN_PROGRESS"
	ReasonProviderUnavailable    = "PROVIDER_UNAVAILABLE"
	ReasonProviderRateLimited    = "PROVIDER_RATE_LIMITED"
	ReasonProviderBadRequest     = "PROVIDER_BAD_REQUEST"
	ReasonProviderAuthFailed     = "PROVIDER_AUTH_FAILED"
	ReasonSchemaUnsupported      = "PROVIDER_SCHEMA_UNSUPPORTED"
	ReasonReconciliation         = "RECONCILIATION_REQUIRED"
)

type stableError struct {
	err  *kerrors.Error
	code codes.Code
}

func (e *stableError) Error() string { return e.err.Error() }

func (e *stableError) Unwrap() error { return e.err }

func (e *stableError) GRPCStatus() *status.Status {
	protoStatus := e.err.GRPCStatus().Proto()
	protoStatus.Code = int32(e.code)
	return status.FromProto(protoStatus)
}

func newStableError(code codes.Code, httpCode int, reason, message string) error {
	return &stableError{
		err:  kerrors.New(httpCode, reason, message),
		code: code,
	}
}

func requirePrincipal(ctx context.Context) (string, error) {
	incoming, ok := metadata.FromIncomingContext(ctx)
	if !ok {
		return "", newStableError(codes.Unauthenticated, 401, ReasonPrincipalRequired, "缺少可信服务身份")
	}
	values := incoming.Get("x-principal-id")
	if !ok || len(values) == 0 {
		return "", newStableError(codes.Unauthenticated, 401, ReasonPrincipalRequired, "缺少可信服务身份")
	}
	principal := strings.TrimSpace(values[0])
	if principal == "" || len(values) != 1 || principal != values[0] {
		return "", newStableError(codes.Unauthenticated, 401, ReasonPrincipalRequired, "可信服务身份无效")
	}
	return principal, nil
}
