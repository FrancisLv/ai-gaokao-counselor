package biz

import (
	"context"
	"errors"
	"fmt"
	"math"
	"math/rand"

	errs "github.com/choucisan/ai-gaokao-counselor/internal/pkg/errors"
	"reflect"
	"testing"

	"github.com/choucisan/ai-gaokao-counselor/conf"
)

func FuzzSubjectReq(f *testing.F) {
	for _, s := range []string{"", "首选物理，再选化学", "首选历史，再选政治/地理（2选1）", "首选物理，再选化学、 生物(2科必选)", "未知"} {
		f.Add(s)
	}
	f.Fuzz(func(t *testing.T, s string) {
		r, err := parseSubjectReq(s)
		again, err2 := parseSubjectReq(s)
		if (err == nil) != (err2 == nil) || !reflect.DeepEqual(r, again) {
			t.Fatal("解析不确定")
		}
		if err != nil {
			return
		}
		if r.FirstChoice != firstAny && r.FirstChoice != firstPhysics && r.FirstChoice != firstHistory {
			t.Fatal("非法首选")
		}
		seen := map[string]bool{}
		for _, subject := range r.Reselect {
			if seen[subject] || !isReselect(subject) {
				t.Fatal("非法再选")
			}
			seen[subject] = true
		}
		if len(r.Reselect) > 2 {
			t.Fatal("超过两门再选")
		}
	})
}

func TestRegression_WishlistHardLimit(t *testing.T) {
	pool := []CandidateWithScore{cws("R", "1", CategoryRush, .3, .9), cws("S", "1", CategoryStable, .6, .8), cws("B1", "1", CategorySafe, .99, .2), cws("B2", "1", CategorySafe, .95, .1)}
	params := OptimizeParams{MaxWishlistSize: 2, MinSafeCount: 2, SafeGuardThreshold: .9, TargetRatio: RatioConfig{.5, .5, 0}}
	res, err := OptimizeWishlist(pool, params)
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Candidates) != 2 || res.Report.SafeCount != 2 || !res.Report.SafeGuardOK {
		t.Fatalf("上限与保底未同时满足: %+v", res)
	}
	for i, c := range res.Candidates {
		if c.FinalRank != i+1 {
			t.Fatal("排名不连续")
		}
	}
}

func TestRegression_RejectInvalidCandidates(t *testing.T) {
	uc := NewOptimizeUsecase(&conf.Bootstrap{})
	base := cws("A", "1", CategorySafe, .95, .5).CandidateWithProb
	for _, probability := range []float64{math.NaN(), math.Inf(1), math.Inf(-1), 1.5, 1, .001} {
		item := base
		item.Probability = probability
		if _, err := uc.OptimizeWishlist(context.Background(), []CandidateWithProb{item}, UserProfile{}); err == nil {
			t.Errorf("非法概率未拒绝: %v", probability)
		}
	}
	for _, category := range []string{"", "未知"} {
		item := base
		item.Category = category
		if _, err := uc.OptimizeWishlist(context.Background(), []CandidateWithProb{item}, UserProfile{}); err == nil {
			t.Errorf("非法类别未拒绝: %q", category)
		}
	}
	duplicates := []CandidateWithProb{base, base}
	if _, err := uc.OptimizeWishlist(context.Background(), duplicates, UserProfile{}); err == nil {
		t.Error("优化未拒绝重复组")
	}
	rec := NewRecommendUsecase(nil, nil, &conf.Bootstrap{})
	if _, _, err := rec.CheckConstraints(context.Background(), []string{"物理", "化学", "生物"}, "物理类", duplicates); err == nil {
		t.Error("约束未拒绝重复组")
	}
}

func TestRegression_InvalidSubjects(t *testing.T) {
	uc := NewRecommendUsecase(nil, nil, &conf.Bootstrap{})
	pool := []CandidateWithProb{{Candidate: Candidate{UniversityCode: "A", MajorGroup: "1", SubjectReq: []string{"首选历史，再选不限"}}}}
	for _, subjects := range [][]string{{"历史", "政治", "地理"}, {"物理", "历史", "地理"}, {"历史", "物理", "地理"}, {"物理", "化学", "化学"}, {"物理", "化学", "数学"}, nil} {
		if _, _, err := uc.CheckConstraints(context.Background(), subjects, "物理类", pool); err == nil {
			t.Errorf("非法科类/选科未拒绝: %v", subjects)
		}
	}
}

func TestRegression_StrictSubjectGrammar(t *testing.T) {
	for _, text := range []string{"首选物理，再选化学/生物(2选1)且须生物", "物理，化学", "首选物理，再选化学(2科必选)", "首选物理，再选数学", "首选物理，再选化学/化学(2选1)"} {
		if _, err := parseSubjectReq(text); err == nil {
			t.Errorf("未知格式未拒绝: %s", text)
		}
	}
	for _, reqs := range [][]string{{"首选物理，再选不限", "未知格式"}, {"未知格式", "首选物理，再选不限"}} {
		_, _, err := matchCandidateSubjects([]string{"物理", "化学", "生物"}, reqs)
		if err == nil {
			t.Error("已匹配项掩盖了组内未知格式")
		}
	}
}

func TestRegression_HistoryAudit(t *testing.T) {
	candidate := Candidate{UniversityCode: "A", MajorGroup: "1"}
	key := GroupKey{"A", "1"}
	history := map[GroupKey][]AdmissionRecord{key: {{Year: 2024, GroupMinRank: 400, TotalCount: 1000}}}
	plan := map[GroupKey]PlanChange{key: {PlanCount: 200, PrevPlanCount: 100}}
	res := batchEstimateProb([]Candidate{candidate}, .5, history, plan, testRecommendConfig())
	if len(res) != 1 || len(res[0].HistoryData) != 1 {
		t.Fatalf("缺少历史审计: %+v", res)
	}
	if res[0].HistoryData[0].MinPercentile != .4 || res[0].Probability != .99 {
		t.Fatal("审计原始百分位与计划修正混淆")
	}
	res[0].HistoryData[0].Year = 0
	if !reflect.DeepEqual(history[key], []AdmissionRecord{{Year: 2024, GroupMinRank: 400, TotalCount: 1000}}) {
		t.Fatal("污染共享历史")
	}
}

func TestSystem_DirtyHistory(t *testing.T) {
	key := GroupKey{"A", "1"}
	history := map[GroupKey][]AdmissionRecord{key: {
		{Year: 2024, GroupMinRank: 0, TotalCount: 1000},
		{Year: 2023, GroupMinRank: 1200, TotalCount: 1000},
		{Year: 2022, GroupMinRank: 500, TotalCount: 1000},
	}}
	res := batchEstimateProb([]Candidate{{UniversityCode: "A", MajorGroup: "1"}}, .4, history, nil, testRecommendConfig())
	if len(res) != 1 || res[0].DataYears != 1 || len(res[0].HistoryData) != 1 || res[0].Probability != .99 {
		t.Fatalf("脏历史参与计算或审计不一致: %+v", res)
	}
}

func TestSystem_RankBoundaries(t *testing.T) {
	for _, score := range []float64{math.NaN(), math.Inf(1), math.Inf(-1)} {
		if _, err := ConvertByScore(score, fakeTable(), fakeControl); !errors.Is(err, errs.ErrInvalidQuery) {
			t.Fatalf("非法分数: %v", err)
		}
	}
	if _, err := ConvertByScore(math.MaxFloat64, fakeTable(), fakeControl); !errors.Is(err, errs.ErrScoreAboveMax) {
		t.Fatalf("超大分数溢出: %v", err)
	}
	last := 0
	for score := 750; score >= 693; score-- {
		r, err := ConvertByScore(float64(score), fakeTable(), fakeControl)
		if err != nil || r.Rank < last || r.Rank < 1 || r.Percentile <= 0 || r.Percentile > 1 {
			t.Fatalf("顶端区间 %d: %+v %v", score, r, err)
		}
		last = r.Rank
	}
}

func TestSystem_ConfigBoundaries(t *testing.T) {
	for _, a := range []*conf.Algo{
		{WindowYears: -1}, {WindowYears: 101}, {DecayFactor: math.NaN()}, {DecayFactor: 1.1},
		{PlanAdjust: -1}, {PlanAdjust: 1.1}, {RushThreshold: .9}, {SafeThreshold: 1},
		{MaxWishlistSize: -1}, {MaxWishlistSize: 1001}, {MinSafeCount: -1}, {MinSafeCount: 46},
		{SafeGuardThreshold: .7}, {SafeGuardThreshold: 1}, {PerCategoryLimit: 1},
	} {
		if err := ValidateConfig(&conf.Bootstrap{Algo: a}); !errors.Is(err, errs.ErrInvalidConfig) {
			t.Errorf("非法配置未拒绝: %v %v", a, err)
		}
	}
	if err := ValidateConfig(nil); err != nil {
		t.Fatal(err)
	}
	if err := validateSubjects([]string{"历史", " 政治 ", "地理"}, "历史类"); err != nil {
		t.Fatal(err)
	}
}

func TestSystem_WishlistInvariants(t *testing.T) {
	rng := rand.New(rand.NewSource(42))
	for n := 0; n <= 60; n++ {
		pool := make([]CandidateWithScore, n)
		for i := range pool {
			p := []float64{.3, .6, .85, .99}[i%4]
			cat := CategorySafe
			if p < .5 {
				cat = CategoryRush
			} else if p < .8 {
				cat = CategoryStable
			}
			pool[i] = cws(fmt.Sprint(i), "1", cat, p, float64(i%5)/5)
		}
		for _, risk := range []string{"", "balanced", "conservative", "aggressive", "unknown"} {
			params := OptimizeParams{MaxWishlistSize: 17, MinSafeCount: 5, SafeGuardThreshold: .9, TargetRatio: RatioFromRisk(risk)}
			res, err := OptimizeWishlist(pool, params)
			if err != nil {
				t.Fatal(err)
			}
			if len(res.Candidates) != min(n, 17) {
				t.Fatal("数量不符")
			}
			seen := map[GroupKey]bool{}
			rush, stable, safe, slip, total := 0, 0, 0, 1.0, 0.0
			for i, c := range res.Candidates {
				key := GroupKey{c.UniversityCode, c.MajorGroup}
				if seen[key] || c.FinalRank != i+1 {
					t.Fatal("重复或排名不连续")
				}
				seen[key] = true
				if i > 0 && lessByUtilityThenKey(c, res.Candidates[i-1]) {
					t.Fatal("非全序")
				}
				if c.Category == CategoryRush {
					rush++
				}
				if c.Category == CategoryStable {
					stable++
				}
				if c.Probability >= .9 {
					safe++
				}
				slip *= 1 - c.Probability
				total += c.Score
			}
			if len(res.Candidates) > 0 {
				total /= float64(len(res.Candidates))
			}
			if res.Report.RushCount != rush || res.Report.StableCount != stable || res.Report.SafeCount != safe || res.Report.SafeGuardOK != (safe >= 5) || res.Report.SlipRate != slip || res.Report.ExpectedSatisfaction != total {
				t.Fatal("报告与最终表不一致")
			}
			shuffled := append([]CandidateWithScore(nil), pool...)
			rng.Shuffle(n, func(i, j int) { shuffled[i], shuffled[j] = shuffled[j], shuffled[i] })
			again, err := OptimizeWishlist(shuffled, params)
			if err != nil || !reflect.DeepEqual(res, again) {
				t.Fatal("并列排序依赖输入顺序")
			}
		}
	}
}

func TestRegression_InvalidConfig(t *testing.T) {
	uc := NewRecommendUsecase(&fakeAdmissionRepo{}, NewRankUsecase(&fakeRankRepo{table: fakeTable(), control: fakeControl}), &conf.Bootstrap{Algo: &conf.Algo{DecayFactor: -1}})
	_, err := uc.EstimateProb(context.Background(), UserQuery{Province: "湖南", Category: "物理类", Year: 2025, Score: 450, Subjects: []string{"物理", "化学", "生物"}}, UserProfile{})
	if err == nil {
		t.Fatal("非法衰减配置未拒绝")
	}
}
