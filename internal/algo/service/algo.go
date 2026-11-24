// Package service 是 svc-algo 的服务层：proto 对接（薄），wire 注入 biz usecase。
//
// 薄契约（Q18）：每个 RPC 方法体只做三件事——① 解析/校验入参 ② 调一个 usecase 方法 ③ biz↔pb 映射。
// 禁止 service 直接调 Repo 或串多个 biz 步骤；所有编排在 usecase 内部。
package service

import (
	"context"
	"strconv"
	"strings"

	kerrors "github.com/go-kratos/kratos/v2/errors"
	"github.com/google/wire"

	algov1 "github.com/choucisan/ai-gaokao-counselor/api/algo/v1"
	"github.com/choucisan/ai-gaokao-counselor/internal/algo/biz"
)

// ProviderSet 是 service 层的 wire provider 集。
var ProviderSet = wire.NewSet(NewAlgo)

// Service 实现 algov1.AlgoServer（核心确定性算法 + 结构化资格 + 草稿风险汇总）。
type Service struct {
	algov1.UnimplementedAlgoServer
	rank        *biz.RankUsecase
	recommend   *biz.RecommendUsecase
	optimize    *biz.OptimizeUsecase
	eligibility *biz.EligibilityUsecase
}

// NewAlgo 构造 Service（wire 注入 usecase）。
func NewAlgo(rank *biz.RankUsecase, recommend *biz.RecommendUsecase, optimize *biz.OptimizeUsecase, eligibility *biz.EligibilityUsecase) *Service {
	return &Service{rank: rank, recommend: recommend, optimize: optimize, eligibility: eligibility}
}

// RankConvert 分数 → 位次（year=报考年，见 §3.0）。
func (s *Service) RankConvert(ctx context.Context, req *algov1.RankConvertRequest) (*algov1.RankConvertReply, error) {
	if req.GetReleaseId() == "" {
		return nil, kerrors.BadRequest("RELEASE_REQUIRED", "release_id 不能为空")
	}
	year, err := parseYear(req.GetYear())
	if err != nil {
		return nil, err
	}
	res, err := s.rank.Convert(ctx, biz.RankQuery{
		ReleaseID: req.GetReleaseId(),
		Province:  req.GetProvince(),
		Category:  req.GetCategory(),
		Year:      year,
		Score:     float64(req.GetScore()),
	})
	if err != nil {
		return nil, toKratosErr(err)
	}
	return toPbRankConvertReply(res), nil
}

// EstimateProb 录取概率 = 候选池推荐（service 薄：解析 year/profile → usecase 编排 → biz→pb 映射，§4.2②）。
func (s *Service) EstimateProb(ctx context.Context, req *algov1.EstimateProbRequest) (*algov1.EstimateProbReply, error) {
	if req.GetReleaseId() == "" {
		return nil, kerrors.BadRequest("RELEASE_REQUIRED", "release_id 不能为空")
	}
	year, err := parseYear(req.GetYear())
	if err != nil {
		return nil, err
	}
	// 注（Q1）：req.per_category_limit MVP 不消费——usecase 用 conf 默认 PerCategoryLimit=0（全量、后端不截断），
	// 薄 service 不合并参数（Q20/DR3）。
	res, err := s.recommend.EstimateProb(ctx, biz.UserQuery{
		ReleaseID: req.GetReleaseId(),
		Province:  req.GetProvince(),
		Year:      year,
		Category:  req.GetCategory(),
		Batch:     req.GetBatch(),
		Score:     float64(req.GetScore()),
		Subjects:  req.GetSubjects(),
	}, fromPbUserProfile(req.GetProfile()))
	if err != nil {
		return nil, toKratosErr(err)
	}
	return toPbEstimateProbReply(res), nil
}

// CheckConstraints 硬规则裁决（service 薄：pb→biz 映射候选 → usecase 委托纯函数 → biz→pb 映射，§4.2③）。
func (s *Service) CheckConstraints(ctx context.Context, req *algov1.CheckConstraintsRequest) (*algov1.CheckConstraintsReply, error) {
	candidates := fromPbCandidateWithProbSlice(req.GetCandidates())
	kept, rejected, err := s.recommend.CheckConstraints(ctx, req.GetSubjects(), req.GetCategory(), candidates)
	if err != nil {
		return nil, toKratosErr(err)
	}
	return toPbCheckConstraintsReply(kept, rejected), nil
}

// EvaluateEligibility 是结构化资格唯一主入口；service 只做类型映射。
func (s *Service) EvaluateEligibility(ctx context.Context, req *algov1.EvaluateEligibilityRequest) (*algov1.EvaluateEligibilityReply, error) {
	result, err := s.eligibility.EvaluateEligibility(ctx, fromPbEligibilityRequest(req))
	if err != nil {
		return nil, toKratosErr(err)
	}
	return toPbEligibilityReply(result), nil
}

// OptimizeWishlist 志愿表排序与配比（service 薄：pb→biz 映射候选/profile → usecase 编排 → biz→pb 映射，§4.2④）。
func (s *Service) OptimizeWishlist(ctx context.Context, req *algov1.OptimizeWishlistRequest) (*algov1.OptimizeWishlistReply, error) {
	candidates := fromPbCandidateWithProbSlice(req.GetCandidates())
	res, err := s.optimize.OptimizeWishlist(ctx, candidates, fromPbUserProfile(req.GetProfile()))
	if err != nil {
		return nil, toKratosErr(err)
	}
	return &algov1.OptimizeWishlistReply{Wishlist: toPbWishlistResult(res)}, nil
}

// SummarizeWishlistDraft 只汇总用户选中的组，不重排候选或回填任何组。
func (s *Service) SummarizeWishlistDraft(
	ctx context.Context,
	req *algov1.SummarizeWishlistDraftRequest,
) (*algov1.SummarizeWishlistDraftReply, error) {
	if req.GetReleaseId() == "" {
		return nil, kerrors.BadRequest("RELEASE_REQUIRED", "release_id 不能为空")
	}
	result, err := s.optimize.SummarizeWishlistDraft(
		ctx,
		fromPbDraftGroupRiskInputs(req.GetGroups()),
	)
	if err != nil {
		return nil, toKratosErr(err)
	}
	return &algov1.SummarizeWishlistDraftReply{
		Summary: toPbWishlistDraftRisk(result),
	}, nil
}

// parseYear 解析 req.Year（string，报考年如 "2025"）→ int；空/非法 → INVALID_YEAR（DE2）。
func parseYear(y string) (int, error) {
	y = strings.TrimSpace(y)
	if y == "" {
		return 0, kerrors.BadRequest("INVALID_YEAR", "year 不能为空")
	}
	n, err := strconv.Atoi(y)
	if err != nil {
		return 0, kerrors.BadRequest("INVALID_YEAR", "year 非法："+y)
	}
	return n, nil
}
