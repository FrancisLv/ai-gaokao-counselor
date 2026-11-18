package biz

import "math"

// 本文件是 §3.2 录取概率估计的 biz 纯函数层：不碰 SQL、不 import pb、不 import Kratos errors。
// historyMap/planMap 由 data 层（AdmissionRepo）查好后注入，本层只做确定性算术。
// 可复现纪律（DH1/DH3）：单线程、按 candidates 输入序 append、只按 key 查 map（禁 map 迭代产序）。

// methodWeightedHit 概率计算方法标注（ProbEstimate.Method，满足可审计原则）。
const methodWeightedHit = "weighted_hit"

// DeriveSchoolTier 从 university 三个布尔派生学校层次标签（§3.2 Step1 方案 B：biz 纯函数，可单测）。
// 优先级 985 > 211 > 双一流 > 普通，与 utility tier 打分（1.0/0.8/0.6/0.4）排序一致。
// 现实中 985⊂211 且多在双一流名单内，布尔有重叠，优先级 switch 保证唯一确定标签。
// 导出供 data 层行→Candidate 映射时调用（SQL 只选三布尔、不用 CASE，使优先级规则显式在代码中）。
func DeriveSchoolTier(is985, is211, isDualClass bool) string {
	switch {
	case is985:
		return "985"
	case is211:
		return "211"
	case isDualClass:
		return "双一流"
	default:
		return "普通"
	}
}

// calculateProbability 计算单个候选的录取概率（加权命中率法，二元命中，§3.2 Step2）。
// 核心思想：统计历史上用户「够得着」（用户百分位 ≤ 录取百分位）的年份加权比例。
// history 须按年份降序（index 越小越近、权重 decay^i 越高），由 data 层 ORDER BY year DESC 保证。
// 注：二元命中 → 概率为有限离散值，同档内区分度由 utility 兜底；连续性修正为后续优化（§6.1）。
func calculateProbability(userPercentile float64, history []AdmissionRecord, config RecommendConfig) float64 {
	if len(history) == 0 {
		return 0.01 // 无历史数据，返回最低概率
	}

	// 加权命中率：Σ(命中年份权重) / Σ(所有年份权重)。保留 Pow 与累加次序，不分配临时权重 slice。
	var sumWeight, sumHit float64
	for i, h := range history {
		weight := math.Pow(config.DecayFactor, float64(i))
		sumWeight += weight
		if userPercentile <= h.MinPercentile { // 用户百分位 ≤ 录取百分位 → 「够得着」
			sumHit += weight
		}
	}

	prob := sumHit / sumWeight
	// 限制到 [1%, 99%]：最低 1% 避免绝望、最高 99% 避免过度承诺
	return math.Max(0.01, math.Min(0.99, prob))
}

// batchEstimateProb 对一批候选逐组算概率（biz 纯函数，§3.2 Step2）。
// historyMap/planMap 由 data 层批量查好注入（避免 N+1）；无历史数据的候选跳过（新增组概率层不估）。
func batchEstimateProb(candidates []Candidate, userPercentile float64,
	historyMap map[GroupKey][]AdmissionRecord, planMap map[GroupKey]PlanChange,
	config RecommendConfig) []CandidateWithProb {

	results := make([]CandidateWithProb, 0, len(candidates))
	for _, cand := range candidates {
		key := GroupKey{UniversityCode: cand.UniversityCode, MajorGroup: cand.MajorGroup}
		hist, ok := historyMap[key]
		if !ok || len(hist) == 0 {
			continue // 无历史数据，跳过
		}
		// 拷贝一份，避免下面的百分位回填 / 计划修正污染 historyMap 里的共享 slice
		history := make([]AdmissionRecord, 0, len(hist))
		for _, h := range hist {
			if h.TotalCount > 0 && h.GroupMinRank > 0 && h.GroupMinRank <= h.TotalCount {
				history = append(history, h)
			}
		}
		if len(history) == 0 {
			continue
		}

		// 组投档线位次 → 百分位（复用 RankToPercentile，与用户百分位同口径、同分母才能直接比较）
		for i := range history {
			history[i].MinPercentile = RankToPercentile(history[i].GroupMinRank, history[i].TotalCount)
		}

		// 审计保存原始 rank/totalCount 百分位，不混入下面的计划修正；独立拷贝避免共享污染。
		cand.HistoryData = append([]AdmissionRecord(nil), history...)

		// 计划修正：用「报考年 vs 报考年-1」的计划变化率统一修正各年百分位（近似，不逐年还原）
		if plan, ok := planMap[key]; ok && plan.PrevPlanCount > 0 {
			change := float64(plan.PlanCount-plan.PrevPlanCount) / float64(plan.PrevPlanCount)
			for i := range history {
				history[i].MinPercentile *= (1 + change*config.PlanAdjust)
			}
		}

		prob := calculateProbability(userPercentile, history, config)
		results = append(results, CandidateWithProb{
			Candidate: cand, // 内嵌候选全部字段（UniversityName/MajorGroup/Majors 等）
			ProbEstimate: ProbEstimate{
				Probability: prob,
				Method:      methodWeightedHit,
				DataYears:   len(history),
			},
		})
	}
	return results
}

// categorize 按概率阈值分冲/稳/保（biz 纯函数，§3.2 Step3）。
// < RushThreshold 淘汰（FilteredCount++）；[Rush,Stable) 冲；[Stable,Safe) 稳；≥Safe 保。
// H3/Q33：回填 *Total（EstimateProbReply.rush/stable/safe_total 来源）。MVP PerCategoryLimit=0 不截断，
// 故 *Total == len(*Candidates)（截断逻辑为可选安全阀，MVP 不启用，与 §3.2 categorize 权威代码一致）。
func categorize(candidates []CandidateWithProb, config RecommendConfig) RecommendResult {
	result := RecommendResult{TotalCandidates: len(candidates)}

	for _, c := range candidates {
		switch {
		case c.Probability < config.RushThreshold:
			result.FilteredCount++ // 概率 < 冲阈值，不推荐（淘汰）
		case c.Probability < config.StableThreshold:
			c.Category = CategoryRush
			result.RushCandidates = append(result.RushCandidates, c)
		case c.Probability < config.SafeThreshold:
			c.Category = CategoryStable
			result.StableCandidates = append(result.StableCandidates, c)
		default:
			c.Category = CategorySafe
			result.SafeCandidates = append(result.SafeCandidates, c)
		}
	}

	result.RushTotal = len(result.RushCandidates)
	result.StableTotal = len(result.StableCandidates)
	result.SafeTotal = len(result.SafeCandidates)
	return result
}

// RecommendCandidates 概率计算 + 分档（prob 层主入口，biz 纯函数，§3.2）。
// = batchEstimateProb（逐组算概率）+ categorize（按阈值分冲/稳/保）的组合封装。
// 编排（rank→LoadCandidates→CheckConstraints→调 Repo 取 historyMap/planMap）在
// RecommendUsecase.EstimateProb 内部完成（§4.2②）；本函数只吃注入数据，保持无 IO 不变量。
func RecommendCandidates(candidates []Candidate, userPercentile float64,
	historyMap map[GroupKey][]AdmissionRecord, planMap map[GroupKey]PlanChange,
	config RecommendConfig) RecommendResult {

	withProb := batchEstimateProb(candidates, userPercentile, historyMap, planMap, config)
	return categorize(withProb, config)
}
