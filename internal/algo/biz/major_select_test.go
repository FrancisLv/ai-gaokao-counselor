package biz

import "testing"

func TestSelectRecommendedMajors_DeterministicLimitAndPreference(t *testing.T) {
	highProbability := 0.9
	lowProbability := 0.2
	planCount := 10
	candidates := []Candidate{{
		Majors: []MajorDetail{
			{MajorCode: "07", MajorName: "专业7", PlanCount: &planCount},
			{MajorCode: "01", MajorName: "专业1", PlanCount: &planCount, AdmissionProbability: &lowProbability},
			{MajorCode: "02", MajorName: "专业2", PlanCount: &planCount, AdmissionProbability: &highProbability},
			{MajorCode: "03", MajorName: "专业3", PlanCount: &planCount},
			{MajorCode: "04", MajorName: "专业4", PlanCount: &planCount},
			{MajorCode: "05", MajorName: "专业5", PlanCount: &planCount},
			{MajorCode: "06", MajorName: "专业6", PlanCount: &planCount},
		},
	}}

	selectRecommendedMajors(candidates, UserProfile{Interests: []string{"专业7"}})

	recommended := 0
	for _, major := range candidates[0].Majors {
		if major.Recommended {
			recommended++
		}
	}
	if recommended != 6 {
		t.Fatalf("recommended count = %d, want 6", recommended)
	}
	if !candidates[0].Majors[0].Recommended || candidates[0].Majors[0].RecommendationRank != 1 {
		t.Fatalf("interest match must be rank 1: %+v", candidates[0].Majors[0])
	}
	if !candidates[0].Majors[2].Recommended || candidates[0].Majors[2].RecommendationRank != 2 {
		t.Fatalf("higher probability must follow interest: %+v", candidates[0].Majors[2])
	}
	if candidates[0].Majors[6].RecommendationRank != 0 {
		t.Fatalf("seventh default major must not receive rank: %+v", candidates[0].Majors[6])
	}
}
