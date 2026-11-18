package biz

import (
	"context"
	"math"

	errs "github.com/choucisan/ai-gaokao-counselor/internal/pkg/errors"
)

// ScoreRankEntry 一分一段表条目（对应 score_rank_map 表；§3.1）。
// ScoreRangeLow/High、RankRangeLow/High 由 data 层从 score_range/rank_range 字符串（如 "693-750"、"1-51"）解析而来。
type ScoreRankEntry struct {
	Score           int // 分数（= score_range 区间下限）
	ScoreRangeLow   int // 分数区间下限（如 693-750 中的 693）
	ScoreRangeHigh  int // 分数区间上限（如 693-750 中的 750）
	SegmentCount    int // 本分数段人数
	CumulativeCount int // 累计人数（即位次）
	RankRangeLow    int // 位次区间下限
	RankRangeHigh   int // 位次区间上限
}

// ConvertByScore 分数转位次（纯函数）；table + controlScore 均由 data 层查 score_rank_map 后注入。
//
// 步骤（§3.1）：
//  0. float→int 归整（Q23/DG2-A）：s := int(math.Round(score)) 一次性四舍五入，消除浮点毛刺、保证确定性。
//  1. s < controlScore（本科线）→ ErrBelowControlLine（MVP 仅支持本科批）。
//  2. s > 表内最高 ScoreRangeHigh（=750）→ ErrScoreAboveMax（DG1-A：严禁外推，与下界对称）。
//  3. 精确匹配 ScoreRangeLow ≤ s ≤ ScoreRangeHigh 的行：
//     - 单分行（low==high）：位次 = CumulativeCount；
//     - 合并区间（如 693-750）：区间内均匀分布插值 → rank = RankRangeLow + (high−s)/(high−low) × (RankRangeHigh−RankRangeLow)。
//  4. 百分位 = RankToPercentile(位次, 全省总人数)，分母 = 表内 MAX(CumulativeCount)（全省总排位，与批次无关）。
func ConvertByScore(score float64, table []ScoreRankEntry, controlScore int) (RankResult, error) {
	if len(table) == 0 {
		return RankResult{}, errs.ErrMissingScoreRankMap
	}

	if !finite(score) {
		return RankResult{}, errs.ErrInvalidQuery
	}
	if controlScore <= 0 {
		return RankResult{}, errs.ErrNoReliableData
	}
	// 0. float→int 一次性归整（后续匹配与插值统一用 s）
	s := int(math.Round(score))

	// 派生表内最高分（ScoreRangeHigh 上限）与全省总人数（CumulativeCount 最大值 = 最低分那行）
	maxHigh := 0
	totalCount := 0
	for _, e := range table {
		if e.ScoreRangeHigh > maxHigh {
			maxHigh = e.ScoreRangeHigh
		}
		if e.CumulativeCount > totalCount {
			totalCount = e.CumulativeCount
		}
	}

	// 1. 下界：低于本科线（MVP 仅支持本科批）
	if s < controlScore {
		return RankResult{}, errs.ErrBelowControlLine
	}
	// 2. 上界：> 表内最高分（=750）必是脏输入，报错 surface，严禁外推
	if s > maxHigh {
		return RankResult{}, errs.ErrScoreAboveMax
	}

	// 3. 精确匹配所在分数区间行
	rank := 0
	matched := false
	for _, e := range table {
		if s >= e.ScoreRangeLow && s <= e.ScoreRangeHigh {
			if e.ScoreRangeLow == e.ScoreRangeHigh {
				// 单分行：直接用累计人数（=位次）
				rank = e.CumulativeCount
			} else {
				// 合并区间：区间内均匀分布插值（位次随分数升高而减小）
				span := e.ScoreRangeHigh - e.ScoreRangeLow
				ratio := float64(e.ScoreRangeHigh-s) / float64(span)
				rank = e.RankRangeLow + int(math.Round(ratio*float64(e.RankRangeHigh-e.RankRangeLow)))
			}
			matched = true
			break
		}
	}
	if !matched {
		// 分数在 [controlScore, maxHigh] 内却无行覆盖 = 数据缺口（防御，正常不发生）
		return RankResult{}, errs.ErrNoReliableData
	}

	// 4. 百分位（分母 = 全省总人数）
	if rank <= 0 || totalCount <= 0 || rank > totalCount {
		return RankResult{}, errs.ErrNoReliableData
	}
	percentile := RankToPercentile(rank, totalCount)

	return RankResult{
		Rank:         rank,
		Percentile:   percentile,
		TotalCount:   totalCount,
		ControlScore: controlScore,
	}, nil
}

// RankToPercentile 位次转百分位（抽成独立函数，供 prob 模块复用，保证用户侧与历史侧口径一致）。
// 百分位 = 位次 / 全省总人数。
func RankToPercentile(rank int, totalCount int) float64 {
	if totalCount <= 0 {
		return 0
	}
	return float64(rank) / float64(totalCount)
}

// RankUsecase 承接 RankConvert RPC；被 RecommendUsecase 复用做分数→百分位换算（DZ4）。
type RankUsecase struct {
	repo RankRepo
}

// NewRankUsecase 构造（wire 注入 RankRepo）。
func NewRankUsecase(repo RankRepo) *RankUsecase {
	return &RankUsecase{repo: repo}
}

// Convert：LoadScoreRankMap(报考年) → ConvertByScore → RankResult。
// 报考年一分一段表缺失 → data 层返回 ErrMissingScoreRankMap（DY5，不静默回退）。
func (u *RankUsecase) Convert(ctx context.Context, q RankQuery) (RankResult, error) {
	if err := ctx.Err(); err != nil {
		return RankResult{}, err
	}
	if err := validateQuery(q.Province, q.Category, q.Year); err != nil {
		return RankResult{}, err
	}
	table, controlScore, err := u.repo.LoadScoreRankMap(ctx, q.Province, q.Category, q.Year)
	if err != nil {
		return RankResult{}, err
	}
	return ConvertByScore(q.Score, table, controlScore)
}
