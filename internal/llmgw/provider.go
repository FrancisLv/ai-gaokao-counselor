package llmgw

import (
	"context"
	"errors"
	"time"
)

// Provider is the process-internal boundary for one governed supplier request.
// The public gRPC seam never exposes provider URLs, credentials, retries, or raw errors.
type Provider interface {
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
}

type unavailableProvider struct{}

func (unavailableProvider) Chat(context.Context, ProviderRequest) (ProviderResponse, error) {
	return ProviderResponse{}, ErrProviderUnavailable
}

var (
	ErrProviderUnavailable = errors.New("provider unavailable")
	ErrProviderNotCharged  = errors.New("provider rejected request without charging")
)
