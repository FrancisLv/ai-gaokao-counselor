// Package algo 实现 svc-algo：纯确定性计算，可复现、可审计、可独立单测。
//
// 本包只做「转发到子算法包」与 proto 映射；具体算法在 rank/prob/constraint/optimize/sim。
package algo

import (
	"context"
	"errors"

	algov1 "github.com/choucisan/ai-gaokao-counselor/api/algo/v1"
)

// Service 实现 algo.v1.AlgoServer。
type Service struct {
	algov1.UnimplementedAlgoServer
	// TODO(M1): store（PG：一分一段表 / 录取线）
}

// NewService 构造 Service。
func NewService() *Service { return &Service{} }

// RankConvert 分数 → 位次。
func (s *Service) RankConvert(ctx context.Context, req *algov1.RankConvertRequest) (*algov1.RankConvertReply, error) {
	// TODO(M1): 查 rank 包（一分一段表 + 插值）。注意：不依赖 LLM。
	return nil, errors.New("TODO: RankConvert not implemented")
}

// EstimateProb 录取概率估计。
func (s *Service) EstimateProb(ctx context.Context, req *algov1.EstimateProbRequest) (*algov1.EstimateProbReply, error) {
	// TODO(M4): prob 包——历史位次分布 → 概率 + 可审计依据。
	return nil, errors.New("TODO: EstimateProb not implemented")
}

// CheckConstraints 硬规则裁决。
func (s *Service) CheckConstraints(ctx context.Context, req *algov1.CheckConstraintsRequest) (*algov1.CheckConstraintsReply, error) {
	// TODO(M4): constraint 包——选科 / 单科 / 体检 / 批次；剔除带原因。
	return nil, errors.New("TODO: CheckConstraints not implemented")
}

// OptimizeWishlist 志愿表排序与配比。
func (s *Service) OptimizeWishlist(ctx context.Context, req *algov1.OptimizeWishlistRequest) (*algov1.OptimizeWishlistReply, error) {
	// TODO(M4): optimize 包——约束满足 + 启发式搜索；冲稳保配比、梯度平滑。
	return nil, errors.New("TODO: OptimizeWishlist not implemented")
}
