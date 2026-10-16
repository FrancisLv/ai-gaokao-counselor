// Package rank：分数 ↔ 位次 换算与跨年位次可比化。
//
// 设计：纯函数、无 IO —— 一分一段表由调用方（data 层）查好后注入，便于独立单测与复现。
package rank

import "errors"

// ErrNotImplemented 占位错误。
var ErrNotImplemented = errors.New("rank: not implemented")

// Segment 一分一段表的一行。
type Segment struct {
	Score           int
	CumulativeCount int // 累计人数（即位次）
}

// ConvertByScore 按「省 / 年 / 科类」的一分一段表把分数换算为位次。
//
// 规则要点：
//   - 同分并列：取该分数段的累计人数（最后一名）；
//   - 表中无该分数 → 在相邻分段间线性插值；
//   - 跨年比较时用同位次法 / 线差法换算为可比位次。
func ConvertByScore(score int, table []Segment) (int, error) {
	// TODO(M1): 查表 + 插值；处理边界。
	return 0, ErrNotImplemented
}
