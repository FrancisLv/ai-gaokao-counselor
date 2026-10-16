// Package gateway 实现 svc-gateway：入口、鉴权、限流、会话、HITL 推送。
//
// 边界：只做「接入与门控」；不承载编排逻辑（那是 svc-orchestrator），
// 也不直接连接模型 / 算法。
package gateway

import (
	"context"
	"errors"

	gatewayv1 "github.com/choucisan/ai-gaokao-counselor/api/gateway/v1"
)

// Service 实现 gateway.v1.GatewayHTTPServer。
type Service struct {
	// TODO(M2): orchestrator 客户端（gRPC）、session store（Redis）、限流器
}

// NewService 构造 Service。
func NewService() *Service { return &Service{} }

// CreatePlan 建会话、驱动编排图，并经 SSE 订阅事件流。
func (s *Service) CreatePlan(ctx context.Context, req *gatewayv1.CreatePlanRequest) (*gatewayv1.CreatePlanReply, error) {
	// TODO(M2): 鉴权/限流 → 建 session_id → 调 orchestrator.StartGraph → SSE 转推。
	return nil, errors.New("TODO: CreatePlan not implemented")
}

// ConfirmProfile HITL 恢复点：用户在画像确认卡片上确认 / 修正。
func (s *Service) ConfirmProfile(ctx context.Context, req *gatewayv1.ConfirmProfileRequest) (*gatewayv1.ConfirmProfileReply, error) {
	// TODO(M3): 回执 → orchestrator.ResumeGraph。
	return nil, errors.New("TODO: ConfirmProfile not implemented")
}

// BackfillProfile 条件必需补问后的恢复点（体检 / 单科 / 语种）。
func (s *Service) BackfillProfile(ctx context.Context, req *gatewayv1.BackfillProfileRequest) (*gatewayv1.BackfillProfileReply, error) {
	// TODO(M3): 回执 → orchestrator.ResumeGraph。
	return nil, errors.New("TODO: BackfillProfile not implemented")
}
