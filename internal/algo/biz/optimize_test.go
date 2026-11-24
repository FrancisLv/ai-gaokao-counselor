package biz

import (
	"context"
	"errors"
	"math"
	"testing"

	"github.com/choucisan/ai-gaokao-counselor/conf"
	errs "github.com/choucisan/ai-gaokao-counselor/internal/pkg/errors"
)

func BenchmarkOptimizeWishlist(b *testing.B) {
	candidates, history := benchmarkCandidates()
	res := RecommendCandidates(candidates, 0.3, history, nil, testRecommendConfig())
	pool := append(append(res.RushCandidates, res.StableCandidates...), res.SafeCandidates...)
	uc := NewOptimizeUsecase(&conf.Bootstrap{})
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if _, err := uc.OptimizeWishlist(context.Background(), pool, UserProfile{}); err != nil {
			b.Fatal(err)
		}
	}
}

// cws 构造测试用 CandidateWithScore（只填 optimize 关心的字段）。
func cws(code, group, category string, prob, score float64) CandidateWithScore {
	return CandidateWithScore{
		CandidateWithProb: CandidateWithProb{
			Candidate:    Candidate{UniversityCode: code, MajorGroup: group},
			ProbEstimate: ProbEstimate{Probability: prob, Category: category},
		},
		UtilityScore: UtilityScore{Score: score},
	}
}

func TestRatioFromRisk(t *testing.T) {
	cases := []struct {
		risk               string
		rush, stable, safe float64
	}{
		{"conservative", 0.2, 0.4, 0.4},
		{"aggressive", 0.45, 0.35, 0.2},
		{"balanced", 0.3, 0.4, 0.3},
		{"", 0.3, 0.4, 0.3},
		{"未知偏好", 0.3, 0.4, 0.3},
	}
	for _, c := range cases {
		got := RatioFromRisk(c.risk)
		if math.Abs(got.Rush-c.rush) > 1e-9 || math.Abs(got.Stable-c.stable) > 1e-9 || math.Abs(got.Safe-c.safe) > 1e-9 {
			t.Errorf("RatioFromRisk(%q) = %+v, want {%v %v %v}", c.risk, got, c.rush, c.stable, c.safe)
		}
	}
}

func TestLessByUtilityThenKey(t *testing.T) {
	hi := cws("B", "1", CategoryRush, 0.3, 0.9)
	lo := cws("A", "1", CategoryRush, 0.3, 0.5)
	if !lessByUtilityThenKey(hi, lo) {
		t.Error("score 高者应排前（降序）")
	}
	if lessByUtilityThenKey(lo, hi) {
		t.Error("score 低者不应排前")
	}
	// 同分：UniversityCode 升序
	a := cws("A", "9", CategoryRush, 0.3, 0.7)
	b := cws("B", "1", CategoryRush, 0.3, 0.7)
	if !lessByUtilityThenKey(a, b) {
		t.Error("同分应 UniversityCode 升序")
	}
	// 同分同校：MajorGroup 升序
	g1 := cws("A", "1", CategoryRush, 0.3, 0.7)
	g2 := cws("A", "2", CategoryRush, 0.3, 0.7)
	if !lessByUtilityThenKey(g1, g2) {
		t.Error("同分同校应 MajorGroup 升序")
	}
	if lessByUtilityThenKey(g1, g1) {
		t.Error("全序比较器应严格（相等返回 false）")
	}
}

func TestSplitByCategory(t *testing.T) {
	in := []CandidateWithScore{
		cws("A", "1", CategoryRush, 0.3, 0.5),
		cws("B", "1", CategoryStable, 0.6, 0.5),
		cws("C", "1", CategorySafe, 0.95, 0.5),
		cws("D", "1", CategoryRush, 0.25, 0.5),
		cws("E", "1", "未知档", 0.5, 0.5), // 非三档，应被忽略
	}
	rush, stable, safe := splitByCategory(in)
	if len(rush) != 2 || len(stable) != 1 || len(safe) != 1 {
		t.Errorf("分档数量错误：rush=%d stable=%d safe=%d, want 2/1/1", len(rush), len(stable), len(safe))
	}
}

func TestSelectByRatio_QuotaAllocation(t *testing.T) {
	// L=5, ratio balanced{0.3,0.4,0.3} → rushT=2, stableT=2, safeT=1
	cands := []CandidateWithScore{
		cws("R1", "1", CategoryRush, 0.3, 0.9),
		cws("R2", "1", CategoryRush, 0.3, 0.8),
		cws("R3", "1", CategoryRush, 0.3, 0.7),
		cws("S1", "1", CategoryStable, 0.6, 0.6),
		cws("S2", "1", CategoryStable, 0.6, 0.5),
		cws("S3", "1", CategoryStable, 0.6, 0.4),
		cws("B1", "1", CategorySafe, 0.95, 0.3),
		cws("B2", "1", CategorySafe, 0.95, 0.2),
	}
	sel, picked := selectByRatio(cands, RatioConfig{0.3, 0.4, 0.3}, 5)
	if len(sel) != 5 {
		t.Fatalf("选出数量 = %d, want 5", len(sel))
	}
	if len(picked) != 5 {
		t.Errorf("picked 集大小 = %d, want 5", len(picked))
	}
	// 各档取 utility 最高：rush 应含 R1/R2（0.9/0.8），stable 含 S1/S2，safe 含 B1
	byCat := map[string]int{}
	for _, c := range sel {
		byCat[c.Category]++
	}
	if byCat[CategoryRush] != 2 || byCat[CategoryStable] != 2 || byCat[CategorySafe] != 1 {
		t.Errorf("配比分档错误：%+v, want 冲2/稳2/保1", byCat)
	}
	for _, want := range []string{"R1", "R2", "S1", "S2", "B1"} {
		if _, ok := picked[GroupKey{UniversityCode: want, MajorGroup: "1"}]; !ok {
			t.Errorf("期望选中 %s（各档 utility 最高）", want)
		}
	}
}

func TestSelectByRatio_DeficitFill(t *testing.T) {
	// rush 配额 3 但只有 1 个 rush → 空位按全局 utility 降序从剩余补
	cands := []CandidateWithScore{
		cws("R1", "1", CategoryRush, 0.3, 0.95),
		cws("S1", "1", CategoryStable, 0.6, 0.90),
		cws("S2", "1", CategoryStable, 0.6, 0.80),
		cws("B1", "1", CategorySafe, 0.95, 0.70),
		cws("B2", "1", CategorySafe, 0.95, 0.60),
		cws("B3", "1", CategorySafe, 0.95, 0.50),
	}
	// L=4, ratio{0.75,0,0.25} → rushT=3(只有1), stableT=0, safeT=1 → 直接选2，补2
	sel, _ := selectByRatio(cands, RatioConfig{0.75, 0.0, 0.25}, 4)
	if len(sel) != 4 {
		t.Fatalf("补足后数量 = %d, want 4", len(sel))
	}
	// 补位应取全局 utility 最高的未选：S1(0.90)、S2(0.80)
	picked := map[string]bool{}
	for _, c := range sel {
		picked[c.UniversityCode] = true
	}
	for _, want := range []string{"R1", "S1", "S2"} {
		if !picked[want] {
			t.Errorf("补位应含 %s", want)
		}
	}
}

func TestBuildSafePool(t *testing.T) {
	cands := []CandidateWithScore{
		cws("A", "1", CategorySafe, 0.95, 0.5),
		cws("B", "1", CategorySafe, 0.99, 0.4),
		cws("C", "1", CategoryStable, 0.6, 0.9), // p<0.9 排除
		cws("D", "1", CategorySafe, 0.92, 0.3),  // 已选中，排除
	}
	selected := map[GroupKey]struct{}{{UniversityCode: "D", MajorGroup: "1"}: {}}
	pool := buildSafePool(cands, selected, 0.9)
	if len(pool) != 2 {
		t.Fatalf("稳拿池大小 = %d, want 2 (A,B)", len(pool))
	}
	// 按 p 降序：B(0.99) 先于 A(0.95)
	if pool[0].UniversityCode != "B" || pool[1].UniversityCode != "A" {
		t.Errorf("稳拿池应按 Probability 降序，got [%s %s]", pool[0].UniversityCode, pool[1].UniversityCode)
	}
}

func TestCheckSafeGuard_AppendPropagation(t *testing.T) {
	// 原表无稳拿（p=0.3<0.9），minSafe=2 → 从池追加 2 个，验证指针传播
	wl := []CandidateWithScore{cws("A", "1", CategoryRush, 0.3, 0.5)}
	pool := []CandidateWithScore{
		cws("B", "1", CategorySafe, 0.99, 0.4),
		cws("C", "1", CategorySafe, 0.95, 0.3),
	}
	rep := checkSafeGuard(&wl, pool, 2, 0.9, 3)
	if len(wl) != 3 {
		t.Fatalf("追加后志愿表长度 = %d, want 3（指针传播）", len(wl))
	}
	if rep.SafeCount != 2 || !rep.SafeGuardOK {
		t.Errorf("SafeCount=%d SafeGuardOK=%v, want 2/true", rep.SafeCount, rep.SafeGuardOK)
	}
	// 追加项 FinalRank 续在表尾
	if wl[1].FinalRank != 2 || wl[2].FinalRank != 3 {
		t.Errorf("追加项 FinalRank 错误：[%d %d], want [2 3]", wl[1].FinalRank, wl[2].FinalRank)
	}
	// 滑档率 Π(1−pᵢ) = 0.7×0.01×0.05
	wantSlip := 0.7 * 0.01 * 0.05
	if math.Abs(rep.SlipRate-wantSlip) > 1e-9 {
		t.Errorf("SlipRate = %v, want %v", rep.SlipRate, wantSlip)
	}
}

func TestCheckSafeGuard_NoRequirement(t *testing.T) {
	// minSafe<=0 视为无保底要求 → SafeGuardOK 恒真，不追加
	wl := []CandidateWithScore{cws("A", "1", CategoryRush, 0.3, 0.5)}
	rep := checkSafeGuard(&wl, nil, 0, 0.9, 1)
	if len(wl) != 1 || !rep.SafeGuardOK || rep.SafeCount != 0 {
		t.Errorf("minSafe=0 应无追加且 OK：len=%d SafeCount=%d OK=%v", len(wl), rep.SafeCount, rep.SafeGuardOK)
	}
}

func TestCheckSafeGuard_InsufficientWarning(t *testing.T) {
	// 稳拿池不足以达标 → SafeGuardOK=false（告警非 error）
	wl := []CandidateWithScore{cws("A", "1", CategoryRush, 0.3, 0.5)}
	rep := checkSafeGuard(&wl, nil, 3, 0.9, 1)
	if rep.SafeGuardOK {
		t.Error("无稳拿池且 minSafe=3 → SafeGuardOK 应为 false")
	}
	if rep.SafeCount != 0 {
		t.Errorf("SafeCount = %d, want 0", rep.SafeCount)
	}
}

func TestOptimizeWishlist_EndToEnd(t *testing.T) {
	cands := []CandidateWithScore{
		cws("R1", "1", CategoryRush, 0.30, 0.9),
		cws("R2", "1", CategoryRush, 0.25, 0.85),
		cws("R3", "1", CategoryRush, 0.20, 0.80),
		cws("S1", "1", CategoryStable, 0.60, 0.75),
		cws("S2", "1", CategoryStable, 0.55, 0.70),
		cws("S3", "1", CategoryStable, 0.50, 0.65),
		cws("B1", "1", CategorySafe, 0.95, 0.60),
		cws("B2", "1", CategorySafe, 0.97, 0.55),
		cws("B3", "1", CategorySafe, 0.99, 0.50),
	}
	params := OptimizeParams{
		MaxWishlistSize:    5,
		MinSafeCount:       2,
		SafeGuardThreshold: 0.9,
		TargetRatio:        RatioConfig{0.3, 0.4, 0.3},
	}
	res, err := OptimizeWishlist(cands, params)
	if err != nil {
		t.Fatalf("OptimizeWishlist 返回 error: %v", err)
	}
	// 配额选满5项后，以保底替换效用最低的非稳拿项 S2；最终仍不超过5项。
	if len(res.Candidates) != 5 {
		t.Fatalf("志愿表长度 = %d, want 5", len(res.Candidates))
	}
	if res.Report.RushCount != 2 || res.Report.StableCount != 1 {
		t.Errorf("RushCount=%d StableCount=%d, want 2/1", res.Report.RushCount, res.Report.StableCount)
	}
	if res.Report.SafeCount != 2 || !res.Report.SafeGuardOK {
		t.Errorf("SafeCount=%d SafeGuardOK=%v, want 2/true", res.Report.SafeCount, res.Report.SafeGuardOK)
	}
	// FinalRank 连续 1..N
	for i, c := range res.Candidates {
		if c.FinalRank != i+1 {
			t.Errorf("Candidates[%d].FinalRank = %d, want %d", i, c.FinalRank, i+1)
		}
	}
	// 保底修复后整个最终表仍按 utility 降序。
	for i := 0; i+1 < 5; i++ {
		if res.Candidates[i].Score < res.Candidates[i+1].Score {
			t.Errorf("志愿表未 utility 降序：[%d]=%v < [%d]=%v", i, res.Candidates[i].Score, i+1, res.Candidates[i+1].Score)
		}
	}
	if res.Report.ExpectedSatisfaction <= 0 {
		t.Errorf("ExpectedSatisfaction = %v, want >0", res.Report.ExpectedSatisfaction)
	}
}

func TestOptimizeWishlist_DefaultsFallback(t *testing.T) {
	// params 零值：L<=0→45, threshold<=0→0.9，不 panic
	cands := []CandidateWithScore{cws("A", "1", CategorySafe, 0.95, 0.5)}
	res, err := OptimizeWishlist(cands, OptimizeParams{})
	if err != nil {
		t.Fatalf("零值 params 不应报错: %v", err)
	}
	if len(res.Candidates) != 1 {
		t.Errorf("志愿表长度 = %d, want 1", len(res.Candidates))
	}
}

func TestOptimizeUsecase_OptimizeWishlist_WithoutProb(t *testing.T) {
	uc := NewOptimizeUsecase(&conf.Bootstrap{})
	// Probability 零值 → ErrCandidateWithoutProb
	in := []CandidateWithProb{{
		Candidate:    Candidate{UniversityCode: "A", MajorGroup: "1"},
		ProbEstimate: ProbEstimate{Probability: 0, Category: CategoryRush},
	}}
	_, err := uc.OptimizeWishlist(context.Background(), in, UserProfile{})
	if !errors.Is(err, errs.ErrCandidateWithoutProb) {
		t.Errorf("err = %v, want ErrCandidateWithoutProb", err)
	}
}

func TestOptimizeUsecase_OptimizeWishlist_HappyPath(t *testing.T) {
	uc := NewOptimizeUsecase(&conf.Bootstrap{})
	in := []CandidateWithProb{
		{
			Candidate:    Candidate{UniversityCode: "A", MajorGroup: "1", SchoolTier: "985"},
			ProbEstimate: ProbEstimate{Probability: 0.95, Category: CategorySafe, Method: methodWeightedHit},
		},
		{
			Candidate:    Candidate{UniversityCode: "B", MajorGroup: "1", SchoolTier: "211"},
			ProbEstimate: ProbEstimate{Probability: 0.30, Category: CategoryRush, Method: methodWeightedHit},
		},
	}
	// RiskAppetite=aggressive 覆盖 TargetRatio
	res, err := uc.OptimizeWishlist(context.Background(), in, UserProfile{RiskAppetite: "aggressive"})
	if err != nil {
		t.Fatalf("happy path 不应报错: %v", err)
	}
	if len(res.Candidates) == 0 {
		t.Fatal("志愿表不应为空")
	}
	// utility 已注入（Score 非全零，985 tier 得分 > 211）
	if res.Candidates[0].Score <= 0 {
		t.Errorf("效用分应已注入，got Score=%v", res.Candidates[0].Score)
	}
}

func TestSummarizeWishlistDraft(t *testing.T) {
	itemScores := []float64{0.9, 0.7, 0.5}
	groups := []DraftGroupRiskInput{
		{
			UniversityCode: "A",
			MajorGroup:     "01",
			Probability:    0.95,
			Category:       CategorySafe,
			UtilityScore:   &itemScores[0],
		},
		{
			UniversityCode: "B",
			MajorGroup:     "02",
			Probability:    0.60,
			Category:       CategoryStable,
			UtilityScore:   &itemScores[1],
		},
		{
			UniversityCode: "C",
			MajorGroup:     "03",
			Probability:    0.30,
			Category:       CategoryRush,
			UtilityScore:   &itemScores[2],
		},
	}
	result, err := SummarizeWishlistDraft(groups, OptimizeParams{
		MaxWishlistSize:    45,
		MinSafeCount:       2,
		SafeGuardThreshold: 0.9,
	})
	if err != nil {
		t.Fatalf("SummarizeWishlistDraft 返回 error: %v", err)
	}
	if result.GroupCount != 3 || result.RushCount != 1 ||
		result.StableCount != 1 || result.SafeTierCount != 1 {
		t.Fatalf("分组统计错误：%+v", result)
	}
	if result.SafeGuardCount != 1 || result.SafeGuardTarget != 2 ||
		result.SafeGuardOK {
		t.Fatalf("保底统计错误：%+v", result)
	}
	wantSlip := (1 - 0.95) * (1 - 0.60) * (1 - 0.30)
	if math.Abs(result.SlipRate-wantSlip) > 1e-12 {
		t.Fatalf("SlipRate=%v, want %v", result.SlipRate, wantSlip)
	}
	if result.ExpectedSatisfaction == nil ||
		math.Abs(*result.ExpectedSatisfaction-0.7) > 1e-12 {
		t.Fatalf("ExpectedSatisfaction=%v, want 0.7", result.ExpectedSatisfaction)
	}
}

func TestSummarizeWishlistDraftRejectsInvalidSelection(t *testing.T) {
	_, err := SummarizeWishlistDraft([]DraftGroupRiskInput{
		{
			UniversityCode: "A",
			MajorGroup:     "01",
			Probability:    0.60,
			Category:       CategoryStable,
		},
		{
			UniversityCode: "A",
			MajorGroup:     "01",
			Probability:    0.70,
			Category:       CategoryStable,
		},
	}, OptimizeParams{})
	if !errors.Is(err, errs.ErrInvalidCandidate) {
		t.Fatalf("err=%v, want ErrInvalidCandidate", err)
	}
}
