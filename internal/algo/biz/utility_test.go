package biz

import (
	"math"
	"testing"
)

// assertWeightsSum1 断言权重和为 1（按固定 utilityDims 序求和，避免 map 迭代序影响浮点累加）。
func assertWeightsSum1(t *testing.T, w map[string]float64) {
	t.Helper()
	var sum float64
	for _, d := range utilityDims {
		sum += w[d]
	}
	if math.Abs(sum-1.0) > 1e-9 {
		t.Errorf("权重和 = %v, want 1.0 (%+v)", sum, w)
	}
}

func eqWeights(t *testing.T, got map[string]float64, tier, major, loc, str float64) {
	t.Helper()
	want := map[string]float64{DimTier: tier, DimMajor: major, DimLocation: loc, DimStrength: str}
	for _, d := range utilityDims {
		if math.Abs(got[d]-want[d]) > 1e-6 {
			t.Errorf("weights[%s] = %v, want %v (full %+v)", d, got[d], want[d], got)
		}
	}
}

func TestTierScore(t *testing.T) {
	cases := map[string]float64{"985": 1.0, "211": 0.8, "双一流": 0.6, "普通": 0.4, "": 0.4, "未知层次": 0.4}
	for tier, want := range cases {
		if got := tierScore(tier); math.Abs(got-want) > 1e-9 {
			t.Errorf("tierScore(%q) = %v, want %v", tier, got, want)
		}
	}
}

func TestMajorScore(t *testing.T) {
	majors := []MajorDetail{{MajorName: "计算机科学与技术"}, {MajorName: "软件工程"}}
	cases := []struct {
		name      string
		interests []string
		want      float64
	}{
		{"命中子串(计算机)", []string{"计算机"}, 1.0},
		{"命中子串(软件)", []string{"软件"}, 1.0},
		{"多兴趣命中其一", []string{"金融", "计算机"}, 1.0},
		{"全不命中", []string{"金融", "土木"}, 0.0},
		{"无 interests", nil, 0.0},
		{"空串 interest 不命中", []string{""}, 0.0},
		{"无专业明细", []string{"计算机"}, 0.0},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			ms := majors
			if c.name == "无专业明细" {
				ms = nil
			}
			if got := majorScore(c.interests, ms); math.Abs(got-c.want) > 1e-9 {
				t.Errorf("majorScore(%v) = %v, want %v", c.interests, got, c.want)
			}
		})
	}
}

func TestLocationScore(t *testing.T) {
	cases := []struct {
		name     string
		cities   []string
		location string
		province string
		want     float64
	}{
		{"偏好城市精确命中", []string{"长沙市"}, "长沙市", "湖南", 1.0},
		{"偏好城市子串命中(长沙∈长沙市)", []string{"长沙"}, "长沙市", "湖南", 1.0},
		{"偏好城市反向子串(北京市∈北京)", []string{"北京市"}, "北京", "北京", 1.0},
		{"多城市命中其一", []string{"武汉", "长沙"}, "长沙市", "湖南", 1.0},
		{"未命中城市但同省", []string{"长沙"}, "株洲市", "湖南", 0.8},
		{"无偏好城市+同省", nil, "株洲市", "湖南", 0.8},
		{"外省其他", []string{"长沙"}, "武汉市", "湖北", 0.5},
		{"无偏好城市+外省", nil, "武汉市", "湖北", 0.5},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := locationScore(c.cities, c.location, c.province); math.Abs(got-c.want) > 1e-9 {
				t.Errorf("locationScore(%v,%q,%q) = %v, want %v", c.cities, c.location, c.province, got, c.want)
			}
		})
	}
}

func TestMinmax(t *testing.T) {
	cases := []struct {
		x, min, max int
		want        float64
	}{
		{5, 0, 10, 0.5},
		{0, 0, 10, 0.0},
		{10, 0, 10, 1.0},
		{7, 7, 7, 0.5}, // max==min → 0.5
		{15, 10, 20, 0.5},
	}
	for _, c := range cases {
		if got := minmax(c.x, c.min, c.max); math.Abs(got-c.want) > 1e-9 {
			t.Errorf("minmax(%d,%d,%d) = %v, want %v", c.x, c.min, c.max, got, c.want)
		}
	}
}

func TestStrengthScore(t *testing.T) {
	stats := NormStats{MasterMin: 0, MasterMax: 100, DoctorMin: 0, DoctorMax: 50, RuankeMax: 100}
	// 满配：master=100→1.0, doctor=50→1.0, ruanke=1→1-1/100=0.99
	want := 0.4*1.0 + 0.3*1.0 + 0.3*0.99
	if got := strengthScore(Candidate{NumMaster: 100, NumDoctor: 50, RuankeRank: 1}, stats); math.Abs(got-want) > 1e-9 {
		t.Errorf("满配 strengthScore = %v, want %v", got, want)
	}
	// 缺失软科（RuankeRank=0）→ 软科项按 0 计（不误给最高分）
	want2 := 0.4*1.0 + 0.3*1.0 + 0.3*0.0
	if got := strengthScore(Candidate{NumMaster: 100, NumDoctor: 50, RuankeRank: 0}, stats); math.Abs(got-want2) > 1e-9 {
		t.Errorf("缺失软科 strengthScore = %v, want %v", got, want2)
	}
	// 全 0（min==max）→ master/doctor 各 0.5、软科缺失 → 0.4*0.5+0.3*0.5+0 = 0.35
	zero := NormStats{}
	want3 := 0.4*0.5 + 0.3*0.5 + 0.0
	if got := strengthScore(Candidate{}, zero); math.Abs(got-want3) > 1e-9 {
		t.Errorf("全零 strengthScore = %v, want %v", got, want3)
	}
}

func TestComputeNormStats(t *testing.T) {
	cs := []Candidate{
		{NumMaster: 10, NumDoctor: 5, RuankeRank: 50},
		{NumMaster: 30, NumDoctor: 2, RuankeRank: 20},
		{NumMaster: 20, NumDoctor: 8, RuankeRank: 0}, // 缺失软科不影响 max
	}
	s := computeNormStats(cs)
	if s.MasterMin != 10 || s.MasterMax != 30 {
		t.Errorf("master min/max = %d/%d, want 10/30", s.MasterMin, s.MasterMax)
	}
	if s.DoctorMin != 2 || s.DoctorMax != 8 {
		t.Errorf("doctor min/max = %d/%d, want 2/8", s.DoctorMin, s.DoctorMax)
	}
	if s.RuankeMax != 50 {
		t.Errorf("RuankeMax = %d, want 50", s.RuankeMax)
	}
	if e := computeNormStats(nil); e != (NormStats{}) {
		t.Errorf("空批应零值, got %+v", e)
	}
}

func TestDeriveWeights(t *testing.T) {
	cfg := UtilityConfig{DefaultWeights: map[string]float64{
		DimTier: 0.25, DimMajor: 0.25, DimLocation: 0.25, DimStrength: 0.25,
	}}

	// 无偏好 → 各 0.25
	w := deriveWeights(UserProfile{}, cfg)
	eqWeights(t, w, 0.25, 0.25, 0.25, 0.25)
	assertWeightsSum1(t, w)

	// PriorityDims=[major] → major×2 → 0.2/0.4/0.2/0.2（sum=1.25 归一）
	w = deriveWeights(UserProfile{PriorityDims: []string{"major"}}, cfg)
	eqWeights(t, w, 0.2, 0.4, 0.2, 0.2)
	assertWeightsSum1(t, w)

	// 文档例：PriorityDims=[major] + PreferredTiers=[985,211] → tier×2 major×2 → 0.333/0.333/0.167/0.167
	w = deriveWeights(UserProfile{PriorityDims: []string{"major"}, PreferredTiers: []string{"985", "211"}}, cfg)
	eqWeights(t, w, 1.0/3.0, 1.0/3.0, 1.0/6.0, 1.0/6.0)
	assertWeightsSum1(t, w)

	// 封顶：PriorityDims=[tier] + PreferredTiers=[985] → tier 只×2（不×4）→ 0.4/0.2/0.2/0.2
	w = deriveWeights(UserProfile{PriorityDims: []string{"tier"}, PreferredTiers: []string{"985"}}, cfg)
	eqWeights(t, w, 0.4, 0.2, 0.2, 0.2)
	assertWeightsSum1(t, w)

	// 仅 PreferredTiers（无 PriorityDims）→ tier×2 → 0.4/0.2/0.2/0.2
	w = deriveWeights(UserProfile{PreferredTiers: []string{"985"}}, cfg)
	eqWeights(t, w, 0.4, 0.2, 0.2, 0.2)
	assertWeightsSum1(t, w)

	// 未知维度忽略、合法维度仍生效
	w = deriveWeights(UserProfile{PriorityDims: []string{"unknown", "major"}}, cfg)
	eqWeights(t, w, 0.2, 0.4, 0.2, 0.2)

	// 空 config → 兜底 0.25
	w = deriveWeights(UserProfile{}, UtilityConfig{})
	eqWeights(t, w, 0.25, 0.25, 0.25, 0.25)
}

func TestCalculateUtilityBatch(t *testing.T) {
	profile := UserProfile{
		PriorityDims:    []string{"tier"},
		Interests:       []string{"计算机"},
		PreferredCities: []string{"长沙"},
		PreferredTiers:  []string{"985"},
	}
	cs := []Candidate{
		{UniversityCode: "A", SchoolTier: "985", Location: "长沙市", Province: "湖南",
			NumMaster: 100, NumDoctor: 50, RuankeRank: 1, Majors: []MajorDetail{{MajorName: "计算机科学与技术"}}},
		{UniversityCode: "B", SchoolTier: "普通", Location: "武汉市", Province: "湖北",
			NumMaster: 10, NumDoctor: 2, RuankeRank: 200, Majors: []MajorDetail{{MajorName: "机械工程"}}},
	}
	cfg := UtilityConfig{DefaultWeights: map[string]float64{
		DimTier: 0.25, DimMajor: 0.25, DimLocation: 0.25, DimStrength: 0.25,
	}}
	scores, err := CalculateUtilityBatch(profile, cs, cfg)
	if err != nil {
		t.Fatalf("unexpected err: %v", err)
	}
	if len(scores) != 2 {
		t.Fatalf("scores len = %d, want 2", len(scores))
	}
	// A 应显著高于 B（985 + 计算机命中 + 长沙 + 强升学资源）
	if scores[0].Score <= scores[1].Score {
		t.Errorf("A(%v) 应 > B(%v)", scores[0].Score, scores[1].Score)
	}
	for i, s := range scores {
		if s.Score < 0 || s.Score > 1 {
			t.Errorf("scores[%d].Score = %v 越界 [0,1]", i, s.Score)
		}
		if len(s.Breakdown) != 4 {
			t.Errorf("scores[%d].Breakdown 应 4 维, got %d", i, len(s.Breakdown))
		}
		assertWeightsSum1(t, s.Weights)
	}
	// A：tier=1.0(985)、major=1.0(计算机命中)、location=1.0(长沙)
	if scores[0].Breakdown[DimTier] != 1.0 || scores[0].Breakdown[DimMajor] != 1.0 || scores[0].Breakdown[DimLocation] != 1.0 {
		t.Errorf("A breakdown 错: %+v", scores[0].Breakdown)
	}
	// B：major=0(机械≠计算机)、location=0.5(武汉外省)、tier=0.4(普通)
	if scores[1].Breakdown[DimMajor] != 0.0 || scores[1].Breakdown[DimLocation] != 0.5 || scores[1].Breakdown[DimTier] != 0.4 {
		t.Errorf("B breakdown 错: %+v", scores[1].Breakdown)
	}
}

// TestCalculateUtilityBatch_Deterministic 相同输入两次调用结果逐位相同 + 保序（DH1/DH3）。
func TestCalculateUtilityBatch_Deterministic(t *testing.T) {
	profile := UserProfile{PriorityDims: []string{"major", "location"}, Interests: []string{"软件"}, PreferredCities: []string{"长沙"}}
	cs := []Candidate{
		{UniversityCode: "A", SchoolTier: "211", Location: "长沙市", Province: "湖南", NumMaster: 30, NumDoctor: 10, RuankeRank: 40, Majors: []MajorDetail{{MajorName: "软件工程"}}},
		{UniversityCode: "B", SchoolTier: "985", Location: "北京市", Province: "北京", NumMaster: 80, NumDoctor: 40, RuankeRank: 5, Majors: []MajorDetail{{MajorName: "计算机"}}},
		{UniversityCode: "C", SchoolTier: "普通", Location: "长沙市", Province: "湖南", NumMaster: 5, NumDoctor: 0, RuankeRank: 300, Majors: []MajorDetail{{MajorName: "汉语言"}}},
	}
	first, _ := CalculateUtilityBatch(profile, cs, UtilityConfig{})
	second, _ := CalculateUtilityBatch(profile, cs, UtilityConfig{})
	if len(first) != 3 {
		t.Fatalf("len = %d, want 3", len(first))
	}
	for i := range first {
		if first[i].Score != second[i].Score {
			t.Errorf("非确定性: scores[%d] %v != %v", i, first[i].Score, second[i].Score)
		}
	}
}

// TestCalculateUtilityBatch_Empty 空批 → 空结果、无 error、不 panic。
func TestCalculateUtilityBatch_Empty(t *testing.T) {
	scores, err := CalculateUtilityBatch(UserProfile{}, nil, UtilityConfig{})
	if err != nil {
		t.Fatalf("unexpected err: %v", err)
	}
	if len(scores) != 0 {
		t.Errorf("空批应空, got %d", len(scores))
	}
}
