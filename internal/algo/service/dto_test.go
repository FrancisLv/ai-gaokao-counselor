package service

import (
	"reflect"
	"testing"

	"github.com/choucisan/ai-gaokao-counselor/internal/algo/biz"
)

// dto_test.go —— biz↔pb 映射 round-trip 保真测试（§4.3：fromPb(toPb(x)) == x）。
// 只覆盖双向映射类型（输出专用类型如 AuditBasis/OptimizationReport/WishlistResult 在 algo_test.go 断言字段值）。

func TestRoundTrip_MajorDetail(t *testing.T) {
	planCount := 30
	probability := 0.72
	in := biz.MajorDetail{
		MajorCode: "080901", MajorName: "计算机科学与技术", SubjectReq: "首选物理，再选不限",
		Duration: "四年", Tuition: 5900.0, HasTuition: true, MajorNote: "（卓越班）",
		MinScore: 620, MinRank: 3500, ReferenceYear: 2024, AdmitCount: 30,
		PlanCount: &planCount, AdmissionProbability: &probability,
		ProbabilityDataYears: 2, ProbabilitySampleYears: []int{2023, 2024},
		Recommended: true, RecommendationRank: 1,
	}
	if got := fromPbMajorDetail(toPbMajorDetail(in)); !reflect.DeepEqual(got, in) {
		t.Errorf("MajorDetail round-trip = %+v, want %+v", got, in)
	}
}

func TestRoundTrip_AdmissionRecord(t *testing.T) {
	in := biz.AdmissionRecord{
		Year: 2024, GroupMinScore: 620, GroupMinRank: 1500,
		TotalCount: 313685, MinPercentile: 0.0048, EnrollCount: 25,
		MappingMethod: "MAJOR_NAME_JACCARD", MappingConfidence: 0.75,
	}
	if got := fromPbAdmissionRecord(toPbAdmissionRecord(in)); !reflect.DeepEqual(got, in) {
		t.Errorf("AdmissionRecord round-trip = %+v, want %+v", got, in)
	}
}

func TestRoundTrip_Candidate(t *testing.T) {
	factor := 0.8913043478
	in := biz.Candidate{
		UniversityID: 101, UniversityCode: "1101", UniversityName: "北京大学", MajorGroup: "（109）",
		GroupMinRank: 161, SchoolTier: "985", Location: "北京市", Province: "北京",
		SubjectReq: []string{"首选物理，再选化学"},
		NumMaster:  10, NumDoctor: 8, RuankeRank: 2,
		PlanCount: 120, PlanCountKnown: true,
		PreviousPlanCount: 138, PreviousPlanCountKnown: true,
		PlanAdjustFactor: &factor,
		Majors: []biz.MajorDetail{
			{MajorCode: "070101", MajorName: "数学与应用数学", SubjectReq: "首选物理，再选不限", Duration: "四年", Tuition: 5000, HasTuition: true, MinScore: 680, MinRank: 150, ReferenceYear: 2024, AdmitCount: 5},
		},
		HistoryData: []biz.AdmissionRecord{
			{Year: 2024, GroupMinRank: 155, TotalCount: 313685, MinPercentile: 0.0005, EnrollCount: 6},
		},
	}
	if got := fromPbCandidate(toPbCandidate(in)); !reflect.DeepEqual(got, in) {
		t.Errorf("Candidate round-trip = %+v, want %+v", got, in)
	}
}

func TestRoundTrip_Candidate_NilSlices(t *testing.T) {
	// 空 slice 字段 round-trip 后应保持 nil（nil/空一致约定）
	in := biz.Candidate{UniversityCode: "1101", MajorGroup: "（109）"}
	got := fromPbCandidate(toPbCandidate(in))
	if got.Majors != nil || got.SubjectReq != nil || got.HistoryData != nil {
		t.Errorf("空 slice 应保持 nil，got Majors=%v SubjectReq=%v HistoryData=%v", got.Majors, got.SubjectReq, got.HistoryData)
	}
}

func TestRoundTrip_ProbEstimate(t *testing.T) {
	in := biz.ProbEstimate{Probability: 0.87, Method: "weighted_hit", DataYears: 3, Category: "保"}
	if got := fromPbProbEstimate(toPbProbEstimate(in)); !reflect.DeepEqual(got, in) {
		t.Errorf("ProbEstimate round-trip = %+v, want %+v", got, in)
	}
}

func TestRoundTrip_CandidateWithProb(t *testing.T) {
	in := biz.CandidateWithProb{
		Candidate:    biz.Candidate{UniversityID: 7, UniversityCode: "1102", UniversityName: "清华大学", MajorGroup: "（101）", SchoolTier: "985", Province: "北京"},
		ProbEstimate: biz.ProbEstimate{Probability: 0.35, Method: "weighted_hit", DataYears: 3, Category: "冲"},
	}
	if got := fromPbCandidateWithProb(toPbCandidateWithProb(in)); !reflect.DeepEqual(got, in) {
		t.Errorf("CandidateWithProb round-trip = %+v, want %+v", got, in)
	}
}

func TestRoundTrip_UtilityScore(t *testing.T) {
	in := biz.UtilityScore{
		Score:     0.73,
		Breakdown: map[string]float64{"tier": 1.0, "major": 0.5, "location": 0.8, "strength": 0.6},
		Weights:   map[string]float64{"tier": 0.33, "major": 0.33, "location": 0.17, "strength": 0.17},
	}
	if got := fromPbUtilityScore(toPbUtilityScore(in)); !reflect.DeepEqual(got, in) {
		t.Errorf("UtilityScore round-trip = %+v, want %+v", got, in)
	}
}

func TestRoundTrip_CandidateWithScore(t *testing.T) {
	in := biz.CandidateWithScore{
		CandidateWithProb: biz.CandidateWithProb{
			Candidate:    biz.Candidate{UniversityCode: "1103", MajorGroup: "（2）", SchoolTier: "211", Province: "湖南"},
			ProbEstimate: biz.ProbEstimate{Probability: 0.95, Method: "weighted_hit", DataYears: 3, Category: "保"},
		},
		UtilityScore: biz.UtilityScore{
			Score:     0.66,
			Breakdown: map[string]float64{"tier": 0.8, "major": 0.5},
			Weights:   map[string]float64{"tier": 0.4, "major": 0.6},
		},
		FinalRank: 3,
	}
	if got := fromPbCandidateWithScore(toPbCandidateWithScore(in)); !reflect.DeepEqual(got, in) {
		t.Errorf("CandidateWithScore round-trip = %+v, want %+v", got, in)
	}
}

func TestRoundTrip_UserProfile(t *testing.T) {
	in := biz.UserProfile{
		RiskAppetite: "aggressive", PriorityDims: []string{"tier", "major"},
		Interests: []string{"计算机"}, PreferredCities: []string{"北京", "上海"}, PreferredTiers: []string{"985"},
		RequiredProvinces: []string{"湖南"}, ExcludedProvinces: []string{"广东"},
		RequiredCities: []string{"长沙"}, ExcludedCities: []string{"武汉"},
		RequiredTiers: []string{"211"}, RequiredMajorKeywords: []string{"计算机"},
		ExcludedMajorKeywords: []string{"土木"}, ExcludedMajorNames: []string{"物理学"},
		ExcludedUniversities: []string{"示例大学"},
	}
	if got := fromPbUserProfile(toPbUserProfile(in)); !reflect.DeepEqual(got, in) {
		t.Errorf("UserProfile round-trip = %+v, want %+v", got, in)
	}
}

func TestRoundTrip_CandidateWithProbSlice_Empty(t *testing.T) {
	if got := toPbCandidateWithProbSlice(nil); got != nil {
		t.Errorf("toPb(nil slice) 应为 nil, got %v", got)
	}
	if got := fromPbCandidateWithProbSlice(nil); got != nil {
		t.Errorf("fromPb(nil slice) 应为 nil, got %v", got)
	}
}

func TestToPbAuditBasis(t *testing.T) {
	in := biz.AuditBasis{
		UserRank: 153634, UserPercentile: 0.49, SampleYears: []int{2022, 2023, 2024},
		Method: "weighted_hit", Province: "湖南", Category: "物理类",
	}
	pb := toPbAuditBasis(in)
	if pb.GetUserRank() != 153634 || pb.GetUserPercentile() != 0.49 {
		t.Errorf("AuditBasis rank/percentile 映射错误：%v/%v", pb.GetUserRank(), pb.GetUserPercentile())
	}
	if !reflect.DeepEqual(pb.GetSampleYears(), []int32{2022, 2023, 2024}) {
		t.Errorf("SampleYears = %v, want [2022 2023 2024]", pb.GetSampleYears())
	}
	if pb.GetMethod() != "weighted_hit" || pb.GetProvince() != "湖南" || pb.GetCategory() != "物理类" {
		t.Errorf("AuditBasis 标量字段映射错误：%+v", pb)
	}
}

func TestToPbOptimizationReport(t *testing.T) {
	in := biz.OptimizationReport{
		RushCount: 14, StableCount: 18, SafeCount: 13, SafeGuardOK: true,
		SlipRate: 1e-37, ExpectedSatisfaction: 0.3988,
	}
	pb := toPbOptimizationReport(in)
	if pb.GetRushCount() != 14 || pb.GetStableCount() != 18 || pb.GetSafeCount() != 13 {
		t.Errorf("report 计数映射错误：%+v", pb)
	}
	if !pb.GetSafeGuardOk() {
		t.Error("SafeGuardOk 应为 true")
	}
	if pb.GetSlipRate() != 1e-37 || pb.GetExpectedSatisfaction() != 0.3988 {
		t.Errorf("report 浮点字段映射错误：slip=%v sat=%v", pb.GetSlipRate(), pb.GetExpectedSatisfaction())
	}
}

func TestToPbWishlistResult(t *testing.T) {
	in := biz.WishlistResult{
		Candidates: []biz.CandidateWithScore{
			{
				CandidateWithProb: biz.CandidateWithProb{
					Candidate:    biz.Candidate{UniversityCode: "1101", MajorGroup: "（109）"},
					ProbEstimate: biz.ProbEstimate{Probability: 0.95, Category: "保"},
				},
				UtilityScore: biz.UtilityScore{Score: 0.8},
				FinalRank:    1,
			},
		},
		Report: biz.OptimizationReport{RushCount: 0, StableCount: 0, SafeCount: 1, SafeGuardOK: false, SlipRate: 0.05},
	}
	pb := toPbWishlistResult(in)
	if len(pb.GetCandidates()) != 1 {
		t.Fatalf("wishlist candidates 数 = %d, want 1", len(pb.GetCandidates()))
	}
	if pb.GetCandidates()[0].GetFinalRank() != 1 {
		t.Errorf("FinalRank = %v, want 1", pb.GetCandidates()[0].GetFinalRank())
	}
	if pb.GetReport().GetSafeCount() != 1 || pb.GetReport().GetSafeGuardOk() {
		t.Errorf("report 映射错误：%+v", pb.GetReport())
	}
}

func TestToPbEstimateProbReply(t *testing.T) {
	res := biz.RecommendResult{
		RushCandidates:   []biz.CandidateWithProb{{Candidate: biz.Candidate{UniversityCode: "A"}, ProbEstimate: biz.ProbEstimate{Probability: 0.3, Category: "冲"}}},
		StableCandidates: []biz.CandidateWithProb{{Candidate: biz.Candidate{UniversityCode: "B"}, ProbEstimate: biz.ProbEstimate{Probability: 0.6, Category: "稳"}}},
		SafeCandidates:   []biz.CandidateWithProb{{Candidate: biz.Candidate{UniversityCode: "C"}, ProbEstimate: biz.ProbEstimate{Probability: 0.9, Category: "保"}}},
		RushTotal:        1, StableTotal: 1, SafeTotal: 1,
		Basis: biz.AuditBasis{UserRank: 100, UserPercentile: 0.9, SampleYears: []int{2022, 2023, 2024}, Method: "weighted_hit", Province: "湖南", Category: "物理类"},
	}
	pb := toPbEstimateProbReply(res)
	if len(pb.GetRushCandidates()) != 1 || len(pb.GetStableCandidates()) != 1 || len(pb.GetSafeCandidates()) != 1 {
		t.Errorf("三档候选映射错误：rush=%d stable=%d safe=%d", len(pb.GetRushCandidates()), len(pb.GetStableCandidates()), len(pb.GetSafeCandidates()))
	}
	if pb.GetRushTotal() != 1 || pb.GetStableTotal() != 1 || pb.GetSafeTotal() != 1 {
		t.Errorf("*_total 映射错误：%+v", pb)
	}
	if pb.GetBasis().GetUserRank() != 100 {
		t.Errorf("basis 映射错误：%+v", pb.GetBasis())
	}
}
