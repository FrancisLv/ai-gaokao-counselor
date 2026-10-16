// Package errs 统一错误（跨服务一致的语义）。
package errs

import "errors"

// 预定义错误：这些是「合法且必须被明确表达」的结论，不是异常。
var (
	// ErrBudgetExceeded 预算超限：截断后续循环，返回当前最优结果并说明。
	ErrBudgetExceeded = errors.New("budget exceeded")

	// ErrNoReliableData 无可靠数据：宁可明确说"没有可靠数据，不予推荐"，也不编造。
	ErrNoReliableData = errors.New("no reliable data")

	// ErrNotImplemented 占位（骨架期）。
	ErrNotImplemented = errors.New("not implemented")
)
