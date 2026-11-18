package biz

import (
	"fmt"
	"math"
	"testing"
)

// testRecommendConfig 单测用配置（与 algo.yaml 默认一致：W=3、decay=0.7、plan_adjust=0.5、阈值 0.2/0.5/0.8）。
func testRecommendConfig() RecommendConfig {
	return RecommendConfig{
		WindowYears:     3,
		DecayFactor:     0.7,
		PlanAdjust:      0.5,
		RushThreshold:   0.2,
		StableThreshold: 0.5,
		SafeThreshold:   0.8,
	}
}

// benchmarkCandidates 使用固定规模与唯一 GroupKey，数据构造不计入 benchmark。
func benchmarkCandidates() ([]Candidate, map[GroupKey][]AdmissionRecord) {
	candidates := make([]Candidate, 2165)
	history := make(map[GroupKey][]AdmissionRecord, len(candidates))
	reqs := []string{"首选物理，再选不限", "首选物理，再选化学", "首选物理，再选思想政治", "首选物理，再选化学/生物(2选1)"}
	for i := range candidates {
		candidates[i] = Candidate{UniversityCode: fmt.Sprintf("%05d", i), MajorGroup: "(101)", SubjectReq: []string{reqs[i%len(reqs)]}}
		history[GroupKey{candidates[i].UniversityCode, candidates[i].MajorGroup}] = []AdmissionRecord{
			{Year: 2024, GroupMinRank: 10000 + i*100, TotalCount: 300000},
			{Year: 2023, GroupMinRank: 20000 + i*100, TotalCount: 300000},
			{Year: 2022, GroupMinRank: 30000 + i*100, TotalCount: 300000},
		}
	}
	return candidates, history
}

func BenchmarkRecommendCandidates(b *testing.B) {
	candidates, history := benchmarkCandidates()
	cfg := testRecommendConfig()
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		res := RecommendCandidates(candidates, 0.3, history, nil, cfg)
		if res.TotalCandidates != len(candidates) {
			b.Fatal("候选数量变化")
		}
	}
}

// 原权重计算作为优化对照，逐位比较而非误差容忍。
func TestCalculateProbability_ExactReference(t *testing.T) {
	for _, decay := range []float64{0.1, 0.7, 0.9, 1} {
		for n := 1; n <= 8; n++ {
			for mask := 0; mask < 1<<n; mask++ {
				history := make([]AdmissionRecord, n)
				weights := make([]float64, n)
				for i := range history {
					weights[i] = math.Pow(decay, float64(i))
					if mask&(1<<i) != 0 {
						history[i].MinPercentile = 0.8
					}
				}
				var total, hits float64
				for i, h := range history {
					total += weights[i]
					if 0.5 <= h.MinPercentile {
						hits += weights[i]
					}
				}
				want := math.Max(0.01, math.Min(0.99, hits/total))
				cfg := testRecommendConfig()
				cfg.DecayFactor = decay
				got := calculateProbability(0.5, history, cfg)
				if math.Float64bits(got) != math.Float64bits(want) {
					t.Fatalf("decay=%v n=%d mask=%d: %v != %v", decay, n, mask, got, want)
				}
			}
		}
	}
}

// hist 快捷构造 AdmissionRecord（直接给 MinPercentile，供 calculateProbability 用）。
func hist(year int, minPercentile float64) AdmissionRecord {
	return AdmissionRecord{Year: year, MinPercentile: minPercentile}
}

func TestDeriveSchoolTier(t *testing.T) {
	cases := []struct {
		is985, is211, isDual bool
		want                 string
	}{
		{true, true, true, "985"},   // 优先级最高
		{true, false, false, "985"}, // 985 单独
		{false, true, true, "211"},  // 211 优先于双一流
		{false, true, false, "211"},
		{false, false, true, "双一流"},
		{false, false, false, "普通"},
	}
	for _, c := range cases {
		if got := DeriveSchoolTier(c.is985, c.is211, c.isDual); got != c.want {
			t.Errorf("DeriveSchoolTier(%v,%v,%v) = %q, want %q", c.is985, c.is211, c.isDual, got, c.want)
		}
	}
}

// —— calculateProbability：覆盖 0 年 / ≥3 年 / 1~2 年 三种历史长度 ——

func TestCalculateProbability_ZeroYears(t *testing.T) {
	// 0 年（无历史）→ 最低概率 0.01
	if got := calculateProbability(0.5, nil, testRecommendConfig()); got != 0.01 {
		t.Errorf("空历史 prob = %f, want 0.01", got)
	}
	if got := calculateProbability(0.5, []AdmissionRecord{}, testRecommendConfig()); got != 0.01 {
		t.Errorf("空 slice prob = %f, want 0.01", got)
	}
}

func TestCalculateProbability_ThreeYearsAllHit(t *testing.T) {
	// §3.2 文档示例：用户 0.042，三年录取百分位 0.048/0.059/0.068 全命中 → 1.0 → 上限 0.99
	h := []AdmissionRecord{hist(2024, 0.048), hist(2023, 0.059), hist(2022, 0.068)}
	if got := calculateProbability(0.042, h, testRecommendConfig()); got != 0.99 {
		t.Errorf("全命中 prob = %f, want 0.99（1.0 封顶）", got)
	}
}

func TestCalculateProbability_ThreeYearsPartialHit(t *testing.T) {
	// 权重 [1.0, 0.7, 0.49]，ΣW=2.19。用户 0.45：
	//   2024(0.50) 命中、2023(0.40) 不中、2022(0.60) 命中 → ΣHit=1.0+0.49=1.49 → 1.49/2.19≈0.680365
	h := []AdmissionRecord{hist(2024, 0.50), hist(2023, 0.40), hist(2022, 0.60)}
	want := 1.49 / 2.19
	got := calculateProbability(0.45, h, testRecommendConfig())
	if !almostEq(got, want) {
		t.Errorf("部分命中 prob = %f, want %f", got, want)
	}
	if got <= 0.5 || got >= 0.8 {
		t.Errorf("prob=%f 应落在稳档 [0.5,0.8)", got)
	}
}

func TestCalculateProbability_ThreeYearsNoHit(t *testing.T) {
	// 用户百分位高于所有录取百分位 → 0 命中 → 0.0 → 下限 0.01
	h := []AdmissionRecord{hist(2024, 0.10), hist(2023, 0.12), hist(2022, 0.08)}
	if got := calculateProbability(0.90, h, testRecommendConfig()); got != 0.01 {
		t.Errorf("0 命中 prob = %f, want 0.01（下限）", got)
	}
}

func TestCalculateProbability_OneYear(t *testing.T) {
	// 1 年：权重 [1.0]，命中 → 1.0→0.99；不命中 → 0.0→0.01
	cfg := testRecommendConfig()
	if got := calculateProbability(0.30, []AdmissionRecord{hist(2024, 0.50)}, cfg); got != 0.99 {
		t.Errorf("1 年命中 prob = %f, want 0.99", got)
	}
	if got := calculateProbability(0.70, []AdmissionRecord{hist(2024, 0.50)}, cfg); got != 0.01 {
		t.Errorf("1 年不命中 prob = %f, want 0.01", got)
	}
}

func TestCalculateProbability_TwoYears(t *testing.T) {
	// 2 年：权重 [1.0, 0.7]，ΣW=1.7。仅最近年命中 → 1.0/1.7≈0.588235（稳档）
	h := []AdmissionRecord{hist(2024, 0.60), hist(2023, 0.20)}
	want := 1.0 / 1.7
	got := calculateProbability(0.50, h, testRecommendConfig())
	if !almostEq(got, want) {
		t.Errorf("2 年 prob = %f, want %f", got, want)
	}
}

func TestCalculateProbability_BoundaryHitIsInclusive(t *testing.T) {
	// 用户百分位 == 录取百分位 → 「够得着」（≤ 含等号）
	h := []AdmissionRecord{hist(2024, 0.50)}
	if got := calculateProbability(0.50, h, testRecommendConfig()); got != 0.99 {
		t.Errorf("相等应命中 prob = %f, want 0.99", got)
	}
}

// —— batchEstimateProb：MinPercentile 计算 + 计划修正 + 跳过无历史 + 不污染入参 ——

func TestBatchEstimateProb_PercentileAndSkip(t *testing.T) {
	cfg := testRecommendConfig()
	cands := []Candidate{
		{UniversityCode: "U1", MajorGroup: "(101)", UniversityName: "有历史"},
		{UniversityCode: "U2", MajorGroup: "(102)", UniversityName: "无历史"}, // historyMap 缺此 key → 跳过
	}
	// U1：3 年，GroupMinRank/TotalCount → MinPercentile=200000/313685≈0.6376（用户 0.4896 够得着 → 全命中）
	historyMap := map[GroupKey][]AdmissionRecord{
		{UniversityCode: "U1", MajorGroup: "(101)"}: {
			{Year: 2024, GroupMinRank: 200000, TotalCount: 313685},
			{Year: 2023, GroupMinRank: 200000, TotalCount: 313685},
			{Year: 2022, GroupMinRank: 200000, TotalCount: 313685},
		},
	}
	got := batchEstimateProb(cands, 0.4896, historyMap, nil, cfg)
	if len(got) != 1 {
		t.Fatalf("应只剩 1 个有历史的候选，got %d", len(got))
	}
	if got[0].UniversityCode != "U1" {
		t.Errorf("保留的应是 U1，got %s", got[0].UniversityCode)
	}
	if got[0].Probability != 0.99 {
		t.Errorf("U1 全命中 prob = %f, want 0.99", got[0].Probability)
	}
	if got[0].Method != "weighted_hit" {
		t.Errorf("Method = %q, want weighted_hit", got[0].Method)
	}
	if got[0].DataYears != 3 {
		t.Errorf("DataYears = %d, want 3", got[0].DataYears)
	}
	// 入参 historyMap 不被污染（MinPercentile 在拷贝上算，原值仍为 0）
	if historyMap[GroupKey{UniversityCode: "U1", MajorGroup: "(101)"}][0].MinPercentile != 0 {
		t.Errorf("historyMap 被污染：原 MinPercentile 应仍为 0")
	}
}

func TestBatchEstimateProb_PlanAdjustFlips(t *testing.T) {
	cfg := testRecommendConfig()
	cands := []Candidate{{UniversityCode: "U1", MajorGroup: "(101)"}}
	// 无计划修正时：MinPercentile=400/1000=0.4，用户 0.5 > 0.4 → 不命中 → 0.01
	// 有计划修正：PlanCount=200/PrevPlanCount=100 → change=1.0 → ×(1+1.0×0.5)=1.5 → 0.4×1.5=0.6，用户 0.5 ≤ 0.6 → 命中 → 0.99
	historyMap := map[GroupKey][]AdmissionRecord{
		{UniversityCode: "U1", MajorGroup: "(101)"}: {{Year: 2024, GroupMinRank: 400, TotalCount: 1000}},
	}
	planMap := map[GroupKey]PlanChange{
		{UniversityCode: "U1", MajorGroup: "(101)"}: {PlanCount: 200, PrevPlanCount: 100},
	}
	got := batchEstimateProb(cands, 0.5, historyMap, planMap, cfg)
	if len(got) != 1 || got[0].Probability != 0.99 {
		t.Fatalf("计划修正后应命中 → 0.99，got %+v", got)
	}
	// 对照：无 planMap 时不命中
	gotNoPlan := batchEstimateProb(cands, 0.5, historyMap, nil, cfg)
	if gotNoPlan[0].Probability != 0.01 {
		t.Errorf("无计划修正应 0.01，got %f", gotNoPlan[0].Probability)
	}
}

func TestBatchEstimateProb_PrevPlanZeroNoAdjust(t *testing.T) {
	// PrevPlanCount=0（去年未招）→ 不做计划修正（防除零）
	cfg := testRecommendConfig()
	cands := []Candidate{{UniversityCode: "U1", MajorGroup: "(101)"}}
	historyMap := map[GroupKey][]AdmissionRecord{
		{UniversityCode: "U1", MajorGroup: "(101)"}: {{Year: 2024, GroupMinRank: 400, TotalCount: 1000}},
	}
	planMap := map[GroupKey]PlanChange{{UniversityCode: "U1", MajorGroup: "(101)"}: {PlanCount: 200, PrevPlanCount: 0}}
	got := batchEstimateProb(cands, 0.5, historyMap, planMap, cfg)
	// MinPercentile=0.4，用户 0.5 不命中 → 0.01（未因修正翻转，证明 PrevPlanCount=0 时跳过修正）
	if got[0].Probability != 0.01 {
		t.Errorf("PrevPlanCount=0 应跳过修正 → 0.01，got %f", got[0].Probability)
	}
}

// —— categorize：阈值分档 + Total 回填 ——

func TestCategorize_Thresholds(t *testing.T) {
	cfg := testRecommendConfig()
	cwp := []CandidateWithProb{
		{Candidate: Candidate{UniversityCode: "F"}, ProbEstimate: ProbEstimate{Probability: 0.10}}, // < 0.2 淘汰
		{Candidate: Candidate{UniversityCode: "R"}, ProbEstimate: ProbEstimate{Probability: 0.30}}, // 冲
		{Candidate: Candidate{UniversityCode: "S"}, ProbEstimate: ProbEstimate{Probability: 0.60}}, // 稳
		{Candidate: Candidate{UniversityCode: "B"}, ProbEstimate: ProbEstimate{Probability: 0.90}}, // 保
	}
	res := categorize(cwp, cfg)
	if res.TotalCandidates != 4 {
		t.Errorf("TotalCandidates = %d, want 4", res.TotalCandidates)
	}
	if res.FilteredCount != 1 {
		t.Errorf("FilteredCount = %d, want 1", res.FilteredCount)
	}
	if len(res.RushCandidates) != 1 || res.RushCandidates[0].Category != "冲" {
		t.Errorf("冲档错误：%+v", res.RushCandidates)
	}
	if len(res.StableCandidates) != 1 || res.StableCandidates[0].Category != "稳" {
		t.Errorf("稳档错误：%+v", res.StableCandidates)
	}
	if len(res.SafeCandidates) != 1 || res.SafeCandidates[0].Category != "保" {
		t.Errorf("保档错误：%+v", res.SafeCandidates)
	}
	// H3/Q33：Total 回填
	if res.RushTotal != 1 || res.StableTotal != 1 || res.SafeTotal != 1 {
		t.Errorf("*Total 未正确回填：rush=%d stable=%d safe=%d", res.RushTotal, res.StableTotal, res.SafeTotal)
	}
}

func TestCategorize_BoundaryValues(t *testing.T) {
	cfg := testRecommendConfig()
	// 阈值边界：0.2→冲（>=Rush 且 <Stable）、0.5→稳、0.8→保
	cwp := []CandidateWithProb{
		{ProbEstimate: ProbEstimate{Probability: 0.2}},
		{ProbEstimate: ProbEstimate{Probability: 0.5}},
		{ProbEstimate: ProbEstimate{Probability: 0.8}},
	}
	res := categorize(cwp, cfg)
	if len(res.RushCandidates) != 1 || len(res.StableCandidates) != 1 || len(res.SafeCandidates) != 1 {
		t.Errorf("边界分档错误：rush=%d stable=%d safe=%d", len(res.RushCandidates), len(res.StableCandidates), len(res.SafeCandidates))
	}
}

// —— RecommendCandidates：组合封装 ——

func TestRecommendCandidates_Composition(t *testing.T) {
	cfg := testRecommendConfig()
	cands := []Candidate{
		{UniversityCode: "U1", MajorGroup: "(101)"}, // 全命中 → 保
		{UniversityCode: "U2", MajorGroup: "(102)"}, // 无历史 → 跳过
	}
	historyMap := map[GroupKey][]AdmissionRecord{
		{UniversityCode: "U1", MajorGroup: "(101)"}: {
			{Year: 2024, GroupMinRank: 200000, TotalCount: 313685},
			{Year: 2023, GroupMinRank: 200000, TotalCount: 313685},
			{Year: 2022, GroupMinRank: 200000, TotalCount: 313685},
		},
	}
	res := RecommendCandidates(cands, 0.4896, historyMap, nil, cfg)
	if res.TotalCandidates != 1 { // U2 无历史被 batchEstimateProb 跳过，不进 categorize
		t.Errorf("TotalCandidates = %d, want 1", res.TotalCandidates)
	}
	if len(res.SafeCandidates) != 1 {
		t.Errorf("应有 1 个保档，got %d", len(res.SafeCandidates))
	}
	if math.Abs(res.SafeCandidates[0].Probability-0.99) > 1e-9 {
		t.Errorf("保档概率 = %f, want 0.99", res.SafeCandidates[0].Probability)
	}
}
