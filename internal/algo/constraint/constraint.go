// Package constraint：硬规则裁决（规则表驱动）。
//
// 规则维度：选科匹配、单科成绩、体检限报、性别限制、批次、学费上限。
// 纪律：**确定性裁决，不交给 LLM**；每条剔除都必须带原因（供解释与审计）。
package constraint

import "errors"

// ErrNotImplemented 占位错误。
var ErrNotImplemented = errors.New("constraint: not implemented")

// Verdict 单个候选的裁决结果。
type Verdict struct {
	CandidateID string
	Passed      bool
	Reason      string // Passed=false 时必填
}

// Check 对候选集合逐条裁决，返回通过项与被剔除项（含原因）。
//
// profile 与 candidates 为结构化数据（由调用方从 JSON 反序列化，
// 或后续升级为强类型）。此处用 any 占位。
func Check(profile, candidates any) (kept, rejected []Verdict, err error) {
	// TODO(M4): 规则表驱动；保证每条规则可单测。
	return nil, nil, ErrNotImplemented
}
