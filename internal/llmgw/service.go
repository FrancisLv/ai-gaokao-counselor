// Package llmgw 实现 svc-llm-gateway 的业务逻辑。
//
// 职责单一化说明：本包<仅>负责「模型治理」——路由 / 缓存 / 预算 / 记账；
// 领域算法在 internal/algo，两者不互相依赖。
package llmgw

import (
	"context"
	"errors"

	llmgwv1 "github.com/choucisan/ai-gaokao-counselor/api/llmgw/v1"
)

// Service 实现 llmgw.v1.LLMGatewayServer。
type Service struct {
	llmgwv1.UnimplementedLLMGatewayServer
	// TODO(M4): router（role→model 表）、prompt_hash 缓存、Budget Guard、llm_call_log
}

// NewService 构造 Service。
func NewService() *Service { return &Service{} }

// Chat 按 role 路由模型 → 查预算 → 调用厂商 → 记账。
func (s *Service) Chat(ctx context.Context, req *llmgwv1.ChatRequest) (*llmgwv1.ChatResponse, error) {
	// TODO(M4):
	//   1) role → model 路由（Go 侧唯一决策点）
	//   2) prompt_hash 命中缓存则直接返回
	//   3) Budget Guard：超预算直接拒绝（不要先花钱再记账）
	//   4) 调用厂商；失败按策略重试 / 降级
	//   5) 写 llm_call_log（token / 成本 / 延迟）
	return nil, errors.New("TODO: Chat not implemented")
}

// StreamChat 流式版本（token 级）。
func (s *Service) StreamChat(req *llmgwv1.ChatRequest, stream llmgwv1.LLMGateway_StreamChatServer) error {
	// TODO(M4): 透明转发 token 流；注意超时与背压。
	return errors.New("TODO: StreamChat not implemented")
}
