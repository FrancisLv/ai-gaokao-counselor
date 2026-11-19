//go:build integration
// +build integration

// 集成测试：真实连本地 PostgreSQL，经 service 层薄契约跑通 4 个 RPC 端到端
// （RankConvert → EstimateProb → CheckConstraints → OptimizeWishlist），验证 pb DTO 映射 + AuditBasis basis 管道。
// 默认不随 `go test ./...` 运行（需 -tags integration）。
//
// 运行：
//
//	ALGO_TEST_DSN="postgres://francis@localhost:5432/ai_gaokao_counselor?sslmode=disable" \
//	  go test ./internal/algo/service/ -tags integration -v
//
// 未设 ALGO_TEST_DSN 时自动 skip（CI 无库不失败）。
package service

import (
	"context"
	"errors"
	"fmt"
	"math"
	"net"
	"os"
	"sync"
	"testing"
	"time"

	kerrors "github.com/go-kratos/kratos/v2/errors"
	kgrpc "github.com/go-kratos/kratos/v2/transport/grpc"
	"github.com/jackc/pgx/v5/pgxpool"
	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/proto"

	algov1 "github.com/choucisan/ai-gaokao-counselor/api/algo/v1"
	"github.com/choucisan/ai-gaokao-counselor/conf"
	"github.com/choucisan/ai-gaokao-counselor/internal/algo/biz"
	"github.com/choucisan/ai-gaokao-counselor/internal/algo/data"
	errs "github.com/choucisan/ai-gaokao-counselor/internal/pkg/errors"
)

const integrationRelease = "rel_legacy_v1"

// setupService 用真实 data 层 Repo 组装 Service（等价 wire_gen.go 的依赖图）。
func setupService(t testing.TB) *Service {
	t.Helper()
	dsn := os.Getenv("ALGO_TEST_DSN")
	if dsn == "" {
		t.Skip("ALGO_TEST_DSN 未设置，跳过集成测试")
	}
	t.Setenv("ALGO_DATA_DSN", dsn)
	bc := &conf.Bootstrap{Data: &conf.Data{Database: &conf.Data_Database{Dsn: dsn}}}
	d, cleanup, err := data.NewData(bc)
	if err != nil {
		t.Fatalf("NewData: %v", err)
	}
	t.Cleanup(cleanup)

	rankUc := biz.NewRankUsecase(data.NewRankRepo(d))
	recUc := biz.NewRecommendUsecase(data.NewAdmissionRepo(d), rankUc, bc)
	optUc := biz.NewOptimizeUsecase(bc)
	eligibilityUc := biz.NewEligibilityUsecase(data.NewEligibilityRepo(d))
	return NewAlgo(rankUc, recUc, optUc, eligibilityUc)
}

type errorRankRepo struct{ err error }

func (r errorRankRepo) LoadScoreRankMap(context.Context, string, string, string, int) ([]biz.ScoreRankEntry, int, error) {
	return nil, 0, r.err
}

func TestIntegration_Bufconn_ExtendedErrors(t *testing.T) {
	for _, tc := range []struct {
		err    error
		code   codes.Code
		reason string
	}{
		{context.Canceled, codes.Canceled, "CANCELED"},
		{context.DeadlineExceeded, codes.DeadlineExceeded, "DEADLINE_EXCEEDED"},
		{errs.ErrMissingEnrollmentPlan, codes.Internal, "MISSING_ENROLLMENT_PLAN"},
		{errs.ErrInvalidConfig, codes.Internal, "INVALID_CONFIG"},
		{errors.New("私有错误"), codes.Internal, "INTERNAL"},
	} {
		client := setupBufconnClient(t, NewAlgo(biz.NewRankUsecase(errorRankRepo{tc.err}), nil, nil, nil))
		_, err := client.RankConvert(context.Background(), &algov1.RankConvertRequest{ReleaseId: integrationRelease, Province: "湖南", Category: "物理类", Year: "2025", Score: 600})
		assertBufconnError(t, err, tc.code, tc.reason)
	}
	client := setupBufconnClient(t, NewAlgo(nil, biz.NewRecommendUsecase(nil, nil, nil), biz.NewOptimizeUsecase(nil), nil))
	for _, p := range []float64{math.NaN(), math.Inf(1), 1.1, .001} {
		_, err := client.OptimizeWishlist(context.Background(), &algov1.OptimizeWishlistRequest{Candidates: []*algov1.CandidateWithProb{pbCWP("A", "1", "保", p)}})
		assertBufconnError(t, err, codes.InvalidArgument, "INVALID_PROBABILITY")
	}
	_, err := client.CheckConstraints(context.Background(), &algov1.CheckConstraintsRequest{Category: "物理类", Subjects: []string{"历史", "化学", "生物"}})
	assertBufconnError(t, err, codes.InvalidArgument, "INVALID_SUBJECTS")
}

func TestIntegration_ConcurrentClients(t *testing.T) {
	svc := setupService(t)
	clients := make([]algov1.AlgoClient, 8)
	for i := range clients {
		clients[i] = setupBufconnClient(t, svc)
	}
	req := &algov1.EstimateProbRequest{ReleaseId: integrationRelease, Province: "湖南", Category: "物理类", Year: "2025", Score: 600, Subjects: []string{"物理", "化学", "生物"}}
	baseline, err := clients[0].EstimateProb(context.Background(), req)
	if err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	for _, client := range clients {
		wg.Add(1)
		go func(c algov1.AlgoClient) {
			defer wg.Done()
			for i := 0; i < 3; i++ {
				ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
				reply, err := c.EstimateProb(ctx, req)
				cancel()
				if err != nil || !proto.Equal(reply, baseline) {
					t.Errorf("并发响应不同: %v", err)
					return
				}
			}
		}(client)
	}
	wg.Wait()
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	_, err = clients[0].EstimateProb(ctx, req)
	if status.Code(err) != codes.Canceled {
		t.Fatalf("取消未传播: %v", err)
	}
	ctx, cancel = context.WithDeadline(context.Background(), time.Now().Add(-time.Second))
	defer cancel()
	_, err = clients[0].EstimateProb(ctx, req)
	if status.Code(err) != codes.DeadlineExceeded {
		t.Fatalf("超时未传播: %v", err)
	}
}

func TestIntegration_HistoryCategoryAudit(t *testing.T) {
	s := setupService(t)
	for _, item := range []struct {
		category string
		subjects []string
	}{
		{"物理类", []string{"物理", "化学", "生物"}}, {"历史类", []string{"历史", "政治", "地理"}},
	} {
		r, err := s.EstimateProb(context.Background(), &algov1.EstimateProbRequest{ReleaseId: integrationRelease, Province: "湖南", Category: item.category, Year: "2025", Score: 600, Subjects: item.subjects})
		if err != nil {
			t.Fatal(err)
		}
		all := append(append(append([]*algov1.CandidateWithProb{}, r.RushCandidates...), r.StableCandidates...), r.SafeCandidates...)
		if len(all) == 0 {
			t.Fatal("真实科类无候选")
		}
		for _, c := range all {
			if len(c.Candidate.HistoryData) != int(c.Prob.DataYears) || c.Prob.DataYears == 0 {
				t.Fatal("审计年数不符")
			}
			for _, h := range c.Candidate.HistoryData {
				if h.Year < 2022 || h.Year > 2024 || h.TotalCount <= 0 || h.MinPercentile != float64(h.GroupMinRank)/float64(h.TotalCount) {
					t.Fatal("历史范围或分母错误")
				}
			}
		}
		t.Log(fmt.Sprintf("%s候选=%d", item.category, len(all)))
	}
}

func BenchmarkIntegration_EstimateProb(b *testing.B) {
	s := setupService(b)
	req := &algov1.EstimateProbRequest{ReleaseId: integrationRelease, Province: "湖南", Year: "2025", Category: "物理类", Batch: "本科批", Score: 600, Subjects: []string{"物理", "化学", "生物"}}
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if _, err := s.EstimateProb(context.Background(), req); err != nil {
			b.Fatal(err)
		}
	}
}

// TestIntegration_Service_FourRPCs 串起 service 层 4 个 RPC（真实库、真实 pb DTO 映射）。
func TestIntegration_Service_FourRPCs(t *testing.T) {
	s := setupService(t)
	ctx := context.Background()

	// ① RankConvert：分数 → 位次
	rcReply, err := s.RankConvert(ctx, &algov1.RankConvertRequest{
		ReleaseId: integrationRelease, Province: "湖南", Year: "2025", Category: "物理类", Score: 600,
	})
	if err != nil {
		t.Fatalf("RankConvert: %v", err)
	}
	t.Logf("① RankConvert 600 → rank=%d percentile=%.4f", rcReply.GetRank(), rcReply.GetPercentile())
	if rcReply.GetRank() <= 0 {
		t.Errorf("rank = %d, want >0", rcReply.GetRank())
	}

	// ② EstimateProb：分数 → 分档候选池 + basis
	epReply, err := s.EstimateProb(ctx, &algov1.EstimateProbRequest{
		ReleaseId: integrationRelease, Province: "湖南", Year: "2025", Category: "物理类", Batch: "本科批", Score: 600,
		Subjects: []string{"物理", "化学", "生物"},
		Profile:  &algov1.UserProfile{RiskAppetite: "balanced", PriorityDims: []string{"tier"}},
	})
	if err != nil {
		t.Fatalf("EstimateProb: %v", err)
	}
	t.Logf("② EstimateProb 分档：冲=%d 稳=%d 保=%d", epReply.GetRushTotal(), epReply.GetStableTotal(), epReply.GetSafeTotal())
	if epReply.GetRushTotal()+epReply.GetStableTotal()+epReply.GetSafeTotal() == 0 {
		t.Error("三档全空，概率计算可能有误")
	}
	// basis 管道验证（Phase 5 关键：用户 rank/percentile + SampleYears + method 经 RecommendResult.Basis 回填）
	basis := epReply.GetBasis()
	if basis.GetUserRank() <= 0 {
		t.Errorf("basis.user_rank 未回填：%+v", basis)
	}
	if basis.GetUserRank() != rcReply.GetRank() {
		t.Errorf("basis.user_rank=%d 与 RankConvert rank=%d 不一致（口径应统一）", basis.GetUserRank(), rcReply.GetRank())
	}
	if len(basis.GetSampleYears()) != 3 {
		t.Errorf("basis.sample_years = %v, want 3 年窗口 [2022 2023 2024]", basis.GetSampleYears())
	}
	if basis.GetMethod() != "weighted_hit" {
		t.Errorf("basis.method = %q, want weighted_hit", basis.GetMethod())
	}

	// 合并三档为单数组（模拟 orchestrator 合并回传）
	all := make([]*algov1.CandidateWithProb, 0)
	all = append(all, epReply.GetRushCandidates()...)
	all = append(all, epReply.GetStableCandidates()...)
	all = append(all, epReply.GetSafeCandidates()...)
	if len(all) == 0 {
		t.Fatal("合并候选池为空，无法继续 CheckConstraints/OptimizeWishlist")
	}

	// ③ CheckConstraints：候选已在 EstimateProb 内过选科过滤，此处 rejected 应≈0（幂等验证）
	ccReply, err := s.CheckConstraints(ctx, &algov1.CheckConstraintsRequest{
		Candidates: all, Subjects: []string{"物理", "化学", "生物"}, Category: "物理类",
	})
	if err != nil {
		t.Fatalf("CheckConstraints: %v", err)
	}
	t.Logf("③ CheckConstraints：kept=%d rejected=%d", len(ccReply.GetKept()), len(ccReply.GetRejected()))
	if len(ccReply.GetKept()) == 0 {
		t.Error("kept 为 0，选科过滤过严")
	}
	// kept 的 prob 应经 GroupKey 回填（非零）
	var backfilled int
	for _, k := range ccReply.GetKept() {
		if k.GetProb().GetProbability() > 0 {
			backfilled++
		}
	}
	if backfilled != len(ccReply.GetKept()) {
		t.Errorf("kept prob 回填不完整：%d/%d", backfilled, len(ccReply.GetKept()))
	}

	// ④ OptimizeWishlist：消费三档合并候选池 → 成品志愿表
	owReply, err := s.OptimizeWishlist(ctx, &algov1.OptimizeWishlistRequest{
		Candidates: all,
		Profile: &algov1.UserProfile{
			RiskAppetite: "balanced", PriorityDims: []string{"tier"}, PreferredTiers: []string{"985", "211"},
		},
	})
	if err != nil {
		t.Fatalf("OptimizeWishlist: %v", err)
	}
	wl := owReply.GetWishlist()
	rep := wl.GetReport()
	t.Logf("④ OptimizeWishlist：len=%d 冲=%d 稳=%d 稳拿保底=%d SafeGuardOK=%v 滑档率=%.3e 满意度=%.4f",
		len(wl.GetCandidates()), rep.GetRushCount(), rep.GetStableCount(), rep.GetSafeCount(),
		rep.GetSafeGuardOk(), rep.GetSlipRate(), rep.GetExpectedSatisfaction())
	if len(wl.GetCandidates()) == 0 {
		t.Fatal("志愿表为空")
	}
	for i, c := range wl.GetCandidates() {
		if c.GetFinalRank() != int32(i+1) {
			t.Errorf("Candidates[%d].FinalRank = %d, want %d", i, c.GetFinalRank(), i+1)
		}
		if c.GetUtility().GetScore() <= 0 {
			t.Errorf("Candidates[%d].utility.score = %v, want >0（效用分应注入）", i, c.GetUtility().GetScore())
		}
	}
}

func TestIntegration_EvaluateEligibility_SubjectRule(t *testing.T) {
	dsn := os.Getenv("ALGO_TEST_DSN")
	if dsn == "" {
		t.Skip("ALGO_TEST_DSN 未设置，跳过集成测试")
	}
	pool, err := pgxpool.New(context.Background(), dsn)
	if err != nil {
		t.Fatalf("pgxpool.New: %v", err)
	}
	t.Cleanup(pool.Close)

	releaseID := fmt.Sprintf("rel_m4b_test_%d", time.Now().UnixNano())
	if _, err := pool.Exec(context.Background(),
		`INSERT INTO release (release_id, status, manifest, manifest_sha256, published_at)
		 VALUES ($1, 'PUBLISHED', '{}'::jsonb, 'sha256:m4b', NOW())`, releaseID); err != nil {
		t.Fatalf("insert release: %v", err)
	}
	t.Cleanup(func() {
		_, _ = pool.Exec(context.Background(), `DELETE FROM release WHERE release_id = $1`, releaseID)
	})

	if _, err := pool.Exec(context.Background(),
		`INSERT INTO rule_version (
			rule_id, version, rule_key, rule_type, scope, required_profile_fields,
			expression, outcome_when_true, reason_code, review_status,
			valid_from, release_id, source_evidence
		) VALUES (
			'rule-subject-test', 1, 'subject-requirement', 'SUBJECT_REQUIREMENT',
			'{}'::jsonb, '["subjects","category"]'::jsonb,
			'{"op":"NOT","child":{"op":"SUBJECT_MATCH"}}'::jsonb, 'FAIL',
			'SUBJECT_NOT_MATCHED', 'PUBLISHED', NOW(), $1,
			jsonb_build_array(jsonb_build_object(
				'evidence_id', 'ev-subject',
				'evidence_type', 'DOCUMENT',
				'release_id', $1::text,
				'content_sha256', 'sha256:subject'
			))
		)`, releaseID); err != nil {
		t.Fatalf("insert rule_version: %v", err)
	}
	if _, err := pool.Exec(context.Background(),
		`INSERT INTO rule_coverage (
			release_id, rule_type, scope, status, covered_data_sources, notes
		) VALUES (
			$1, 'SUBJECT_REQUIREMENT', '{}'::jsonb, 'FULL',
			'["major_admission"]'::jsonb, 'test fixture'
		)`, releaseID); err != nil {
		t.Fatalf("insert rule_coverage: %v", err)
	}

	t.Setenv("ALGO_DATA_DSN", dsn)
	bc := &conf.Bootstrap{Data: &conf.Data{Database: &conf.Data_Database{Dsn: dsn}}}
	d, cleanup, err := data.NewData(bc)
	if err != nil {
		t.Fatalf("NewData: %v", err)
	}
	t.Cleanup(cleanup)
	svc := NewAlgo(nil, nil, nil, biz.NewEligibilityUsecase(data.NewEligibilityRepo(d)))
	client := setupBufconnClient(t, svc)

	req := &algov1.EvaluateEligibilityRequest{
		ReleaseId: releaseID, ConfirmedProfileVersion: "profile-v1",
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
		CandidateGroups: []*algov1.EligibilityCandidateGroup{
			{
				UniversityId: 1, UniversityCode: "1001", UniversityName: "测试大学", MajorGroup: "01",
				Majors: []*algov1.EligibilityCandidateMajor{{
					MajorCode: "080901", MajorName: "计算机科学与技术",
					SubjectRequirement: "首选物理，再选化学", InCurrentPlan: true,
				}},
			},
			{
				UniversityId: 2, UniversityCode: "1002", UniversityName: "测试大学二", MajorGroup: "02",
				Majors: []*algov1.EligibilityCandidateMajor{{
					MajorCode: "080902", MajorName: "软件工程",
					SubjectRequirement: "首选物理，再选思想政治", InCurrentPlan: true,
				}},
			},
		},
	}
	reply, err := client.EvaluateEligibility(context.Background(), req)
	if err != nil {
		t.Fatalf("EvaluateEligibility: %v", err)
	}
	if reply.GetStatus() != algov1.EligibilityResultStatus_ELIGIBILITY_RESULT_STATUS_COMPLETE {
		t.Fatalf("status = %v, want COMPLETE", reply.GetStatus())
	}
	if got := reply.GetGroups()[0].GetMajors()[0].GetStatus(); got != algov1.MajorEligibilityStatus_MAJOR_ELIGIBILITY_STATUS_PASS {
		t.Fatalf("matched major = %v, want PASS", got)
	}
	if got := reply.GetGroups()[1].GetMajors()[0].GetStatus(); got != algov1.MajorEligibilityStatus_MAJOR_ELIGIBILITY_STATUS_FAIL {
		t.Fatalf("unmatched major = %v, want FAIL", got)
	}
	if got := reply.GetGroups()[1].GetMajors()[0].GetEvidenceRefs(); len(got) != 1 || got[0].GetEvidenceId() != "ev-subject" {
		t.Fatalf("evidence refs = %+v", got)
	}

	if _, err := pool.Exec(context.Background(), `UPDATE release SET status='REVOKED', revoked_at=NOW() WHERE release_id=$1`, releaseID); err != nil {
		t.Fatalf("revoke release: %v", err)
	}
	blocked, err := client.EvaluateEligibility(context.Background(), req)
	if err != nil {
		t.Fatalf("EvaluateEligibility after revoke: %v", err)
	}
	if blocked.GetStatus() != algov1.EligibilityResultStatus_ELIGIBILITY_RESULT_STATUS_BLOCKED {
		t.Fatalf("revoked status = %v, want BLOCKED", blocked.GetStatus())
	}
}

func TestIntegration_EvaluateEligibility_TypedAST(t *testing.T) {
	dsn := os.Getenv("ALGO_TEST_DSN")
	if dsn == "" {
		t.Skip("ALGO_TEST_DSN 未设置，跳过集成测试")
	}
	pool, err := pgxpool.New(context.Background(), dsn)
	if err != nil {
		t.Fatalf("pgxpool.New: %v", err)
	}
	t.Cleanup(pool.Close)

	suffix := time.Now().UnixNano()
	releaseID := fmt.Sprintf("rel_m4b_ast_test_%d", suffix)
	ruleID := fmt.Sprintf("rule-ast-test-%d", suffix)
	if _, err := pool.Exec(context.Background(),
		`INSERT INTO release (release_id, status, manifest, manifest_sha256, published_at)
		 VALUES ($1, 'PUBLISHED', '{}'::jsonb, 'sha256:m4b-ast', NOW())`, releaseID); err != nil {
		t.Fatalf("insert release: %v", err)
	}
	t.Cleanup(func() {
		_, _ = pool.Exec(context.Background(), `DELETE FROM release WHERE release_id = $1`, releaseID)
	})

	if _, err := pool.Exec(context.Background(),
		`INSERT INTO rule_version (
			rule_id, version, rule_key, rule_type, scope, required_profile_fields,
			expression, expression_version, outcome_when_true, reason_code,
			review_status, valid_from, release_id, source_evidence
		) VALUES (
			$2, 1, 'score-and-category', 'OTHER_HARD',
			'{}'::jsonb, '["category","score"]'::jsonb,
			'{"op":"AND","left":{"op":"GTE","field":"score","value":600},"right":{"op":"EQ","field":"category","value":"物理类"}}'::jsonb,
			1, 'FAIL', 'SCORE_TOO_LOW', 'PUBLISHED', NOW(), $1,
			jsonb_build_array(jsonb_build_object(
				'evidence_id', 'ev-ast',
				'evidence_type', 'DOCUMENT',
				'release_id', $1::text,
				'content_sha256', 'sha256:ast'
			))
		)`, releaseID, ruleID); err != nil {
		t.Fatalf("insert typed expression rule: %v", err)
	}
	if _, err := pool.Exec(context.Background(),
		`INSERT INTO rule_coverage (
			release_id, rule_type, scope, status, covered_data_sources, notes
		) VALUES
			($1, 'SUBJECT_REQUIREMENT', '{}'::jsonb, 'FULL', '["major_admission"]'::jsonb, 'test fixture'),
			($1, 'OTHER_HARD', '{}'::jsonb, 'FULL', '["major_admission"]'::jsonb, 'test fixture')`,
		releaseID); err != nil {
		t.Fatalf("insert coverage: %v", err)
	}

	t.Setenv("ALGO_DATA_DSN", dsn)
	bc := &conf.Bootstrap{Data: &conf.Data{Database: &conf.Data_Database{Dsn: dsn}}}
	d, cleanup, err := data.NewData(bc)
	if err != nil {
		t.Fatalf("NewData: %v", err)
	}
	t.Cleanup(cleanup)
	svc := NewAlgo(nil, nil, nil, biz.NewEligibilityUsecase(data.NewEligibilityRepo(d)))
	client := setupBufconnClient(t, svc)

	request := &algov1.EvaluateEligibilityRequest{
		ReleaseId: releaseID, ConfirmedProfileVersion: "profile-ast-v1",
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
			{
				FieldKey: "score", State: algov1.EligibilityProfileFieldState_ELIGIBILITY_PROFILE_FIELD_STATE_PROVIDED,
				IntValue: 630,
			},
		},
		CandidateGroups: []*algov1.EligibilityCandidateGroup{{
			UniversityId: 1, UniversityCode: "1001", UniversityName: "测试大学", MajorGroup: "01",
			Majors: []*algov1.EligibilityCandidateMajor{{
				MajorCode: "080901", MajorName: "计算机科学与技术",
				SubjectRequirement: "首选物理，再选化学", InCurrentPlan: true,
			}},
		}},
	}
	first, err := client.EvaluateEligibility(context.Background(), request)
	if err != nil {
		t.Fatalf("EvaluateEligibility typed AST: %v", err)
	}
	second, err := client.EvaluateEligibility(context.Background(), request)
	if err != nil {
		t.Fatalf("EvaluateEligibility typed AST repeat: %v", err)
	}
	if !proto.Equal(first, second) {
		t.Fatal("同一 typed AST 输入未得到逐项一致结果")
	}
	if got := first.GetGroups()[0].GetMajors()[0].GetStatus(); got != algov1.MajorEligibilityStatus_MAJOR_ELIGIBILITY_STATUS_FAIL {
		t.Fatalf("typed AST major status = %v, want FAIL", got)
	}

	invalidReleaseID := releaseID + "_invalid"
	invalidRuleID := fmt.Sprintf("rule-ast-invalid-%d", suffix)
	if _, err := pool.Exec(context.Background(),
		`INSERT INTO release (release_id, status, manifest, manifest_sha256, published_at)
		 VALUES ($1, 'PUBLISHED', '{}'::jsonb, 'sha256:m4b-ast-invalid', NOW())`, invalidReleaseID); err != nil {
		t.Fatalf("insert invalid release: %v", err)
	}
	t.Cleanup(func() {
		_, _ = pool.Exec(context.Background(), `DELETE FROM release WHERE release_id = $1`, invalidReleaseID)
	})
	if _, err := pool.Exec(context.Background(),
		`INSERT INTO rule_version (
			rule_id, version, rule_key, rule_type, scope, required_profile_fields,
			expression, expression_version, outcome_when_true, reason_code,
			review_status, valid_from, release_id, source_evidence
		) VALUES (
			$2, 1, 'dangerous-expression', 'OTHER_HARD',
			'{}'::jsonb, '["category"]'::jsonb,
			'{"op":"EQ","field":"category","value":"物理类","sql":"SELECT secret"}'::jsonb,
			1, 'FAIL', 'INVALID', 'PUBLISHED', NOW(), $1,
			jsonb_build_array(jsonb_build_object(
				'evidence_id', 'ev-ast-invalid',
				'evidence_type', 'DOCUMENT',
				'release_id', $1::text,
				'content_sha256', 'sha256:ast-invalid'
			))
		)`, invalidReleaseID, invalidRuleID); err != nil {
		t.Fatalf("insert invalid expression rule: %v", err)
	}
	if _, err := pool.Exec(context.Background(),
		`INSERT INTO rule_coverage (
			release_id, rule_type, scope, status, covered_data_sources, notes
		) VALUES
			($1, 'SUBJECT_REQUIREMENT', '{}'::jsonb, 'FULL', '["major_admission"]'::jsonb, 'test fixture'),
			($1, 'OTHER_HARD', '{}'::jsonb, 'FULL', '["major_admission"]'::jsonb, 'test fixture')`,
		invalidReleaseID); err != nil {
		t.Fatalf("insert invalid coverage: %v", err)
	}

	request.ReleaseId = invalidReleaseID
	blocked, err := client.EvaluateEligibility(context.Background(), request)
	if err != nil {
		t.Fatalf("EvaluateEligibility invalid AST returned transport error: %v", err)
	}
	if blocked.GetStatus() != algov1.EligibilityResultStatus_ELIGIBILITY_RESULT_STATUS_BLOCKED {
		t.Fatalf("invalid AST status = %v, want BLOCKED", blocked.GetStatus())
	}
	reasons := blocked.GetGroups()[0].GetMajors()[0].GetUnknownReasons()
	if len(reasons) != 1 || reasons[0] != "RULE_EXPRESSION_INVALID" {
		t.Fatalf("invalid AST reasons = %v", reasons)
	}
}

// setupBufconnClient 使用与生产相同的 Kratos gRPC 实现及生成的注册函数，
// 只将网络监听替换为内存连接；不占 TCP 端口，也不验证 main/wire/YAML 启动链路。
func setupBufconnClient(t *testing.T, svc *Service) algov1.AlgoClient {
	t.Helper()
	lis := bufconn.Listen(1024 * 1024)
	server := kgrpc.NewServer(kgrpc.Timeout(30 * time.Second))
	algov1.RegisterAlgoServer(server, svc)
	done := make(chan error, 1)
	go func() { done <- server.Serve(lis) }()
	t.Cleanup(func() {
		// 先终止请求，再清理 Kratos 管理服务，避免失败用例让 GracefulStop 无限等待。
		server.Server.Stop()
		if err := server.Stop(context.Background()); err != nil {
			t.Errorf("停止 bufconn 服务: %v", err)
		}
		_ = lis.Close()
		select {
		case err := <-done:
			if err != nil && !errors.Is(err, grpc.ErrServerStopped) {
				t.Errorf("bufconn Serve: %v", err)
			}
		case <-time.After(5 * time.Second):
			t.Error("bufconn Serve 未按时退出")
		}
	})

	conn, err := grpc.NewClient("passthrough:///bufnet",
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
			return lis.DialContext(ctx)
		}),
	)
	if err != nil {
		t.Fatalf("创建 bufconn 客户端: %v", err)
	}
	// Cleanup 后进先出：客户端 → 服务端 → setupService 注册的数据库池。
	t.Cleanup(func() {
		if err := conn.Close(); err != nil {
			t.Errorf("关闭 bufconn 客户端: %v", err)
		}
	})
	return algov1.NewAlgoClient(conn)
}

// assertBufconnReply 比较完整 protobuf 响应（含嵌套字段、列表顺序及 map 值），
// 三次远程调用都必须与直调结果语义相等；不把 map 编码顺序称为字节级确定性。
func assertBufconnReply[Req, Reply proto.Message](t *testing.T, name string, req Req,
	direct func(context.Context, Req) (Reply, error),
	remote func(context.Context, Req, ...grpc.CallOption) (Reply, error),
) Reply {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	want, err := direct(ctx, proto.Clone(req).(Req))
	if err != nil {
		t.Fatalf("%s 直调: %v", name, err)
	}
	var got Reply
	matched := true
	for i := 0; i < 3; i++ {
		got, err = remote(ctx, req)
		if err != nil {
			t.Fatalf("%s bufconn 第 %d 次: %v", name, i+1, err)
		}
		if !proto.Equal(got, want) {
			if matched {
				if estimate, ok := any(got).(*algov1.EstimateProbReply); ok {
					logEstimateDifference(t, estimate, any(want).(*algov1.EstimateProbReply))
				}
			}
			matched = false
			// 保留失败，同时让后续 RPC 继续验证收到的候选，避免一处差异遮住其余传输覆盖。
			t.Errorf("%s bufconn 第 %d 次完整响应与直调不一致", name, i+1)
		}
	}
	if matched {
		t.Logf("%s：3 次完整 protobuf 响应与直调一致（%d 字节）", name, proto.Size(got))
	}
	return got
}

// logEstimateDifference 将全量候选响应的差异缩小到首个组，避免失败时打印整个候选池。
func logEstimateDifference(t *testing.T, got, want *algov1.EstimateProbReply) {
	t.Helper()
	for _, bucket := range []struct {
		name      string
		got, want []*algov1.CandidateWithProb
	}{
		{"冲", got.GetRushCandidates(), want.GetRushCandidates()},
		{"稳", got.GetStableCandidates(), want.GetStableCandidates()},
		{"保", got.GetSafeCandidates(), want.GetSafeCandidates()},
	} {
		if len(bucket.got) != len(bucket.want) {
			t.Logf("%s档数量变化: got=%d want=%d", bucket.name, len(bucket.got), len(bucket.want))
			return
		}
		for i, c := range bucket.got {
			if !proto.Equal(c, bucket.want[i]) {
				g, w := c.GetCandidate(), bucket.want[i].GetCandidate()
				t.Logf("%s档[%d] 首个差异组：%s/%s（%s）", bucket.name, i,
					g.GetUniversityCode(), g.GetMajorGroup(), g.GetUniversityName())
				if len(g.GetMajors()) == len(w.GetMajors()) {
					for j, m := range g.GetMajors() {
						if !proto.Equal(m, w.GetMajors()[j]) {
							t.Logf("majors[%d] got=%v want=%v", j, m, w.GetMajors()[j])
							return
						}
					}
				}
				t.Logf("组内专业数 got=%d want=%d；prob got=%v want=%v", len(g.GetMajors()),
					len(w.GetMajors()), c.GetProb(), bucket.want[i].GetProb())
				return
			}
		}
	}
	t.Logf("候选内容一致，需检查 totals 或 basis: got=%v want=%v", got.GetBasis(), want.GetBasis())
}

// 绕过传输层重复直调，区分服务自身的不确定性和 protobuf 序列化问题。
func TestIntegration_Service_EstimateProb_Reproducible(t *testing.T) {
	svc := setupService(t)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	req := &algov1.EstimateProbRequest{
		ReleaseId: integrationRelease, Province: "湖南", Year: "2025", Category: "物理类", Batch: "本科批", Score: 600,
		Subjects: []string{"物理", "化学", "生物"},
	}
	first, err := svc.EstimateProb(ctx, req)
	if err != nil {
		t.Fatalf("EstimateProb 首次直调: %v", err)
	}
	for i := 0; i < 3; i++ {
		next, err := svc.EstimateProb(ctx, req)
		if err != nil {
			t.Fatalf("EstimateProb 重复直调: %v", err)
		}
		if !proto.Equal(next, first) {
			logEstimateDifference(t, next, first)
			t.Fatalf("EstimateProb 第 %d 次重复直调不一致（未经 gRPC）", i+1)
		}
	}
}

func TestIntegration_Bufconn_FourRPCs(t *testing.T) {
	svc := setupService(t)
	client := setupBufconnClient(t, svc)
	subjects := []string{"物理", "化学", "生物"}
	profile := &algov1.UserProfile{
		RiskAppetite: "balanced", PriorityDims: []string{"tier"}, PreferredTiers: []string{"985", "211"},
	}
	rank := assertBufconnReply(t, "RankConvert", &algov1.RankConvertRequest{
		ReleaseId: integrationRelease, Province: "湖南", Year: "2025", Category: "物理类", Score: 600,
	}, svc.RankConvert, client.RankConvert)
	if rank.GetRank() <= 0 || rank.GetPercentile() <= 0 || rank.GetPercentile() > 1 {
		t.Fatalf("位次/百分位非法: %v", rank)
	}
	estimate := assertBufconnReply(t, "EstimateProb", &algov1.EstimateProbRequest{
		ReleaseId: integrationRelease, Province: "湖南", Year: "2025", Category: "物理类", Batch: "本科批", Score: 600,
		Subjects: subjects, Profile: profile,
	}, svc.EstimateProb, client.EstimateProb)
	wantBasis := &algov1.AuditBasis{
		UserRank: rank.GetRank(), UserPercentile: rank.GetPercentile(), SampleYears: []int32{2022, 2023, 2024},
		Method: "weighted_hit", Province: "湖南", Category: "物理类",
	}
	if !proto.Equal(estimate.GetBasis(), wantBasis) {
		t.Fatalf("审计依据不一致: got %v, want %v", estimate.GetBasis(), wantBasis)
	}
	if int(estimate.GetRushTotal()) != len(estimate.GetRushCandidates()) ||
		int(estimate.GetStableTotal()) != len(estimate.GetStableCandidates()) ||
		int(estimate.GetSafeTotal()) != len(estimate.GetSafeCandidates()) {
		t.Fatal("三档总数与全量候选数量不一致")
	}
	all := append([]*algov1.CandidateWithProb{}, estimate.GetRushCandidates()...)
	all = append(all, estimate.GetStableCandidates()...)
	all = append(all, estimate.GetSafeCandidates()...)
	if len(all) == 0 {
		t.Fatal("三档候选为空")
	}
	checked := assertBufconnReply(t, "CheckConstraints", &algov1.CheckConstraintsRequest{
		Candidates: all, Subjects: subjects, Category: "物理类",
	}, svc.CheckConstraints, client.CheckConstraints)
	if len(checked.GetKept()) != len(all) || len(checked.GetRejected()) != 0 {
		t.Fatal("已经过选科过滤的候选再次校验后发生变化")
	}
	for i, c := range checked.GetKept() {
		if !proto.Equal(c, all[i]) {
			t.Fatalf("kept[%d] 候选、概率或顺序发生变化", i)
		}
	}
	optimized := assertBufconnReply(t, "OptimizeWishlist", &algov1.OptimizeWishlistRequest{
		Candidates: checked.GetKept(), Profile: profile,
	}, svc.OptimizeWishlist, client.OptimizeWishlist)
	wishlist := optimized.GetWishlist()
	if len(wishlist.GetCandidates()) != 45 || !wishlist.GetReport().GetSafeGuardOk() {
		t.Fatalf("期望生成 45 个志愿且保底达标，实际 len=%d report=%v", len(wishlist.GetCandidates()), wishlist.GetReport())
	}
	for i, c := range wishlist.GetCandidates() {
		if c.GetFinalRank() != int32(i+1) || c.GetCwp().GetProb().GetProbability() <= 0 {
			t.Fatalf("志愿[%d] 排序或概率不完整: %v", i, c)
		}
		if len(c.GetUtility().GetBreakdown()) != 4 || len(c.GetUtility().GetWeights()) != 4 {
			t.Fatalf("志愿[%d] 的四维效用 map 不完整", i)
		}
	}
}

// 按 §4.4 的目标错误契约断言，同时检查跨 gRPC 的 ErrorInfo.reason。
// 防止两类前置条件错误退回 HTTP 400 的默认 InvalidArgument 映射。
func TestIntegration_Bufconn_ErrorMapping(t *testing.T) {
	client := setupBufconnClient(t, setupService(t))
	cases := []struct {
		name, year string
		score      int32
		code       codes.Code
		reason     string
	}{
		{"空年份", "", 600, codes.InvalidArgument, "INVALID_YEAR"},
		{"非法年份", "abc", 600, codes.InvalidArgument, "INVALID_YEAR"},
		{"低于本科线", "2025", 404, codes.FailedPrecondition, "SCORE_BELOW_CONTROL_LINE"},
		{"超过最高分", "2025", 751, codes.InvalidArgument, "SCORE_ABOVE_MAX"},
		{"缺少报考年数据", "9999", 600, codes.Internal, "MISSING_SCORE_RANK_MAP"},
	}
	for _, tc := range cases {
		t.Run("RankConvert/"+tc.name, func(t *testing.T) {
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			_, err := client.RankConvert(ctx, &algov1.RankConvertRequest{
				ReleaseId: integrationRelease, Province: "湖南", Year: tc.year, Category: "物理类", Score: tc.score,
			})
			assertBufconnError(t, err, tc.code, tc.reason)
		})
		t.Run("EstimateProb/"+tc.name, func(t *testing.T) {
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			_, err := client.EstimateProb(ctx, &algov1.EstimateProbRequest{
				ReleaseId: integrationRelease, Province: "湖南", Year: tc.year, Category: "物理类", Batch: "本科批", Score: tc.score,
				Subjects: []string{"物理", "化学", "生物"},
			})
			assertBufconnError(t, err, tc.code, tc.reason)
		})
	}
	for _, tc := range []struct {
		name string
		prob *algov1.ProbEstimate
	}{
		{"缺失概率消息", nil},
		{"零概率", &algov1.ProbEstimate{Probability: 0}},
		{"负概率", &algov1.ProbEstimate{Probability: -0.1}},
	} {
		t.Run("OptimizeWishlist/"+tc.name, func(t *testing.T) {
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			_, err := client.OptimizeWishlist(ctx, &algov1.OptimizeWishlistRequest{
				Candidates: []*algov1.CandidateWithProb{{
					Candidate: &algov1.Candidate{UniversityCode: "A", MajorGroup: "1"}, Prob: tc.prob,
				}},
			})
			assertBufconnError(t, err, codes.FailedPrecondition, "CANDIDATE_WITHOUT_PROB")
		})
	}
}

func assertBufconnError(t *testing.T, err error, wantCode codes.Code, wantReason string) {
	t.Helper()
	if err == nil {
		t.Fatalf("期望错误 %s/%s，实际成功", wantCode, wantReason)
	}
	st, ok := status.FromError(err)
	if !ok {
		t.Fatalf("错误不是 gRPC status: %v", err)
	}
	if st.Code() != wantCode {
		t.Errorf("gRPC status = %s, want %s（reason=%s）", st.Code(), wantCode, wantReason)
	}
	found := false
	for _, detail := range st.Details() {
		if info, ok := detail.(*errdetails.ErrorInfo); ok && info.GetReason() == wantReason {
			found = true
		}
	}
	if !found {
		t.Fatalf("gRPC ErrorInfo 未携带 reason=%s: %v", wantReason, st.Details())
	}
	if got := kerrors.Reason(err); got != wantReason {
		t.Fatalf("Kratos 客户端解析 reason=%q, want %q", got, wantReason)
	}
	wantHTTP := 400
	switch wantCode {
	case codes.Internal:
		wantHTTP = 500
	case codes.Canceled:
		wantHTTP = 499
	case codes.DeadlineExceeded:
		wantHTTP = 504
	}
	if got := kerrors.Code(err); got != wantHTTP {
		t.Errorf("Kratos 客户端解析 HTTP code=%d, want %d", got, wantHTTP)
	}
	t.Logf("gRPC status=%s, reason=%s", st.Code(), wantReason)
}

func TestIntegration_Bufconn_Boundaries(t *testing.T) {
	svc := setupService(t)
	client := setupBufconnClient(t, svc)
	t.Run("选科拒绝保留嵌套候选及概率", func(t *testing.T) {
		kept := pbCWP("A", "1", "保", 0.95)
		rejected := pbCWP("B", "2", "稳", 0.6)
		rejected.Candidate.SubjectReq = []string{"首选物理，再选思想政治"}
		rejected.Candidate.HistoryData = []*algov1.AdmissionRecord{{
			Year: 2024, GroupMinRank: 12345, TotalCount: 300000, MinPercentile: 0.04115, EnrollCount: 20,
		}}
		reply := assertBufconnReply(t, "CheckConstraints/剔除", &algov1.CheckConstraintsRequest{
			Candidates: []*algov1.CandidateWithProb{kept, rejected},
			Subjects:   []string{"物理", "化学", "生物"}, Category: "物理类",
		}, svc.CheckConstraints, client.CheckConstraints)
		if len(reply.GetKept()) != 1 || len(reply.GetRejected()) != 1 {
			t.Fatalf("选科结果数量错误: %v", reply)
		}
		r := reply.GetRejected()[0]
		if !proto.Equal(reply.GetKept()[0], kept) || !proto.Equal(r.GetCandidate(), rejected) ||
			r.GetRule() != biz.RuleSubjectReq || r.GetReason() == "" {
			t.Fatalf("候选回填或拒绝原因不完整: %v", reply)
		}
	})
	t.Run("保底不足仍成功", func(t *testing.T) {
		reply := assertBufconnReply(t, "OptimizeWishlist/告警", &algov1.OptimizeWishlistRequest{
			Candidates: []*algov1.CandidateWithProb{pbCWP("A", "1", "冲", 0.3)},
			Profile:    &algov1.UserProfile{RiskAppetite: "aggressive"},
		}, svc.OptimizeWishlist, client.OptimizeWishlist)
		wl := reply.GetWishlist()
		if len(wl.GetCandidates()) != 1 || wl.GetReport() == nil || wl.GetReport().GetSafeGuardOk() {
			t.Fatalf("保底不足应正常返回志愿及 false 告警: %v", reply)
		}
	})
	t.Run("空候选与缺省画像", func(t *testing.T) {
		checked := assertBufconnReply(t, "CheckConstraints/空候选", &algov1.CheckConstraintsRequest{},
			svc.CheckConstraints, client.CheckConstraints)
		if len(checked.GetKept()) != 0 || len(checked.GetRejected()) != 0 {
			t.Fatalf("空输入不应生成候选: %v", checked)
		}
		optimized := assertBufconnReply(t, "OptimizeWishlist/空候选", &algov1.OptimizeWishlistRequest{},
			svc.OptimizeWishlist, client.OptimizeWishlist)
		wl := optimized.GetWishlist()
		if len(wl.GetCandidates()) != 0 || wl.GetReport() == nil || wl.GetReport().GetSafeGuardOk() ||
			wl.GetReport().GetSlipRate() != 1 || wl.GetReport().GetExpectedSatisfaction() != 0 {
			t.Fatalf("空志愿应返回保底不足、滑档率 1、满意度 0: %v", optimized)
		}
	})
}
