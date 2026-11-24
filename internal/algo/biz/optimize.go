package biz

import (
	"context"
	"math"
	"sort"

	"github.com/choucisan/ai-gaokao-counselor/conf"
	errs "github.com/choucisan/ai-gaokao-counselor/internal/pkg/errors"
)

// optimize.go —— 志愿表优化（§3.5）。纯函数，不碰 SQL/不 import pb/不 import Kratos errors。
// 职责：从带效用分的候选池中按冲稳保配比选出最优 L 个，全序排序，并做保底健全性检查（防滑档）。
// 可复现纪律（DH1/DH2/DH3）：所有排序走全序比较器 lessByUtilityThenKey（不依赖输入序、无并列残留）；
// 分档遍历走固定 Category 顺序；不对 map 迭代产序。

// defaultMaxWishlistSize L 兜底（params.MaxWishlistSize<=0 时用；与 config.go i32Or 默认一致）。
const defaultMaxWishlistSize = 45

// defaultSafeGuardThreshold 稳拿保底阈值兜底（params.SafeGuardThreshold<=0 时用）：
// 分档的"保"= p≥SafeThreshold(0.8)；此处"稳拿保底"= p≥0.9（防滑档硬标准，比分档更严，§3.5）。
const defaultSafeGuardThreshold = 0.9

// OptimizeUsecase 承接 OptimizeWishlist RPC；持 conf 构造的默认 OptimizeParams + UtilityConfig。
// utility 只在 OptimizeWishlist 用，故 UtilityConfig 归本 usecase（Q19）。
type OptimizeUsecase struct {
	defaultProps OptimizeParams
	defaultUtil  UtilityConfig
	configErr    error
}

// NewOptimizeUsecase 构造（wire 注入 *conf.Bootstrap）：从 bc.Algo 构造默认 OptimizeParams / UtilityConfig。
func NewOptimizeUsecase(bc *conf.Bootstrap) *OptimizeUsecase {
	return &OptimizeUsecase{
		defaultProps: defaultOptimizeParams(bc),
		defaultUtil:  defaultUtilityConfig(bc),
		configErr:    ValidateConfig(bc),
	}
}

// OptimizeWishlist：合并 profile.RiskAppetite→RatioFromRisk 覆盖默认 TargetRatio（usecase 内，Q20/DR3）
// → utility 批量 + optimize 配比排序 + 保底健全性检查（§4.2④）。
func (u *OptimizeUsecase) OptimizeWishlist(ctx context.Context, candidates []CandidateWithProb, profile UserProfile) (WishlistResult, error) {
	if err := ctx.Err(); err != nil {
		return WishlistResult{}, err
	}
	if u.configErr != nil {
		return WishlistResult{}, u.configErr
	}
	if err := validateCandidates(candidates, true); err != nil {
		return WishlistResult{}, err
	}
	raw := make([]Candidate, 0, len(candidates))
	for _, c := range candidates {
		raw = append(raw, c.Candidate)
	}

	// b. utility 一次性批量打分（当批 min-max 归一化，§3.4）。
	scores, err := CalculateUtilityBatch(profile, raw, u.defaultUtil)
	if err != nil {
		return WishlistResult{}, err
	}

	// c. 组装 []CandidateWithScore（= CandidateWithProb + UtilityScore，FinalRank 由纯函数回填）。
	withScore := make([]CandidateWithScore, 0, len(candidates))
	for i, c := range candidates {
		withScore = append(withScore, CandidateWithScore{
			CandidateWithProb: c,
			UtilityScore:      scores[i],
		})
	}

	// d. params：默认←conf.Algo；profile.RiskAppetite 非空→RatioFromRisk(risk) 覆盖默认 TargetRatio（§3.5/§4.1②/Q20）。
	params := u.defaultProps
	if profile.RiskAppetite != "" {
		params.TargetRatio = RatioFromRisk(profile.RiskAppetite)
	}

	// e. 调纯函数（配比选择 + 全序排序 + 保底健全性检查）。
	return OptimizeWishlist(withScore, params)
}

// SummarizeWishlistDraft 汇总用户已选组；不重排、不补组，也不修改任何概率。
func (u *OptimizeUsecase) SummarizeWishlistDraft(
	ctx context.Context,
	groups []DraftGroupRiskInput,
) (WishlistDraftRisk, error) {
	if err := ctx.Err(); err != nil {
		return WishlistDraftRisk{}, err
	}
	if u.configErr != nil {
		return WishlistDraftRisk{}, u.configErr
	}
	return SummarizeWishlistDraft(groups, u.defaultProps)
}

// RatioFromRisk 风险偏好 → 冲稳保配比（纯函数，实现 §3.6 表；Q20/DR2）。
// 空/未知 risk → balanced 默认（防御，不报错）。
func RatioFromRisk(risk string) RatioConfig {
	switch risk {
	case "conservative":
		return RatioConfig{Rush: 0.2, Stable: 0.4, Safe: 0.4}
	case "aggressive":
		return RatioConfig{Rush: 0.45, Stable: 0.35, Safe: 0.2}
	default: // balanced / 空 / 未知
		return RatioConfig{Rush: 0.3, Stable: 0.4, Safe: 0.3}
	}
}

// OptimizeWishlist 优化志愿表（纯函数，§3.5 五步）。
// Step1 分档 → Step2/3 按配比定向选择（各档取 utility 最高，不足则全局 utility 降序补空位）→
// Step4 全序排序赋 FinalRank → Step5 保底健全性检查（不足则从稳拿池追加）。
// SafeGuardOK=false 是告警非 error（DE3）：正常返回 WishlistResult，不返回 error。
func OptimizeWishlist(candidates []CandidateWithScore, params OptimizeParams) (WishlistResult, error) {
	if err := validateOptimizeParams(params); err != nil {
		return WishlistResult{}, err
	}
	raw := make([]CandidateWithProb, len(candidates))
	for i, c := range candidates {
		if !finite(c.Score) || c.Score < 0 || c.Score > 1 {
			return WishlistResult{}, errs.ErrInvalidCandidate
		}
		raw[i] = c.CandidateWithProb
	}
	if err := validateCandidates(raw, true); err != nil {
		return WishlistResult{}, err
	}
	limit := params.MaxWishlistSize
	if limit <= 0 {
		limit = defaultMaxWishlistSize
	}
	threshold := params.SafeGuardThreshold
	if threshold <= 0 {
		threshold = defaultSafeGuardThreshold
	}

	// Step1~3：分档 + 按配比定向选择（含空位回填），返回已选集合 + 选中 GroupKey 集。
	wishlist, selected := selectByRatio(candidates, params.TargetRatio, limit)

	// Step4：全序排序（UtilityScore 降序，并列 GroupKey 升序），FinalRank = 下标+1。
	sort.Slice(wishlist, func(i, j int) bool { return lessByUtilityThenKey(wishlist[i], wishlist[j]) })
	for i := range wishlist {
		wishlist[i].FinalRank = i + 1
	}

	// Step5：保底健全性检查（防滑档）。safePool = p≥threshold 且未入选的候选，按 p 降序供追加。
	safePool := buildSafePool(candidates, selected, threshold)
	report := checkSafeGuard(&wishlist, safePool, params.MinSafeCount, threshold, limit)

	// 冲/稳数量从最终志愿表按 Category 统计（保底追加可能改变分布）。
	report.RushCount = countByCategory(wishlist, CategoryRush)
	report.StableCount = countByCategory(wishlist, CategoryStable)

	return WishlistResult{Candidates: wishlist, Report: report}, nil
}

// SummarizeWishlistDraft 对用户选定顺序的组集合计算草稿风险。
// 组数与保底目标遵守同一优化配置；风险不足只通过 SafeGuardOK=false 表达，不拒绝用户保存。
func SummarizeWishlistDraft(
	groups []DraftGroupRiskInput,
	params OptimizeParams,
) (WishlistDraftRisk, error) {
	if err := validateOptimizeParams(params); err != nil {
		return WishlistDraftRisk{}, err
	}
	limit := params.MaxWishlistSize
	if limit <= 0 {
		limit = defaultMaxWishlistSize
	}
	if len(groups) < 1 || len(groups) > limit {
		return WishlistDraftRisk{}, errs.ErrInvalidCandidate
	}
	threshold := params.SafeGuardThreshold
	if threshold <= 0 {
		threshold = defaultSafeGuardThreshold
	}

	seen := make(map[GroupKey]struct{}, len(groups))
	summary := WishlistDraftRisk{
		GroupCount:      len(groups),
		SafeGuardTarget: params.MinSafeCount,
		SlipRate:        1,
	}
	utilitySum := 0.0
	utilityCount := 0
	for _, group := range groups {
		if group.UniversityCode == "" || group.MajorGroup == "" ||
			!finite(group.Probability) ||
			group.Probability < 0.01 ||
			group.Probability > 0.99 {
			return WishlistDraftRisk{}, errs.ErrInvalidCandidate
		}
		key := GroupKey{
			UniversityCode: group.UniversityCode,
			MajorGroup:     group.MajorGroup,
		}
		if _, ok := seen[key]; ok {
			return WishlistDraftRisk{}, errs.ErrInvalidCandidate
		}
		seen[key] = struct{}{}
		switch group.Category {
		case CategoryRush:
			summary.RushCount++
		case CategoryStable:
			summary.StableCount++
		case CategorySafe:
			summary.SafeTierCount++
		default:
			return WishlistDraftRisk{}, errs.ErrInvalidCandidate
		}
		if group.Probability >= threshold {
			summary.SafeGuardCount++
		}
		if group.UtilityScore != nil {
			score := *group.UtilityScore
			if !finite(score) || score < 0 || score > 1 {
				return WishlistDraftRisk{}, errs.ErrInvalidCandidate
			}
			utilitySum += score
			utilityCount++
		}
		summary.SlipRate *= 1 - group.Probability
	}
	summary.SafeGuardOK = summary.SafeGuardCount >= summary.SafeGuardTarget
	if utilityCount == len(groups) {
		value := utilitySum / float64(utilityCount)
		summary.ExpectedSatisfaction = &value
	}
	return summary, nil
}

// splitByCategory 按 Category 分档（Step1）。返回的 slice 保持 candidates 输入序（DH1），后续各自排序。
func splitByCategory(candidates []CandidateWithScore) (rush, stable, safe []CandidateWithScore) {
	for _, c := range candidates {
		switch c.Category {
		case CategoryRush:
			rush = append(rush, c)
		case CategoryStable:
			stable = append(stable, c)
		case CategorySafe:
			safe = append(safe, c)
		}
	}
	return rush, stable, safe
}

// selectByRatio 按目标配比定向选择（合并 Step2 贪心 + Step3 比例调整，确定性优于"先贪心后调整"）。
// 各档配额 = round(L×ratio)，保档配额兜底 = L−rushT−stableT（吸收取整误差，保证 Σ=L）；
// 各档按 lessByUtilityThenKey 取配额内最优；某档候选不足配额时，空位按全局 utility 降序从剩余候选补齐。
// 返回：已选志愿表（未排序，交 Step4）+ 选中 GroupKey 集（供 buildSafePool 排除）。
func selectByRatio(candidates []CandidateWithScore, ratio RatioConfig, limit int) ([]CandidateWithScore, map[GroupKey]struct{}) {
	rush, stable, safe := splitByCategory(candidates)
	sortByUtility(rush)
	sortByUtility(stable)
	sortByUtility(safe)

	rushT := int(math.Round(float64(limit) * ratio.Rush))
	stableT := int(math.Round(float64(limit) * ratio.Stable))
	safeT := limit - rushT - stableT
	if safeT < 0 {
		safeT = 0
	}

	selected := make([]CandidateWithScore, 0, limit)
	picked := make(map[GroupKey]struct{}, limit)
	take := func(pool []CandidateWithScore, n int) {
		for _, c := range pool {
			if n <= 0 || len(selected) >= limit {
				return
			}
			if _, exists := picked[groupKeyOf(c)]; exists {
				continue
			}
			selected = append(selected, c)
			picked[groupKeyOf(c)] = struct{}{}
			n--
		}
	}
	take(rush, rushT)
	take(stable, stableT)
	take(safe, safeT)

	// 空位回填：某档不足配额时，从剩余候选按全局 utility 降序补齐至 limit。
	if len(selected) < limit {
		rest := make([]CandidateWithScore, 0, len(candidates))
		for _, c := range candidates { // 遍历输入序（DH1），后排序，不 range map
			if _, ok := picked[groupKeyOf(c)]; !ok {
				rest = append(rest, c)
			}
		}
		sortByUtility(rest)
		for _, c := range rest {
			if len(selected) >= limit {
				break
			}
			if _, exists := picked[groupKeyOf(c)]; exists {
				continue
			}
			selected = append(selected, c)
			picked[groupKeyOf(c)] = struct{}{}
		}
	}
	return selected, picked
}

// buildSafePool 构造稳拿池（Step5 前）：从全部候选筛出「p≥threshold 且未入选 wishlist」的组，
// 按 Probability 降序（并列走 lessByUtilityThenKey 保证确定性）供 checkSafeGuard 追加。
// 契约：池内均 p≥threshold，故追加即达标、safeGuard++ 不虚高（§3.5）。
func buildSafePool(candidates []CandidateWithScore, selected map[GroupKey]struct{}, threshold float64) []CandidateWithScore {
	pool := make([]CandidateWithScore, 0)
	for _, c := range candidates {
		if c.Probability < threshold {
			continue
		}
		if _, ok := selected[groupKeyOf(c)]; ok {
			continue
		}
		pool = append(pool, c)
	}
	sort.Slice(pool, func(i, j int) bool {
		if pool[i].Probability != pool[j].Probability {
			return pool[i].Probability > pool[j].Probability
		}
		return lessByUtilityThenKey(pool[i], pool[j])
	})
	return pool
}

// checkSafeGuard 未满时补齐，已满时替换效用最低的非稳拿项；不突破 limit。
// 完成后统一按效用排序、重排 FinalRank，再从最终表计算报告。保底不足仍为告警。
func checkSafeGuard(wishlist *[]CandidateWithScore, safePool []CandidateWithScore, minSafe int, threshold float64, limit int) OptimizationReport {
	safeGuard := 0
	for _, c := range *wishlist {
		if c.Probability >= threshold {
			safeGuard++
		}
	}

	sortByUtility(*wishlist)
	for _, c := range safePool {
		if safeGuard >= minSafe {
			break
		}
		if len(*wishlist) < limit {
			*wishlist = append(*wishlist, c)
		} else {
			replace := -1
			for i := len(*wishlist) - 1; i >= 0; i-- {
				if (*wishlist)[i].Probability < threshold {
					replace = i
					break
				}
			}
			if replace < 0 {
				break
			}
			(*wishlist)[replace] = c
		}
		safeGuard++
	}
	sortByUtility(*wishlist)
	for i := range *wishlist {
		(*wishlist)[i].FinalRank = i + 1
	}

	// 独立事件假设下的简化滑档率，不是实际录取保证。
	slipRate := 1.0
	satSum := 0.0
	for _, c := range *wishlist {
		slipRate *= (1 - c.Probability)
		satSum += c.Score // 内嵌 UtilityScore.Score（效用总分）
	}
	expSat := 0.0
	if n := len(*wishlist); n > 0 {
		expSat = satSum / float64(n)
	}
	return OptimizationReport{
		SafeCount:            safeGuard,
		SafeGuardOK:          safeGuard >= minSafe,
		SlipRate:             slipRate,
		ExpectedSatisfaction: expSat,
	}
}

// lessByUtilityThenKey 全序比较器（Q24/DH2-A）：UtilityScore 降序，并列时 GroupKey{UniversityCode,MajorGroup} 升序。
// 彻底确定、不依赖输入序，sort.Slice 无并列残留，保证可复现。
func lessByUtilityThenKey(a, b CandidateWithScore) bool {
	if a.Score != b.Score {
		return a.Score > b.Score
	}
	if a.UniversityCode != b.UniversityCode {
		return a.UniversityCode < b.UniversityCode
	}
	return a.MajorGroup < b.MajorGroup
}

// sortByUtility 按 lessByUtilityThenKey 原地排序（便捷封装）。
func sortByUtility(s []CandidateWithScore) {
	sort.Slice(s, func(i, j int) bool { return lessByUtilityThenKey(s[i], s[j]) })
}

// groupKeyOf 提取候选的院校专业组唯一标识（选中集/回填的 key）。
func groupKeyOf(c CandidateWithScore) GroupKey {
	return GroupKey{UniversityCode: c.UniversityCode, MajorGroup: c.MajorGroup}
}

// countByCategory 统计志愿表中某分档数量。
func countByCategory(wishlist []CandidateWithScore, category string) int {
	n := 0
	for _, c := range wishlist {
		if c.Category == category {
			n++
		}
	}
	return n
}
