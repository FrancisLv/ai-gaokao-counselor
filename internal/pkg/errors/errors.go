// Package errs 统一错误（跨服务一致的语义）。
package errs

import "errors"

// 预定义错误：这些是「合法且必须被明确表达」的结论，不是异常。
var (
	// ErrBudgetExceeded 预算超限：截断后续循环，返回当前最优结果并说明。
	ErrBudgetExceeded = errors.New("budget exceeded")

	// ErrNoReliableData 无可靠数据：宁可明确说"没有可靠数据，不予推荐"，也不编造。
	ErrNoReliableData = errors.New("no reliable data")

	// —— 领域 sentinel（biz 层返回，框架无关；service 层 toKratosErr 映射为 reason/code，见 task2 §4.4）——

	// ErrBelowControlLine 分数低于报考年本科线（MVP 仅支持本科批）→ SCORE_BELOW_CONTROL_LINE（FailedPrecondition/400）。
	ErrBelowControlLine = errors.New("score below control line")

	// ErrScoreAboveMax 分数超过表内最高分(750)：必是脏输入/抽取错误，报错 surface，严禁外推 → SCORE_ABOVE_MAX（InvalidArgument/400）。
	ErrScoreAboveMax = errors.New("score above max")

	// ErrCandidateWithoutProb OptimizeWishlist 池外候选无 Probability（DA4）→ CANDIDATE_WITHOUT_PROB（FailedPrecondition/400）。
	ErrCandidateWithoutProb = errors.New("candidate without probability")

	// ErrMissingScoreRankMap 报考年一分一段表未入库（DY5，不静默回退）→ MISSING_SCORE_RANK_MAP（Internal/500）。
	ErrMissingScoreRankMap = errors.New("missing score rank map")

	// 输入与配置边界错误；消息由 service 层安全映射。
	ErrInvalidQuery          = errors.New("invalid query")
	ErrInvalidSubjects       = errors.New("invalid subjects")
	ErrInvalidCandidate      = errors.New("invalid candidate")
	ErrDuplicateCandidate    = errors.New("duplicate candidate")
	ErrInvalidProbability    = errors.New("invalid probability")
	ErrInvalidConfig         = errors.New("invalid algorithm config")
	ErrMissingEnrollmentPlan = errors.New("missing enrollment plan")
	ErrProfileNotConfirmed   = errors.New("profile not confirmed")

	// ErrNotImplemented 占位（骨架期）。
	ErrNotImplemented = errors.New("not implemented")
)
