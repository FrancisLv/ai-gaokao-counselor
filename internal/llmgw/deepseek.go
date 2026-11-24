package llmgw

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
)

const deepSeekProviderName = "deepseek"

type DeepSeekProvider struct {
	baseURL string
	apiKey  string
	client  *http.Client
}

func NewDeepSeekProvider(baseURL, apiKey string, client *http.Client) (*DeepSeekProvider, error) {
	baseURL = strings.TrimRight(strings.TrimSpace(baseURL), "/")
	if baseURL == "" {
		baseURL = "https://api.deepseek.com"
	}
	if strings.TrimSpace(apiKey) == "" {
		return nil, errors.New("deepseek api key required")
	}
	if client == nil {
		client = http.DefaultClient
	}
	return &DeepSeekProvider{baseURL: baseURL, apiKey: apiKey, client: client}, nil
}

func (p *DeepSeekProvider) Chat(
	ctx context.Context,
	request ProviderRequest,
) (ProviderResponse, error) {
	if request.Provider != "" && request.Provider != deepSeekProviderName {
		return ProviderResponse{}, &ProviderFailure{
			Code:      ReasonProviderBadRequest,
			Retryable: false,
		}
	}
	payload := deepSeekRequest{
		Model:       request.Model,
		Messages:    make([]deepSeekMessage, 0, len(request.Messages)),
		Temperature: request.Temperature,
		MaxTokens:   request.MaxOutputTokens,
		Stream:      false,
	}
	schemaInstruction := ""
	if len(request.ResponseSchema) > 0 {
		payload.ResponseFormat = &deepSeekResponseFormat{Type: "json_object"}
		payload.Thinking = &deepSeekThinking{Type: "disabled"}
		schemaInstruction = "\n\n输出必须严格符合以下 JSON Schema，且只输出一个 JSON 对象：\n" +
			string(request.ResponseSchema)
	}
	systemApplied := false
	for _, message := range request.Messages {
		content := message.Content
		if schemaInstruction != "" && !systemApplied && message.Role == "system" {
			content += schemaInstruction
			systemApplied = true
		}
		payload.Messages = append(payload.Messages, deepSeekMessage{
			Role:    message.Role,
			Content: content,
		})
	}
	if schemaInstruction != "" && !systemApplied {
		payload.Messages = append(
			[]deepSeekMessage{{Role: "system", Content: strings.TrimSpace(schemaInstruction)}},
			payload.Messages...,
		)
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return ProviderResponse{}, err
	}
	httpRequest, err := http.NewRequestWithContext(
		ctx,
		http.MethodPost,
		p.baseURL+"/chat/completions",
		bytes.NewReader(body),
	)
	if err != nil {
		return ProviderResponse{}, err
	}
	httpRequest.Header.Set("Authorization", "Bearer "+p.apiKey)
	httpRequest.Header.Set("Content-Type", "application/json")
	httpResponse, err := p.client.Do(httpRequest)
	if err != nil {
		// A transport error may happen after the provider accepted the request;
		// keep it unclassified so the gateway records a pending reconciliation.
		return ProviderResponse{}, err
	}
	defer httpResponse.Body.Close()
	responseBody, readErr := io.ReadAll(io.LimitReader(httpResponse.Body, 1<<20))
	if readErr != nil {
		return ProviderResponse{}, readErr
	}
	if httpResponse.StatusCode < 200 || httpResponse.StatusCode >= 300 {
		return ProviderResponse{}, deepSeekHTTPFailure(httpResponse.StatusCode)
	}

	var decoded deepSeekResponse
	if err := json.Unmarshal(responseBody, &decoded); err != nil {
		return ProviderResponse{}, fmt.Errorf("decode deepseek response: %w", err)
	}
	if len(decoded.Choices) == 0 {
		return ProviderResponse{}, errors.New("deepseek response has no choices")
	}
	return ProviderResponse{
		Content:           decoded.Choices[0].Message.Content,
		InputTokens:       decoded.Usage.PromptTokens,
		OutputTokens:      decoded.Usage.CompletionTokens,
		ProviderRequestID: decoded.ID,
		FinishReason:      decoded.Choices[0].FinishReason,
		UsageStatus:       ProviderUsageTrusted,
	}, nil
}

func deepSeekHTTPFailure(status int) *ProviderFailure {
	switch {
	case status == http.StatusTooManyRequests:
		return &ProviderFailure{Code: ReasonProviderRateLimited, Retryable: true}
	case status == http.StatusUnauthorized || status == http.StatusForbidden:
		return &ProviderFailure{Code: ReasonProviderAuthFailed, Retryable: false}
	case status >= 500:
		return &ProviderFailure{Code: ReasonProviderUnavailable, Retryable: true}
	default:
		return &ProviderFailure{Code: ReasonProviderBadRequest, Retryable: false}
	}
}

type deepSeekRequest struct {
	Model          string                  `json:"model"`
	Messages       []deepSeekMessage       `json:"messages"`
	Temperature    float64                 `json:"temperature"`
	MaxTokens      int32                   `json:"max_tokens"`
	Stream         bool                    `json:"stream"`
	ResponseFormat *deepSeekResponseFormat `json:"response_format,omitempty"`
	Thinking       *deepSeekThinking       `json:"thinking,omitempty"`
}

type deepSeekMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

type deepSeekResponseFormat struct {
	Type string `json:"type"`
}

type deepSeekThinking struct {
	Type string `json:"type"`
}

type deepSeekResponse struct {
	ID      string `json:"id"`
	Choices []struct {
		Message struct {
			Content string `json:"content"`
		} `json:"message"`
		FinishReason string `json:"finish_reason"`
	} `json:"choices"`
	Usage struct {
		PromptTokens     int64 `json:"prompt_tokens"`
		CompletionTokens int64 `json:"completion_tokens"`
	} `json:"usage"`
}
