package biz

import (
	"context"
	"errors"
	"testing"

	"github.com/choucisan/ai-gaokao-counselor/conf"
	errs "github.com/choucisan/ai-gaokao-counselor/internal/pkg/errors"
)

// fakeAdmissionRepo 实现 AdmissionRepo，供 EstimateProb 编排测试（无需 DB）。
type fakeAdmissionRepo struct {
	candidates      []Candidate
	historyMap      map[GroupKey][]AdmissionRecord
	majorHistoryMap map[MajorKey][]MajorAdmissionRecord
	planMap         map[GroupKey]PlanChange
	loadErr         error
	historyErr      error
	planErr         error
	// 记录入参，验证编排传参（batch 默认本科批、year 口径）
	gotBatch string
	gotYear  int
}

func (f *fakeAdmissionRepo) LoadCandidates(ctx context.Context, releaseID, province, category, batch string, year int) ([]Candidate, error) {
	f.gotBatch = batch
	f.gotYear = year
	if f.loadErr != nil {
		return nil, f.loadErr
	}
	return f.candidates, nil
}

func (f *fakeAdmissionRepo) BatchGroupHistory(ctx context.Context, releaseID, province, category, batch string, year, windowYears int) (map[GroupKey][]AdmissionRecord, error) {
	if f.historyErr != nil {
		return nil, f.historyErr
	}
	return f.historyMap, nil
}

func (f *fakeAdmissionRepo) BatchMajorHistory(ctx context.Context, releaseID, province, category, batch string, year, windowYears int) (map[MajorKey][]MajorAdmissionRecord, error) {
	return f.majorHistoryMap, nil
}

func (f *fakeAdmissionRepo) BatchGroupPlan(ctx context.Context, releaseID, province, category string, year, prevYear int) (map[GroupKey]PlanChange, error) {
	if f.planErr != nil {
		return nil, f.planErr
	}
	return f.planMap, nil
}

// rec 快捷构造一条历史记录（GroupMinRank/TotalCount，MinPercentile 由 biz 算）。
func rec(year, groupMinRank, totalCount int) AdmissionRecord {
	return AdmissionRecord{Year: year, GroupMinRank: groupMinRank, TotalCount: totalCount}
}

// TestRecommendUsecase_EstimateProb_HappyPath 验证完整编排：
// rank.Convert(450)→用户百分位≈0.4896 → LoadCandidates → BatchGroupHistory/Plan → RecommendCandidates 分档。
// 三组分别落 冲/稳/保（历史命中数不同）。
func TestRecommendUsecase_EstimateProb_HappyPath(t *testing.T) {
	// 用户 450 分 → rank=153585、percentile=153585/313685≈0.48961（见 fakeTable/rank_test.go）
	rank := NewRankUsecase(&fakeRankRepo{table: fakeTable(), control: fakeControl})
	admission := &fakeAdmissionRepo{
		candidates: []Candidate{
			{UniversityCode: "SAFE", MajorGroup: "(1)"},
			{UniversityCode: "STABLE", MajorGroup: "(2)"},
			{UniversityCode: "RUSH", MajorGroup: "(3)"},
		},
		historyMap: map[GroupKey][]AdmissionRecord{
			// 保：3 年 MinPercentile=200000/313685≈0.6376 均 ≥ 用户 0.4896 → 全命中 → 0.99
			{UniversityCode: "SAFE", MajorGroup: "(1)"}: {rec(2024, 200000, 313685), rec(2023, 200000, 313685), rec(2022, 200000, 313685)},
			// 稳：前两年命中(0.6376)、最远年不命中(100000/313685≈0.3188<0.4896) → (1.0+0.7)/2.19≈0.776
			{UniversityCode: "STABLE", MajorGroup: "(2)"}: {rec(2024, 200000, 313685), rec(2023, 200000, 313685), rec(2022, 100000, 313685)},
			// 冲：仅最远年命中(0.6376)、前两年不命中(0.3188) → 0.49/2.19≈0.2237
			{UniversityCode: "RUSH", MajorGroup: "(3)"}: {rec(2024, 100000, 313685), rec(2023, 100000, 313685), rec(2022, 200000, 313685)},
		},
	}
	uc := NewRecommendUsecase(admission, rank, &conf.Bootstrap{})

	res, err := uc.EstimateProb(context.Background(), UserQuery{
		ReleaseID: "rel-test",
		Province:  "湖南", Category: "物理类", Year: 2025, Score: 450, Subjects: []string{"物理", "化学", "生物"}, // Batch 留空 → 默认本科批
	}, UserProfile{})
	if err != nil {
		t.Fatalf("unexpected err: %v", err)
	}

	// 编排传参校验：batch 默认本科批、year=报考年 2025
	if admission.gotBatch != "本科批" {
		t.Errorf("LoadCandidates batch = %q, want 本科批（MVP 默认）", admission.gotBatch)
	}
	if admission.gotYear != 2025 {
		t.Errorf("LoadCandidates year = %d, want 2025（报考年）", admission.gotYear)
	}

	if res.TotalCandidates != 3 {
		t.Errorf("TotalCandidates = %d, want 3", res.TotalCandidates)
	}
	if res.RushTotal != 1 || res.StableTotal != 1 || res.SafeTotal != 1 {
		t.Errorf("分档错误：rush=%d stable=%d safe=%d，want 各 1", res.RushTotal, res.StableTotal, res.SafeTotal)
	}
	if len(res.SafeCandidates) == 1 && res.SafeCandidates[0].UniversityCode != "SAFE" {
		t.Errorf("保档应是 SAFE，got %s", res.SafeCandidates[0].UniversityCode)
	}
	if len(res.StableCandidates) == 1 && res.StableCandidates[0].UniversityCode != "STABLE" {
		t.Errorf("稳档应是 STABLE，got %s", res.StableCandidates[0].UniversityCode)
	}
	if len(res.RushCandidates) == 1 && res.RushCandidates[0].UniversityCode != "RUSH" {
		t.Errorf("冲档应是 RUSH，got %s", res.RushCandidates[0].UniversityCode)
	}
}

// TestRecommendUsecase_EstimateProb_BelowControlLine 分数低于本科线 → rank.Convert 报 ErrBelowControlLine，EstimateProb 透传。
func TestRecommendUsecase_EstimateProb_BelowControlLine(t *testing.T) {
	rank := NewRankUsecase(&fakeRankRepo{table: fakeTable(), control: fakeControl})
	uc := NewRecommendUsecase(&fakeAdmissionRepo{}, rank, &conf.Bootstrap{})

	_, err := uc.EstimateProb(context.Background(), UserQuery{
		ReleaseID: "rel-test",
		Province:  "湖南", Category: "物理类", Year: 2025, Score: 404, Subjects: []string{"物理", "化学", "生物"}, // < 本科线 405
	}, UserProfile{})
	if !errors.Is(err, errs.ErrBelowControlLine) {
		t.Errorf("err = %v, want ErrBelowControlLine", err)
	}
}

// TestRecommendUsecase_EstimateProb_LoadError LoadCandidates 报错 → 透传。
func TestRecommendUsecase_EstimateProb_LoadError(t *testing.T) {
	rank := NewRankUsecase(&fakeRankRepo{table: fakeTable(), control: fakeControl})
	admission := &fakeAdmissionRepo{loadErr: errors.New("db down")}
	uc := NewRecommendUsecase(admission, rank, &conf.Bootstrap{})

	_, err := uc.EstimateProb(context.Background(), UserQuery{
		ReleaseID: "rel-test",
		Province:  "湖南", Category: "物理类", Year: 2025, Score: 450, Subjects: []string{"物理", "化学", "生物"},
	}, UserProfile{})
	if err == nil || err.Error() != "db down" {
		t.Errorf("err = %v, want 'db down' 透传", err)
	}
}

// TestRecommendUsecase_EstimateProb_NoHistory 有候选但无历史 → 全跳过 → 空分档、无 error。
func TestRecommendUsecase_EstimateProb_NoHistory(t *testing.T) {
	rank := NewRankUsecase(&fakeRankRepo{table: fakeTable(), control: fakeControl})
	admission := &fakeAdmissionRepo{
		candidates: []Candidate{{UniversityCode: "U1", MajorGroup: "(1)"}},
		historyMap: map[GroupKey][]AdmissionRecord{}, // 空历史
	}
	uc := NewRecommendUsecase(admission, rank, &conf.Bootstrap{})

	res, err := uc.EstimateProb(context.Background(), UserQuery{
		ReleaseID: "rel-test",
		Province:  "湖南", Category: "物理类", Year: 2025, Score: 450, Subjects: []string{"物理", "化学", "生物"},
	}, UserProfile{})
	if err != nil {
		t.Fatalf("unexpected err: %v", err)
	}
	if res.TotalCandidates != 0 || res.RushTotal != 0 || res.StableTotal != 0 || res.SafeTotal != 0 {
		t.Errorf("无历史应全空，got %+v", res)
	}
}

// TestRecommendUsecase_EstimateProb_SubjectFilter 验证 step c 选科过滤生效：
// 用户选 [物理,化学,地理]，NOMATCH 组要求思想政治 → 被剔除，只剩 MATCH 落保档。
func TestRecommendUsecase_EstimateProb_SubjectFilter(t *testing.T) {
	rank := NewRankUsecase(&fakeRankRepo{table: fakeTable(), control: fakeControl})
	hist := []AdmissionRecord{rec(2024, 200000, 313685), rec(2023, 200000, 313685), rec(2022, 200000, 313685)}
	admission := &fakeAdmissionRepo{
		candidates: []Candidate{
			{UniversityCode: "MATCH", MajorGroup: "(1)", SubjectReq: []string{"首选物理，再选化学"}},
			{UniversityCode: "NOMATCH", MajorGroup: "(1)", SubjectReq: []string{"首选物理，再选思想政治"}},
		},
		historyMap: map[GroupKey][]AdmissionRecord{
			{UniversityCode: "MATCH", MajorGroup: "(1)"}:   hist,
			{UniversityCode: "NOMATCH", MajorGroup: "(1)"}: hist,
		},
	}
	uc := NewRecommendUsecase(admission, rank, &conf.Bootstrap{})

	res, err := uc.EstimateProb(context.Background(), UserQuery{
		ReleaseID: "rel-test",
		Province:  "湖南", Category: "物理类", Year: 2025, Score: 450,
		Subjects: []string{"物理", "化学", "地理"}, // 不含思想政治 → NOMATCH 被过滤
	}, UserProfile{})
	if err != nil {
		t.Fatalf("unexpected err: %v", err)
	}
	if res.TotalCandidates != 1 {
		t.Errorf("TotalCandidates = %d, want 1（NOMATCH 应被选科过滤）", res.TotalCandidates)
	}
	if res.SafeTotal != 1 || len(res.SafeCandidates) != 1 || res.SafeCandidates[0].UniversityCode != "MATCH" {
		t.Errorf("应只剩 MATCH 落保档, got safe=%+v", res.SafeCandidates)
	}
}

// TestRecommendUsecase_CheckConstraints 验证独立 RPC 编排：委托纯函数 + 按 GroupKey 回填 kept/rejected 的 prob。
func TestRecommendUsecase_CheckConstraints(t *testing.T) {
	rank := NewRankUsecase(&fakeRankRepo{table: fakeTable(), control: fakeControl})
	uc := NewRecommendUsecase(&fakeAdmissionRepo{}, rank, &conf.Bootstrap{})

	pool := []CandidateWithProb{
		{Candidate: Candidate{UniversityCode: "A", MajorGroup: "(1)", SubjectReq: []string{"首选物理，再选化学"}},
			ProbEstimate: ProbEstimate{Probability: 0.9, Category: "保", Method: methodWeightedHit}},
		{Candidate: Candidate{UniversityCode: "B", MajorGroup: "(1)", SubjectReq: []string{"首选物理，再选思想政治"}},
			ProbEstimate: ProbEstimate{Probability: 0.3, Category: "冲", Method: methodWeightedHit}},
	}
	kept, rejected, err := uc.CheckConstraints(context.Background(), []string{"物理", "化学", "地理"}, "物理类", pool)
	if err != nil {
		t.Fatalf("unexpected err: %v", err)
	}
	if len(kept) != 1 || kept[0].UniversityCode != "A" {
		t.Fatalf("kept 应=[A], got %+v", kept)
	}
	// 回填 prob：kept[A] 应带回 Probability=0.9/Category=保
	if kept[0].Probability != 0.9 || kept[0].Category != "保" {
		t.Errorf("kept prob 未回填: %+v", kept[0].ProbEstimate)
	}
	if len(rejected) != 1 || rejected[0].UniversityCode != "B" {
		t.Fatalf("rejected 应=[B], got %+v", rejected)
	}
	// rejected 也回填 prob（契约形状完整）+ Rule=subject_req
	if rejected[0].Probability != 0.3 || rejected[0].Category != "冲" {
		t.Errorf("rejected prob 未回填: %+v", rejected[0].ProbEstimate)
	}
	if rejected[0].Rule != RuleSubjectReq {
		t.Errorf("rejected.Rule = %q, want %q", rejected[0].Rule, RuleSubjectReq)
	}
}
