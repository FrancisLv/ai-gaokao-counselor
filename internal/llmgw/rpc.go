package llmgw

import (
	"context"

	llmgwv1 "github.com/choucisan/ai-gaokao-counselor/api/llmgw/v1"
)

func (s *Service) Chat(ctx context.Context, req *llmgwv1.ChatRequest) (*llmgwv1.ChatResponse, error) {
	return s.executeGovernedCall(ctx, req, nil)
}
func (s *Service) StreamChat(
	req *llmgwv1.ChatRequest,
	server llmgwv1.LLMGateway_StreamChatServer,
) error {
	observer := &grpcStreamObserver{server: server}
	_, err := s.executeGovernedCall(server.Context(), req, observer)
	if err == nil {
		return nil
	}
	// The event is best-effort. A disconnected client cannot receive it, but the
	// authoritative unknown-charge state was already persisted by the engine.
	_ = observer.OnError(server.Context(), err)
	return err
}
