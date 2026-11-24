package llmgw

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestDeepSeekProviderSendsGovernedChatAndParsesUsage(t *testing.T) {
	var received deepSeekRequest
	server := httptest.NewServer(http.HandlerFunc(func(
		writer http.ResponseWriter,
		request *http.Request,
	) {
		if request.URL.Path != "/chat/completions" {
			t.Errorf("path = %q", request.URL.Path)
		}
		if got := request.Header.Get("Authorization"); got != "Bearer test-key" {
			t.Errorf("authorization = %q", got)
		}
		if err := json.NewDecoder(request.Body).Decode(&received); err != nil {
			t.Errorf("decode request: %v", err)
		}
		writer.Header().Set("Content-Type", "application/json")
		_, _ = writer.Write([]byte(`{
			"id":"provider-request-1",
			"choices":[{
				"message":{"content":"{\"summary\":\"ok\"}"},
				"finish_reason":"stop"
			}],
			"usage":{"prompt_tokens":12,"completion_tokens":5}
		}`))
	}))
	defer server.Close()

	provider, err := NewDeepSeekProvider(server.URL, "test-key", server.Client())
	if err != nil {
		t.Fatal(err)
	}
	response, err := provider.Chat(context.Background(), ProviderRequest{
		Model:    "deepseek-v4-pro",
		Provider: "deepseek",
		Messages: []ProviderMessage{
			{Role: "system", Content: "test-system"},
			{Role: "user", Content: "test"},
		},
		ResponseSchema:  []byte(`{"type":"object"}`),
		MaxOutputTokens: 128,
		Temperature:     0,
		Timeout:         5 * time.Second,
	})
	if err != nil {
		t.Fatal(err)
	}
	if received.Model != "deepseek-v4-pro" ||
		received.MaxTokens != 128 ||
		received.Stream ||
		received.ResponseFormat == nil ||
		received.ResponseFormat.Type != "json_object" ||
		received.Thinking == nil ||
		received.Thinking.Type != "disabled" {
		t.Fatalf("request = %+v", received)
	}
	if len(received.Messages) != 2 ||
		received.Messages[0].Role != "system" ||
		!strings.Contains(received.Messages[0].Content, "test-system") ||
		!strings.Contains(received.Messages[0].Content, `{"type":"object"}`) ||
		received.Messages[1].Role != "user" ||
		received.Messages[1].Content != "test" {
		t.Fatalf("messages = %+v", received.Messages)
	}
	if response.Content != `{"summary":"ok"}` ||
		response.InputTokens != 12 ||
		response.OutputTokens != 5 ||
		response.ProviderRequestID != "provider-request-1" ||
		response.FinishReason != "stop" ||
		response.UsageStatus != ProviderUsageTrusted {
		t.Fatalf("response = %+v", response)
	}
}

func TestDeepSeekProviderClassifiesHTTPFailures(t *testing.T) {
	for _, testCase := range []struct {
		status    int
		code      string
		retryable bool
	}{
		{http.StatusTooManyRequests, ReasonProviderRateLimited, true},
		{http.StatusUnauthorized, ReasonProviderAuthFailed, false},
		{http.StatusBadRequest, ReasonProviderBadRequest, false},
		{http.StatusServiceUnavailable, ReasonProviderUnavailable, true},
	} {
		t.Run(http.StatusText(testCase.status), func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(
				writer http.ResponseWriter,
				_ *http.Request,
			) {
				writer.WriteHeader(testCase.status)
			}))
			defer server.Close()
			provider, err := NewDeepSeekProvider(server.URL, "test-key", server.Client())
			if err != nil {
				t.Fatal(err)
			}
			_, err = provider.Chat(context.Background(), ProviderRequest{
				Model:    "deepseek-v4-pro",
				Provider: "deepseek",
			})
			var failure *ProviderFailure
			if !errors.As(err, &failure) {
				t.Fatalf("error = %T %v", err, err)
			}
			if failure.Code != testCase.code || failure.Retryable != testCase.retryable {
				t.Fatalf("failure = %+v", failure)
			}
		})
	}
}
