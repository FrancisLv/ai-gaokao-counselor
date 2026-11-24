package biz

import "testing"

func TestAttachMajorProbabilities_UsesCurrentMajorKeyAndKeepsUnknown(t *testing.T) {
	planCount := 12
	candidates := []Candidate{{
		UniversityCode: "1001",
		MajorGroup:     "03",
		Majors: []MajorDetail{
			{
				MajorCode: "080901",
				MajorName: "计算机科学与技术",
				PlanCount: &planCount,
			},
			{
				MajorCode: "080902",
				MajorName: "软件工程",
				PlanCount: &planCount,
			},
		},
	}}
	history := map[MajorKey][]MajorAdmissionRecord{
		{UniversityCode: "1001", MajorGroup: "03", MajorCode: "080901"}: {
			{Year: 2024, MajorName: "计算机科学与技术", MinScore: 630, MinRank: 5000, TotalCount: 10000},
			{Year: 2023, MajorName: "计算机科学与技术", MinScore: 640, MinRank: 2000, TotalCount: 10000},
		},
	}

	attachMajorProbabilities(candidates, 0.5, history, testRecommendConfig())

	known := candidates[0].Majors[0]
	if known.ReferenceYear != 2024 || known.MinScore != 630 || known.MinRank != 5000 {
		t.Fatalf("latest history = %+v", known)
	}
	if known.AdmissionProbability == nil {
		t.Fatal("known major probability is nil")
	}
	if got, want := *known.AdmissionProbability, 0.5/1.7; got < want-0.000001 || got > want+0.000001 {
		t.Fatalf("probability = %f, want %f", got, want)
	}
	if known.ProbabilityDataYears != 2 {
		t.Fatalf("probability data years = %d", known.ProbabilityDataYears)
	}
	if len(known.ProbabilitySampleYears) != 2 ||
		known.ProbabilitySampleYears[0] != 2023 ||
		known.ProbabilitySampleYears[1] != 2024 {
		t.Fatalf("probability sample years = %v", known.ProbabilitySampleYears)
	}

	unknown := candidates[0].Majors[1]
	if unknown.AdmissionProbability != nil || unknown.ProbabilityDataYears != 0 {
		t.Fatalf("unknown major received fabricated probability: %+v", unknown)
	}
	if unknown.MinScore != 0 || unknown.MinRank != 0 || unknown.ReferenceYear != 0 {
		t.Fatalf("unknown major retained stale history: %+v", unknown)
	}
	if unknown.PlanCount == nil || *unknown.PlanCount != 12 {
		t.Fatalf("current plan count lost: %+v", unknown.PlanCount)
	}
}

func TestAttachMajorProbabilities_AcceptsRenumberedSameName(t *testing.T) {
	planCount := 4
	candidates := []Candidate{{
		UniversityCode: "3702",
		MajorGroup:     "（212）",
		Majors: []MajorDetail{{
			MajorCode: "51",
			MajorName: "数学与应用数学",
			PlanCount: &planCount,
		}},
	}}
	history := map[MajorKey][]MajorAdmissionRecord{
		{UniversityCode: "3702", MajorGroup: "（212）", MajorCode: "51"}: {
			{Year: 2024, MajorName: "数学与应用数学", MinScore: 599, MinRank: 14586, TotalCount: 313685},
			{Year: 2023, MajorName: "数学与应用数学", MinScore: 600, MinRank: 15149, TotalCount: 277310},
		},
	}

	attachMajorProbabilities(candidates, 0.05, history, testRecommendConfig())

	major := candidates[0].Majors[0]
	if major.ReferenceYear != 2024 ||
		major.MinScore != 599 ||
		major.MinRank != 14586 {
		t.Fatalf("renumbered same-name major history = %+v", major)
	}
	if major.AdmissionProbability == nil || major.ProbabilityDataYears != 2 {
		t.Fatalf("renumbered same-name major probability = %+v", major)
	}
}

func TestAttachMajorProbabilities_RejectsSameCodeWithDifferentMajorName(t *testing.T) {
	planCount := 3
	candidates := []Candidate{{
		UniversityCode: "2302",
		MajorGroup:     "（104）",
		Majors: []MajorDetail{{
			MajorCode: "19",
			MajorName: "新能源材料与器件",
			PlanCount: &planCount,
		}},
	}}
	history := map[MajorKey][]MajorAdmissionRecord{
		{UniversityCode: "2302", MajorGroup: "（104）", MajorCode: "19"}: {
			{Year: 2024, MajorName: "软件工程", MinScore: 612, MinRank: 10069, TotalCount: 313685},
			{Year: 2022, MajorName: "计算机类", MinScore: 537, MinRank: 40696, TotalCount: 254970},
		},
	}

	attachMajorProbabilities(candidates, 0.064, history, testRecommendConfig())

	major := candidates[0].Majors[0]
	if major.AdmissionProbability != nil ||
		major.ProbabilityDataYears != 0 ||
		len(major.ProbabilitySampleYears) != 0 {
		t.Fatalf("mismatched major identity received probability: %+v", major)
	}
	if major.MinScore != 0 || major.MinRank != 0 || major.ReferenceYear != 0 {
		t.Fatalf("mismatched major identity retained history: %+v", major)
	}
	if major.PlanCount == nil || *major.PlanCount != 3 {
		t.Fatalf("current plan count lost: %+v", major)
	}
}
