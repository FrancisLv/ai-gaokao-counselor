package biz

import (
	"context"

	"github.com/choucisan/ai-gaokao-counselor/conf"
	errs "github.com/choucisan/ai-gaokao-counselor/internal/pkg/errors"
)

// RecommendUsecase 承接 EstimateProb + CheckConstraints RPC；持 conf 构造的默认 RecommendConfig。
// 编排（取数/概率/效用/保底）全在本 usecase 内部，service 层只做 DTO 映射（Q17/Q18）。
type RecommendUsecase struct {
	admission  AdmissionRepo
	rank       *RankUsecase // 复用换算，不自持 RankRepo（DZ4）
	defaultCfg RecommendConfig
	configErr  error
}

// NewRecommendUsecase 构造（wire 注入 AdmissionRepo + *RankUsecase + *conf.Bootstrap）。
// 从 bc.Algo 构造默认 RecommendConfig（usecase 持有；每请求 profile 覆盖在方法内部完成）。
func NewRecommendUsecase(admission AdmissionRepo, rank *RankUsecase, bc *conf.Bootstrap) *RecommendUsecase {
	return &RecommendUsecase{
		admission:  admission,
		rank:       rank,
		defaultCfg: defaultRecommendConfig(bc),
		configErr:  ValidateConfig(bc),
	}
}

// batchUndergrad MVP 固定本科批（controlScore 取本科线、候选/历史 SQL 固定本科批，§3.0）。
const batchUndergrad = "本科批"

// EstimateProb 编排（§4.2②，year=报考年驱动全年份口径）：
//
//	a. 复用注入的 *RankUsecase.Convert(报考年) → 用户百分位（一分一段表缺失/分数低于本科线 → 透传报错，DY5）
//	b. AdmissionRepo.LoadCandidates(报考年) —— 报考年 enrollment_plan 覆盖的组、统计取报考年-1（DY4）
//	c. 硬约束过滤（选科）：constraint.CheckConstraints 纯函数（组级 match-any，过滤先于概率，§3.3）
//	d. AdmissionRepo.BatchGroupHistory(报考年,W) → 窗口[报考年-W,报考年-1] historyMap、
//	   BatchGroupPlan(报考年,报考年-1) → planMap；⚠️窗口绝不含报考年（验证 holdout）
//	   → 把 kept + 用户百分位 + historyMap + planMap + config 注入纯函数 RecommendCandidates（批量算概率+分档）
func (u *RecommendUsecase) EstimateProb(ctx context.Context, q UserQuery, profile UserProfile) (RecommendResult, error) {
	if err := ctx.Err(); err != nil {
		return RecommendResult{}, err
	}
	if u.configErr != nil {
		return RecommendResult{}, u.configErr
	}
	if q.Batch != "" && q.Batch != batchUndergrad {
		return RecommendResult{}, errs.ErrInvalidQuery
	}
	if err := validateSubjects(q.Subjects, q.Category); err != nil {
		return RecommendResult{}, err
	}
	batch := q.Batch
	if batch == "" {
		batch = batchUndergrad // MVP 固定本科批
	}
	cfg := u.defaultCfg // 值拷贝；per-request 偏好覆盖（如 PreferCities）在此叠加，MVP 分档不消费城市偏好

	// a. 分数 → 位次/百分位（复用 RankUsecase，同一百分位口径，保证用户侧与候选侧可比）
	rankRes, err := u.rank.Convert(ctx, RankQuery{
		Province: q.Province, Category: q.Category, Year: q.Year, Score: q.Score,
	})
	if err != nil {
		return RecommendResult{}, err // ErrMissingScoreRankMap / ErrBelowControlLine / ErrScoreAboveMax 透传
	}
	userPercentile := rankRes.Percentile

	// b. 候选池（报考年 enrollment_plan 覆盖的组，统计取报考年-1）
	candidates, err := u.admission.LoadCandidates(ctx, q.Province, q.Category, batch, q.Year)
	if err != nil {
		return RecommendResult{}, err
	}

	// c. 硬约束过滤（选科）：constraint.CheckConstraints 纯函数（组级 match-any，过滤先于概率，§3.3）。
	//    EstimateProb 只推进 kept；rejected（选科不符/无法解析）在本 RPC 丢弃（独立 CheckConstraints RPC 才回传原因）。
	kept, _, err := CheckConstraints(UserQuery{Subjects: q.Subjects, Category: q.Category}, candidates)
	if err != nil {
		return RecommendResult{}, err
	}

	// d. 批量取历史 + 计划（各一条批量 SQL，避免 N+1），注入纯函数算概率 + 分档
	historyMap, err := u.admission.BatchGroupHistory(ctx, q.Province, q.Category, batch, q.Year, cfg.WindowYears)
	if err != nil {
		return RecommendResult{}, err
	}
	planMap, err := u.admission.BatchGroupPlan(ctx, q.Province, q.Category, q.Year, q.Year-1)
	if err != nil {
		return RecommendResult{}, err
	}

	res := RecommendCandidates(kept, userPercentile, historyMap, planMap, cfg)
	// 回填全局审计依据（§4.2②：RecommendResult = 三档 + AuditBasis）。
	// SampleYears = 概率窗口 [报考年-W, 报考年-1]（不含报考年，与 BatchGroupHistory 同口径）。
	res.Basis = AuditBasis{
		UserRank:       rankRes.Rank,
		UserPercentile: rankRes.Percentile,
		SampleYears:    sampleYears(q.Year, cfg.WindowYears),
		Method:         methodWeightedHit,
		Province:       q.Province,
		Category:       q.Category,
	}
	return res, nil
}

// sampleYears 生成概率窗口年份列表 [year-w, year-1]（升序，不含报考年），供 AuditBasis.SampleYears。
// w<=0 时返回空 slice（防御）。
func sampleYears(year, w int) []int {
	if w <= 0 {
		return nil
	}
	years := make([]int, 0, w)
	for y := year - w; y <= year-1; y++ {
		years = append(years, y)
	}
	return years
}

// CheckConstraints 独立 RPC 编排（§4.2③）：拆 []Candidate → 委托 constraint.CheckConstraints 纯函数
// （与 EstimateProb step c 同一 biz fn，DZ3）→ 按 GroupKey{UniversityCode,MajorGroup} 从入参池回填 kept/rejected 的 prob。
// 选科不依赖 prob，回填仅为契约形状完整（rejected 项经纯函数后 prob 为零值，此处补回入参概率）。
func (u *RecommendUsecase) CheckConstraints(ctx context.Context, subjects []string, category string, candidates []CandidateWithProb) ([]CandidateWithProb, []RejectedCandidate, error) {
	if err := ctx.Err(); err != nil {
		return nil, nil, err
	}
	if len(candidates) > 0 || len(subjects) > 0 || category != "" {
		if err := validateSubjects(subjects, category); err != nil {
			return nil, nil, err
		}
	}
	if err := validateCandidates(candidates, false); err != nil {
		return nil, nil, err
	}
	// a. 拆 []Candidate + 建 GroupKey→ProbEstimate 索引（回填用）
	raw := make([]Candidate, 0, len(candidates))
	probIndex := make(map[GroupKey]ProbEstimate, len(candidates))
	for _, cwp := range candidates {
		raw = append(raw, cwp.Candidate)
		probIndex[GroupKey{cwp.UniversityCode, cwp.MajorGroup}] = cwp.ProbEstimate
	}

	// b. 委托 constraint 纯函数（构 UserQuery{Subjects,Category}，与 EstimateProb step c 同一 biz fn）
	keptRaw, rejectedRaw, err := CheckConstraints(UserQuery{Subjects: subjects, Category: category}, raw)
	if err != nil {
		return nil, nil, err
	}

	// c. 按 GroupKey 回填 prob（kept/rejected 均补回入参概率，保证契约形状完整）
	kept := make([]CandidateWithProb, 0, len(keptRaw))
	for _, c := range keptRaw {
		kept = append(kept, CandidateWithProb{Candidate: c, ProbEstimate: probIndex[GroupKey{c.UniversityCode, c.MajorGroup}]})
	}
	rejected := make([]RejectedCandidate, 0, len(rejectedRaw))
	for _, r := range rejectedRaw {
		rejected = append(rejected, RejectedCandidate{
			CandidateWithProb: CandidateWithProb{Candidate: r.Candidate, ProbEstimate: probIndex[GroupKey{r.UniversityCode, r.MajorGroup}]},
			Rule:              r.Rule,
			Reason:            r.Reason,
		})
	}
	return kept, rejected, nil
}
