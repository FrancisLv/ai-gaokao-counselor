package llmgw

import (
	"context"
	"errors"
	"time"
)

// Provider is the process-internal boundary for one governed supplier request.
// The public gRPC seam never exposes provider URLs, credentials, retries, or raw errors.
type Provider interface {
	// Chat performs exactly one supplier request. Provider SDK automatic retries
	// must stay disabled; retry accounting belongs to the gateway.
	Chat(context.Context, ProviderRequest) (ProviderResponse, error)
}

type ProviderMessage struct {
	Role    string
	Content string
}

type ProviderRequest struct {
	LogicalCallID   string
	AttemptID       string
	Model           string
	Provider        string
	Messages        []ProviderMessage
	ResponseSchema  []byte
	Temperature     float64
	MaxOutputTokens int32
	Timeout         time.Duration
}

type ProviderResponse struct {
	Content           string
	InputTokens       int64
	OutputTokens      int64
	ProviderRequestID string
	FinishReason      string
	// UsageStatus lets the adapter explicitly reject a syntactically present
	// usage payload whose accounting meaning is not trustworthy.
	UsageStatus ProviderUsageStatus
}

type ProviderUsageStatus string

const (
	ProviderUsageTrusted   ProviderUsageStatus = "TRUSTED"
	ProviderUsageUntrusted ProviderUsageStatus = "UNTRUSTED"
)

// ProviderFailure is a supplier rejection that the adapter has determined did
// not incur a charge. Retryable failures may use the next route model. Any
// failure without this classification remains potentially charged.
type ProviderFailure struct {
	Code              string
	Retryable         bool
	ProviderRequestID string
}

func (e *ProviderFailure) Error() string {
	if e == nil {
		return ""
	}
	return e.Code
}

type unavailableProvider struct{}

func (unavailableProvider) Chat(context.Context, ProviderRequest) (ProviderResponse, error) {
	return ProviderResponse{}, ErrProviderUnavailable
}

var (
	ErrProviderUnavailable = errors.New("provider unavailable")
	ErrProviderNotCharged  = errors.New("provider rejected request without charging")
)
