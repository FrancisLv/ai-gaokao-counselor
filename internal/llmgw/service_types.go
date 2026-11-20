// Package llmgw implements the LLM governance seam.
package llmgw

import (
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	llmgwv1 "github.com/choucisan/ai-gaokao-counselor/api/llmgw/v1"
	llmgwdb "github.com/choucisan/ai-gaokao-counselor/internal/llmgw/dbgen"
)

type Service struct {
	llmgwv1.UnimplementedLLMGatewayServer

	pool     *pgxpool.Pool
	queries  *llmgwdb.Queries
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
		queries:  llmgwdb.New(pool),
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
	allowedDataClasses     []string
	maxOutput              int32
	timeoutMS              int32
	maxAttempts            int32
	allowResultCache       bool
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
	cacheKeyHash     string
	cacheClaimToken  string
	cacheHit         bool
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
