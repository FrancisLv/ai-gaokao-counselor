package service

import (
	"context"
	"errors"

	kerrors "github.com/go-kratos/kratos/v2/errors"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	errs "github.com/choucisan/ai-gaokao-counselor/internal/pkg/errors"
)

// toKratosErr 把 biz 层框架无关的领域 sentinel 映射为 Kratos errors（DE4，见 task2 §4.4）。
// reason 用稳定枚举（DE2），上层按 reason 分支、不靠 message 文案。
// 普通错误沿用 Kratos 默认转换；两类前置条件错误单独覆盖 gRPC status，保留 HTTP 400 与错误详情。
func toKratosErr(err error) error {
	if err == nil {
		return nil
	}
	switch {
	case errors.Is(err, context.Canceled):
		return kerrors.New(499, "CANCELED", "请求已取消")
	case errors.Is(err, context.DeadlineExceeded):
		return kerrors.New(504, "DEADLINE_EXCEEDED", "请求超时")
	case errors.Is(err, errs.ErrInvalidQuery):
		return kerrors.New(400, "INVALID_QUERY", "仅支持湖南本科批、合法科类与报考年份")
	case errors.Is(err, errs.ErrInvalidSubjects):
		return kerrors.New(400, "INVALID_SUBJECTS", "请提供与科类一致的一门首选和两门不同再选科目")
	case errors.Is(err, errs.ErrInvalidCandidate):
		return kerrors.New(400, "INVALID_CANDIDATE", "候选标识、分档或效用数据无效")
	case errors.Is(err, errs.ErrDuplicateCandidate):
		return kerrors.New(400, "DUPLICATE_CANDIDATE", "候选院校专业组重复")
	case errors.Is(err, errs.ErrInvalidProbability):
		return kerrors.New(400, "INVALID_PROBABILITY", "概率必须为有限数且位于 [0.01,0.99]")
	case errors.Is(err, errs.ErrInvalidConfig):
		return kerrors.New(500, "INVALID_CONFIG", "算法配置无效")
	case errors.Is(err, errs.ErrMissingEnrollmentPlan):
		return kerrors.New(500, "MISSING_ENROLLMENT_PLAN", "报考年招生计划未入库")
	case errors.Is(err, errs.ErrBelowControlLine):
		return &preconditionError{err: kerrors.New(400, "SCORE_BELOW_CONTROL_LINE", "分数低于报考年本科线，MVP 仅支持本科批")}
	case errors.Is(err, errs.ErrScoreAboveMax):
		return kerrors.New(400, "SCORE_ABOVE_MAX", "分数超过上限(750)，请检查输入")
	case errors.Is(err, errs.ErrCandidateWithoutProb):
		return &preconditionError{err: kerrors.New(400, "CANDIDATE_WITHOUT_PROB", "候选缺少概率（池外候选），请先经 EstimateProb")}
	case errors.Is(err, errs.ErrMissingScoreRankMap):
		return kerrors.New(500, "MISSING_SCORE_RANK_MAP", "报考年一分一段表未入库")
	case errors.Is(err, errs.ErrNoReliableData):
		return kerrors.New(500, "NO_RELIABLE_DATA", "录取历史不足，无可靠概率")
	case errors.Is(err, errs.ErrNotImplemented):
		return kerrors.New(501, "NOT_IMPLEMENTED", "功能尚未实现")
	default:
		return kerrors.New(500, "INTERNAL", "算法服务暂不可用")
	}
}

// preconditionError 仅在本服务边界覆盖前置条件状态，不改全局 HTTP→gRPC 转换器。
// Unwrap 保持 errors.As/Is、kerrors.Code/Reason 对底层 Kratos 错误的兼容性。
type preconditionError struct {
	err *kerrors.Error
}

func (e *preconditionError) Error() string { return e.err.Error() }

func (e *preconditionError) Unwrap() error { return e.err }

func (e *preconditionError) GRPCStatus() *status.Status {
	// Proto 返回副本：只改 status code，复用 Kratos 构造的 message、ErrorInfo.reason/metadata。
	s := e.err.GRPCStatus().Proto()
	s.Code = int32(codes.FailedPrecondition)
	return status.FromProto(s)
}
