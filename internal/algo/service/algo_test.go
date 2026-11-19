package service

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"

	kerrors "github.com/go-kratos/kratos/v2/errors"
	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"

	algov1 "github.com/choucisan/ai-gaokao-counselor/api/algo/v1"
	"github.com/choucisan/ai-gaokao-counselor/conf"
	"github.com/choucisan/ai-gaokao-counselor/internal/algo/biz"
	errs "github.com/choucisan/ai-gaokao-counselor/internal/pkg/errors"
)

// algo_test.go —— service 层 RPC 测试（薄契约：pb→biz 映射 + 调 usecase + biz→pb 映射 + toKratosErr）。
// CheckConstraints/OptimizeWishlist 的 usecase 是纯函数（不碰 Repo），故无需 DB 即可测；
// EstimateProb 需 rank/admission Repo，由 data 层集成测试覆盖（真实库端到端）。

// pbCWP 构造 pb.CandidateWithProb（选科要求默认「首选物理，再选不限」）。
func pbCWP(code, group, category string, prob float64) *algov1.CandidateWithProb {
	return &algov1.CandidateWithProb{
		Candidate: &algov1.Candidate{UniversityCode: code, MajorGroup: group, SubjectReq: []string{"首选物理，再选不限"}},
		Prob:      &algov1.ProbEstimate{Probability: prob, Category: category, Method: "weighted_hit", DataYears: 3},
	}
}

type serviceEligibilityRepo struct {
	set biz.EligibilityRuleSet
}

func (r serviceEligibilityRepo) LoadEligibilityRuleSet(context.Context, string) (biz.EligibilityRuleSet, error) {
	if len(r.set.ProfileFields) == 0 {
		r.set.ProfileFields = biz.BuiltinEligibilityProfileFields()
	}
	return r.set, nil
}

func TestService_EvaluateEligibility(t *testing.T) {
	uc := biz.NewEligibilityUsecase(serviceEligibilityRepo{set: biz.EligibilityRuleSet{
		ReleaseStatus: "PUBLISHED",
		Rules: []biz.RuleVersion{{
			RuleID: "rule-subject", Version: 1, RuleKey: "subject-requirement",
			RuleType: biz.RuleTypeSubjectRequirement, ReviewState: biz.ReviewStatePublished,
			RequiredProfileFields: []string{biz.FieldSubjects, biz.FieldCategory},
			ExpressionVersion:     biz.ExpressionVersionV1,
			When: biz.RuleExpression{Operator: biz.OperatorNot, Child: &biz.RuleExpression{
				Operator: biz.OperatorSubjectMatch,
			}},
			SourceEvidence: []biz.EvidenceRef{{EvidenceID: "ev-1", EvidenceType: "DOCUMENT", ReleaseID: "rel-test"}},
		}},
		Coverage: []biz.RuleCoverage{{RuleType: biz.RuleTypeSubjectRequirement, Status: biz.CoverageFull}},
	}})
	svc := NewAlgo(nil, nil, nil, uc)
	reply, err := svc.EvaluateEligibility(context.Background(), &algov1.EvaluateEligibilityRequest{
		ReleaseId: "rel-test", ConfirmedProfileVersion: "profile-v1",
		ProfileLifecycle: algov1.EligibilityProfileLifecycle_ELIGIBILITY_PROFILE_LIFECYCLE_CONFIRMED,
		Scope:            &algov1.EligibilityScope{Province: "湖南", ApplicationYear: 2025, Category: "物理类", Batch: "本科批"},
		ProfileFacts: []*algov1.EligibilityProfileFact{
			{
				FieldKey: "subjects", State: algov1.EligibilityProfileFieldState_ELIGIBILITY_PROFILE_FIELD_STATE_PROVIDED,
				StringValues: []string{"物理", "化学", "生物"},
			},
			{
				FieldKey: "category", State: algov1.EligibilityProfileFieldState_ELIGIBILITY_PROFILE_FIELD_STATE_PROVIDED,
				StringValue: "物理类",
			},
		},
		CandidateGroups: []*algov1.EligibilityCandidateGroup{{
			UniversityId: 1, UniversityCode: "1001", UniversityName: "测试大学", MajorGroup: "01",
			Majors: []*algov1.EligibilityCandidateMajor{{
				MajorCode: "080901", MajorName: "计算机科学与技术",
				SubjectRequirement: "首选物理，再选化学", InCurrentPlan: true,
			}},
		}},
	})
	if err != nil {
		t.Fatalf("EvaluateEligibility: %v", err)
	}
	if reply.GetStatus() != algov1.EligibilityResultStatus_ELIGIBILITY_RESULT_STATUS_COMPLETE {
		t.Fatalf("status = %v, want COMPLETE", reply.GetStatus())
	}
	if got := reply.GetGroups()[0].GetStatus(); got != algov1.GroupEligibilityStatus_GROUP_ELIGIBILITY_STATUS_PASS {
		t.Fatalf("group status = %v, want PASS", got)
	}
	if got := reply.GetGroups()[0].GetMajors()[0].GetStatus(); got != algov1.MajorEligibilityStatus_MAJOR_ELIGIBILITY_STATUS_PASS {
		t.Fatalf("major status = %v, want PASS", got)
	}
}

func TestService_EvaluateEligibility_BlocksInvalidASTContract(t *testing.T) {
	uc := biz.NewEligibilityUsecase(serviceEligibilityRepo{set: biz.EligibilityRuleSet{
		ReleaseStatus: "PUBLISHED",
		Rules: []biz.RuleVersion{{
			RuleID: "rule-invalid", Version: 1, RuleKey: "invalid-expression",
			RuleType: biz.RuleTypeSubjectRequirement, ReviewState: biz.ReviewStatePublished,
			RequiredProfileFields: []string{biz.FieldCategory},
			ExpressionVersion:     biz.ExpressionVersionV1,
			When: biz.RuleExpression{
				Operator: "SQL",
				Field:    biz.FieldCategory,
			},
		}},
		Coverage: []biz.RuleCoverage{{RuleType: biz.RuleTypeSubjectRequirement, Status: biz.CoverageFull}},
	}})
	svc := NewAlgo(nil, nil, nil, uc)
	reply, err := svc.EvaluateEligibility(context.Background(), &algov1.EvaluateEligibilityRequest{
		ReleaseId: "rel-test", ConfirmedProfileVersion: "profile-v1",
		ProfileLifecycle: algov1.EligibilityProfileLifecycle_ELIGIBILITY_PROFILE_LIFECYCLE_CONFIRMED,
		Scope:            &algov1.EligibilityScope{Province: "湖南", ApplicationYear: 2025, Category: "物理类", Batch: "本科批"},
		ProfileFacts: []*algov1.EligibilityProfileFact{{
			FieldKey: biz.FieldCategory, State: algov1.EligibilityProfileFieldState_ELIGIBILITY_PROFILE_FIELD_STATE_PROVIDED,
			StringValue: "物理类",
		}},
		CandidateGroups: []*algov1.EligibilityCandidateGroup{{
			UniversityId: 1, UniversityCode: "1001", UniversityName: "测试大学", MajorGroup: "01",
			Majors: []*algov1.EligibilityCandidateMajor{{
				MajorCode: "080901", MajorName: "计算机科学与技术", InCurrentPlan: true,
			}},
		}},
	})
	if err != nil {
		t.Fatalf("EvaluateEligibility: %v", err)
	}
	if reply.GetStatus() != algov1.EligibilityResultStatus_ELIGIBILITY_RESULT_STATUS_BLOCKED {
		t.Fatalf("status = %v, want BLOCKED", reply.GetStatus())
	}
	reasons := reply.GetGroups()[0].GetMajors()[0].GetUnknownReasons()
	if len(reasons) != 1 || reasons[0] != biz.ReasonRuleExpression {
		t.Fatalf("reasons = %v, want [%s]", reasons, biz.ReasonRuleExpression)
	}
}

func TestService_CheckConstraints(t *testing.T) {
	rec := biz.NewRecommendUsecase(nil, nil, &conf.Bootstrap{})
	s := NewAlgo(nil, rec, nil, nil)

	req := &algov1.CheckConstraintsRequest{
		Candidates: []*algov1.CandidateWithProb{
			pbCWP("A", "1", "冲", 0.3), // 首选物理再选不限 → 匹配
			{ // 要求思想政治，用户未选 → 剔除
				Candidate: &algov1.Candidate{UniversityCode: "B", MajorGroup: "1", SubjectReq: []string{"首选物理，再选思想政治"}},
				Prob:      &algov1.ProbEstimate{Probability: 0.5, Category: "稳", Method: "weighted_hit"},
			},
		},
		Subjects: []string{"物理", "化学", "生物"},
		Category: "物理类",
	}
	reply, err := s.CheckConstraints(context.Background(), req)
	if err != nil {
		t.Fatalf("CheckConstraints: %v", err)
	}
	if len(reply.GetKept()) != 1 || reply.GetKept()[0].GetCandidate().GetUniversityCode() != "A" {
		t.Errorf("kept 应只含 A，got %+v", reply.GetKept())
	}
	// 回填 prob：kept 项概率应从入参池补回（契约形状完整）
	if got := reply.GetKept()[0].GetProb().GetProbability(); got != 0.3 {
		t.Errorf("kept prob 未回填：got %v, want 0.3", got)
	}
	if len(reply.GetRejected()) != 1 || reply.GetRejected()[0].GetCandidate().GetCandidate().GetUniversityCode() != "B" {
		t.Fatalf("rejected 应只含 B，got %+v", reply.GetRejected())
	}
	if reply.GetRejected()[0].GetRule() != biz.RuleSubjectReq {
		t.Errorf("rejected.Rule = %q, want %q", reply.GetRejected()[0].GetRule(), biz.RuleSubjectReq)
	}
	if reply.GetRejected()[0].GetReason() == "" {
		t.Error("rejected.Reason 应有人话原因")
	}
}

func TestService_RankAndEstimateRequireRelease(t *testing.T) {
	s := NewAlgo(nil, nil, nil, nil)
	if _, err := s.RankConvert(context.Background(), &algov1.RankConvertRequest{
		Province: "湖南", Year: "2025", Category: "物理类", Score: 600,
	}); kerrors.Reason(err) != "RELEASE_REQUIRED" {
		t.Fatalf("RankConvert reason = %q", kerrors.Reason(err))
	}
	if _, err := s.EstimateProb(context.Background(), &algov1.EstimateProbRequest{
		Province: "湖南", Year: "2025", Category: "物理类", Score: 600,
	}); kerrors.Reason(err) != "RELEASE_REQUIRED" {
		t.Fatalf("EstimateProb reason = %q", kerrors.Reason(err))
	}
}

func TestService_OptimizeWishlist(t *testing.T) {
	opt := biz.NewOptimizeUsecase(&conf.Bootstrap{})
	s := NewAlgo(nil, nil, opt, nil)

	req := &algov1.OptimizeWishlistRequest{
		Candidates: []*algov1.CandidateWithProb{
			pbCWP("A", "1", "保", 0.95),
			pbCWP("B", "1", "冲", 0.30),
			pbCWP("C", "1", "稳", 0.60),
		},
		Profile: &algov1.UserProfile{RiskAppetite: "balanced", PriorityDims: []string{"tier"}},
	}
	reply, err := s.OptimizeWishlist(context.Background(), req)
	if err != nil {
		t.Fatalf("OptimizeWishlist: %v", err)
	}
	wl := reply.GetWishlist()
	if len(wl.GetCandidates()) == 0 {
		t.Fatal("志愿表为空")
	}
	// FinalRank 连续 + utility 已注入
	for i, c := range wl.GetCandidates() {
		if c.GetFinalRank() != int32(i+1) {
			t.Errorf("Candidates[%d].FinalRank = %d, want %d", i, c.GetFinalRank(), i+1)
		}
	}
	if wl.GetReport() == nil {
		t.Error("report 应非空")
	}
}

func TestService_OptimizeWishlist_CandidateWithoutProb(t *testing.T) {
	opt := biz.NewOptimizeUsecase(&conf.Bootstrap{})
	s := NewAlgo(nil, nil, opt, nil)

	req := &algov1.OptimizeWishlistRequest{
		Candidates: []*algov1.CandidateWithProb{
			{ // Probability 零值 → 池外候选无概率
				Candidate: &algov1.Candidate{UniversityCode: "A", MajorGroup: "1"},
				Prob:      &algov1.ProbEstimate{Probability: 0, Category: "冲"},
			},
		},
		Profile: &algov1.UserProfile{RiskAppetite: "balanced"},
	}
	_, err := s.OptimizeWishlist(context.Background(), req)
	if err == nil {
		t.Fatal("池外候选无概率应报错")
	}
	// toKratosErr 映射：稳定 reason 枚举 CANDIDATE_WITHOUT_PROB（DE2）
	if got := kerrors.Reason(err); got != "CANDIDATE_WITHOUT_PROB" {
		t.Errorf("reason = %q, want CANDIDATE_WITHOUT_PROB", got)
	}
}

func TestService_OptimizeWishlist_SafeGuardWarningNotError(t *testing.T) {
	// 保底不足（无 p≥0.9 候选）→ SafeGuardOK=false 是告警非 error（DE3）
	opt := biz.NewOptimizeUsecase(&conf.Bootstrap{})
	s := NewAlgo(nil, nil, opt, nil)

	req := &algov1.OptimizeWishlistRequest{
		Candidates: []*algov1.CandidateWithProb{
			pbCWP("A", "1", "冲", 0.30),
			pbCWP("B", "1", "稳", 0.55),
		},
		Profile: &algov1.UserProfile{RiskAppetite: "aggressive"},
	}
	reply, err := s.OptimizeWishlist(context.Background(), req)
	if err != nil {
		t.Fatalf("保底不足不应返回 error（DE3），got %v", err)
	}
	if reply.GetWishlist().GetReport().GetSafeGuardOk() {
		t.Error("无 p≥0.9 候选 → SafeGuardOK 应为 false")
	}
}

func TestRegression_InternalErrorRedaction(t *testing.T) {
	err := toKratosErr(errors.New("database password=TEST_PRIVATE_MARKER"))
	if strings.Contains(err.Error(), "TEST_PRIVATE_MARKER") || strings.Contains(status.Convert(err).Message(), "TEST_PRIVATE_MARKER") {
		t.Fatal("内部错误暴露敏感信息")
	}
}

// 默认单测即验证完整码表，避免状态码回归只能在有数据库的集成测试中发现。
func TestToKratosErr_CodesAndReasons(t *testing.T) {
	if got := toKratosErr(nil); got != nil {
		t.Fatalf("nil 错误被转换为 %v", got)
	}
	for _, tc := range []struct {
		err    error
		code   codes.Code
		http   int
		reason string
	}{
		{errs.ErrBelowControlLine, codes.FailedPrecondition, 400, "SCORE_BELOW_CONTROL_LINE"},
		{errs.ErrCandidateWithoutProb, codes.FailedPrecondition, 400, "CANDIDATE_WITHOUT_PROB"},
		{errs.ErrScoreAboveMax, codes.InvalidArgument, 400, "SCORE_ABOVE_MAX"},
		{errs.ErrMissingScoreRankMap, codes.Internal, 500, "MISSING_SCORE_RANK_MAP"},
		{errs.ErrNoReliableData, codes.Internal, 500, "NO_RELIABLE_DATA"},
		{errs.ErrNotImplemented, codes.Unimplemented, 501, "NOT_IMPLEMENTED"},
		{errs.ErrInvalidQuery, codes.InvalidArgument, 400, "INVALID_QUERY"},
		{errs.ErrInvalidSubjects, codes.InvalidArgument, 400, "INVALID_SUBJECTS"},
		{errs.ErrProfileNotConfirmed, codes.InvalidArgument, 400, "PROFILE_NOT_CONFIRMED"},
		{errs.ErrInvalidCandidate, codes.InvalidArgument, 400, "INVALID_CANDIDATE"},
		{errs.ErrDuplicateCandidate, codes.InvalidArgument, 400, "DUPLICATE_CANDIDATE"},
		{errs.ErrInvalidProbability, codes.InvalidArgument, 400, "INVALID_PROBABILITY"},
		{errs.ErrInvalidConfig, codes.Internal, 500, "INVALID_CONFIG"},
		{errs.ErrMissingEnrollmentPlan, codes.Internal, 500, "MISSING_ENROLLMENT_PLAN"},
		{context.Canceled, codes.Canceled, 499, "CANCELED"},
		{context.DeadlineExceeded, codes.DeadlineExceeded, 504, "DEADLINE_EXCEEDED"},
		{errors.New("测试内部错误"), codes.Internal, 500, "INTERNAL"},
	} {
		t.Run(tc.reason, func(t *testing.T) {
			for _, input := range []error{tc.err, fmt.Errorf("usecase: %w", tc.err)} {
				got := toKratosErr(input)
				var ke *kerrors.Error
				if !errors.As(got, &ke) {
					t.Fatalf("errors.As 无法取到底层 Kratos 错误: %v", got)
				}
				if !errors.Is(got, kerrors.New(tc.http, tc.reason, "文案不参与匹配")) {
					t.Errorf("errors.Is 未保留 code/reason 匹配: %v", got)
				}
				if status.Convert(got).Message() != ke.Message {
					t.Error("gRPC message 与 Kratos message 不一致")
				}
				// 输出再次包装、转为 gRPC 错误后，code/reason 仍须一致。
				for _, e := range []error{got, fmt.Errorf("外层: %w", got), status.Convert(got).Err()} {
					if status.Code(e) != tc.code || kerrors.Code(e) != tc.http || kerrors.Reason(e) != tc.reason {
						t.Errorf("错误契约不符: grpc=%s http=%d reason=%q，期望 %s/%d/%s",
							status.Code(e), kerrors.Code(e), kerrors.Reason(e), tc.code, tc.http, tc.reason)
					}
				}
			}
		})
	}
}

func TestPreconditionError_PreservesDetails(t *testing.T) {
	base := kerrors.New(400, "CANDIDATE_WITHOUT_PROB", "候选缺少概率").WithMetadata(map[string]string{"group": "A/1"})
	before := base.GRPCStatus().Proto()
	wrapped := &preconditionError{err: base}
	st := status.Convert(wrapped)
	if st.Code() != codes.FailedPrecondition || st.Message() != base.Message {
		t.Fatalf("前置条件状态或文案不符: %v", st)
	}
	if wrapped.Error() != base.Error() || !errors.Is(wrapped, base) {
		t.Error("错误文本或 Unwrap 链未保留")
	}
	details := st.Details()
	if len(details) != 1 {
		t.Fatalf("ErrorInfo 数量 = %d, want 1", len(details))
	}
	info, ok := details[0].(*errdetails.ErrorInfo)
	if !ok || info.GetReason() != base.Reason || info.GetMetadata()["group"] != "A/1" {
		t.Fatalf("reason/metadata 未保留: %v", details)
	}
	restored := kerrors.FromError(st.Err())
	if restored.Code != 400 || restored.Reason != base.Reason || restored.Metadata["group"] != "A/1" {
		t.Fatalf("gRPC→Kratos 还原失败: %v", restored)
	}
	if !proto.Equal(before, base.GRPCStatus().Proto()) {
		t.Error("覆盖 gRPC code 修改了底层 Kratos 错误")
	}
	if status.Code(kerrors.BadRequest("INVALID_YEAR", "非法年份")) != codes.InvalidArgument {
		t.Error("其他 HTTP 400 的全局默认映射被改变")
	}
}
