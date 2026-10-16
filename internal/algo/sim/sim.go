// Package sim：平行志愿投档模拟（分数优先、遵循志愿）。
//
// 用途：对优化后的志愿表做一次投档模拟，验证方案可行性（是否符合投档规则、是否会滑档）。
package sim

import "errors"

// ErrNotImplemented 占位错误。
var ErrNotImplemented = errors.New("sim: not implemented")

// Result 模拟结果（每个志愿是否命中、最终去向）。
type Result struct {
	HitIndex int
	Admitted bool
}

// Simulate 按投档规则模拟一遍志愿表。
func Simulate(wishlist any) (Result, error) {
	// TODO(M4): 依次扫描志愿，模拟投档。
	return Result{}, ErrNotImplemented
}
