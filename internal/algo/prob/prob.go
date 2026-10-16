// Package prob：某院校专业的录取概率估计。
//
// 方法：历史录取位次分布 → 分位数 / 正态 / 核密度估计，输出概率 + 置信区间 + 依据样本年份。
package prob

import "errors"

// ErrNotImplemented 占位错误。
var ErrNotImplemented = errors.New("prob: not implemented")

// Estimate 基于历史录取位次样本估计录取概率。
//
// 返回的 basis 必须可审计：说明用了哪些年份的样本、分位数取值等——
// 概率数值由算法给，人话解释由 LLM 写（见 docs/ADR）。
func Estimate(userRank int, samples []int) (p float64, basis string, err error) {
	// TODO(M4): 分位数 / 核密度；样本不足时明确返回"依据不足"，而不是给一个假概率。
	return 0, "", ErrNotImplemented
}
