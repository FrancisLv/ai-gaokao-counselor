// Package rank：分数 ↔ 位次 换算，以及 位次 → 百分位（跨年可比化的核心）。
//
// 设计见 docs/algorithms.md §2。纯函数、无 IO：一分一段表由调用方注入，便于单测与复现。
package rank

import "errors"

// ErrNotImplemented 占位错误。
var ErrNotImplemented = errors.New("rank: not implemented")

// Segment 一分一段表的一行。
type Segment struct {
	Score           int
	CumulativeCount int // 累计人数（即位次）
}

// ConvertByScore 分数 → 位次。
//
// 规则：落在某分段取该分段累计人数（=最差名次，保守）；表里没有该分数则在相邻分段间线性插值。
func ConvertByScore(score int, table []Segment) (int, error) {
	// TODO(M1): 查表 + 插值；处理边界（低于最低分 / 高于最高分）。
	return 0, ErrNotImplemented
}

// Normalize 位次 → 百分位：q = rank / total。
//
// 跨年可比化的核心：不同年份总人数不同，裸位次不可比；换算成百分位后年际差异被消除。
// 见 docs/algorithms.md §2。
func Normalize(rank, total int) float64 {
	if total <= 0 {
		return 0
	}
	return float64(rank) / float64(total)
}
