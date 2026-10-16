// Package prob：录取概率估计。
//
// 设计见 docs/algorithms.md §3。纯函数、无 IO。
// 要算的是 P = P(q_school ≥ q_user)：校线百分位不差于你，即"你够得着"。
package prob

import "errors"

var (
	// ErrNotImplemented 占位错误。
	ErrNotImplemented = errors.New("prob: not implemented")
	// ErrNoReliableData 无可靠数据：宁可明说"依据不足"，绝不编造概率。
	ErrNoReliableData = errors.New("prob: no reliable data")
)

// Method 估计方法。
type Method int

const (
	MethodAuto      Method = iota // 按样本量自动选择
	MethodNormal                  // 正态近似（n≥5）
	MethodKDE                     // 核密度（n≥5，疑似多峰）
	MethodEmpirical               // 经验加权（1≤n<5）
)

// Sample 一个历史年份的样本（位次已归一化为百分位）。
type Sample struct {
	Year   int
	Q      float64 // q_i = R_i / T_i
	Weight float64 // 时效权重（可由调用方预计算：λ^age）
}

// Input 估计输入。
type Input struct {
	UserQ   float64  // 用户位次百分位 q_u
	Samples []Sample // 历史样本（建议最近 5 年）
	Method  Method   // 估计方法
}

// ProbResult 估计结果（必须可审计）。
type ProbResult struct {
	Prob       float64
	CILow      float64
	CIHigh     float64
	Basis      string // 依据：样本年份 / 方法 / 是否做趋势修正
	MethodUsed Method
}

// Estimate 基于历史样本估计录取概率。
//
// 步骤：
//  1. 有效样本不足 → ErrNoReliableData；
//  2. 按 n 选方法：正态 / KDE / 经验加权；
//  3. （可选）对 q_i 做趋势修正；
//  4. 输出 prob + 置信区间 + basis。
func Estimate(in Input) (ProbResult, error) {
	// TODO(M4): 见 docs/algorithms.md §3.2/§3.3/§3.4。
	return ProbResult{}, ErrNotImplemented
}
