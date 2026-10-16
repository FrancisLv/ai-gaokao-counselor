// Package optimize：志愿表排序与配比（约束满足 + 启发式搜索）。
//
// 目标：在平行志愿规则下最大化期望效用，同时约束冲稳保配比、梯度平滑、避免同位次扎堆。
package optimize

import "errors"

// ErrNotImplemented 占位错误。
var ErrNotImplemented = errors.New("optimize: not implemented")

// Params 优化参数。
type Params struct {
	RushRatio int // 冲的配比（%）
	StableRatio int // 稳的配比（%）
	SafeRatio int // 保的配比（%）
}

// Wishlist 生成最终志愿表。
//
// 方法：贪心 + 局部搜索；结果必须可复现（固定随机种子 / 全确定性）。
func Wishlist(candidates, profile any, p Params) (any, error) {
	// TODO(M4): 约束满足 + 启发式搜索。
	return nil, ErrNotImplemented
}
