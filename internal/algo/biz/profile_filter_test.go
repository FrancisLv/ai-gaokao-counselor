package biz

import "testing"

func TestFilterCandidatesByProfileAppliesHardFiltersAndMajors(t *testing.T) {
	candidates := []Candidate{
		{
			UniversityName: "长沙示例大学",
			Location:       "长沙",
			Province:       "湖南",
			SchoolTier:     "211",
			Majors: []MajorDetail{
				{MajorCode: "A", MajorName: "计算机科学与技术"},
				{MajorCode: "B", MajorName: "土木工程"},
			},
		},
		{
			UniversityName: "武汉示例大学",
			Location:       "武汉",
			Province:       "湖北",
			SchoolTier:     "985",
			Majors:         []MajorDetail{{MajorCode: "C", MajorName: "计算机科学与技术"}},
		},
	}
	got := FilterCandidatesByProfile(candidates, UserProfile{
		RequiredCities:        []string{"长沙"},
		ExcludedCities:        []string{"武汉"},
		RequiredTiers:         []string{"211"},
		RequiredMajorKeywords: []string{"计算机"},
		ExcludedMajorKeywords: []string{"土木"},
	})

	if len(got) != 1 || got[0].UniversityName != "长沙示例大学" {
		t.Fatalf("filtered candidates = %+v", got)
	}
	if len(got[0].Majors) != 1 || got[0].Majors[0].MajorCode != "A" {
		t.Fatalf("filtered majors = %+v", got[0].Majors)
	}
}

func TestFilterCandidatesByProfileAppliesProvinceHardFilters(t *testing.T) {
	candidates := []Candidate{
		{UniversityName: "广州示例大学", Location: "广州", Province: "广东"},
		{UniversityName: "深圳示例大学", Location: "深圳", Province: "广东"},
		{UniversityName: "长沙示例大学", Location: "长沙", Province: "湖南"},
	}

	got := FilterCandidatesByProfile(candidates, UserProfile{
		ExcludedProvinces: []string{"广东"},
	})

	if len(got) != 1 || got[0].UniversityName != "长沙示例大学" {
		t.Fatalf("province filter result = %+v", got)
	}
}

func TestFilterCandidatesByProfilePreservesInputOrder(t *testing.T) {
	in := []Candidate{
		{UniversityName: "甲", Location: "长沙", Majors: []MajorDetail{{MajorName: "法学"}}},
		{UniversityName: "乙", Location: "长沙", Majors: []MajorDetail{{MajorName: "计算机"}}},
	}
	got := FilterCandidatesByProfile(in, UserProfile{RequiredCities: []string{"长沙"}})
	if len(got) != 2 || got[0].UniversityName != "甲" || got[1].UniversityName != "乙" {
		t.Fatalf("order changed: %+v", got)
	}
}

func TestFilterCandidatesByProfileExcludesExactMajorNameOnly(t *testing.T) {
	candidates := []Candidate{{
		UniversityName: "示例大学",
		Location:       "长沙",
		Majors: []MajorDetail{
			{MajorCode: "A", MajorName: "物理学"},
			{MajorCode: "B", MajorName: "应用物理学"},
			{MajorCode: "C", MajorName: "计算机科学与技术"},
		},
	}}

	got := FilterCandidatesByProfile(candidates, UserProfile{
		ExcludedMajorNames: []string{"物理学"},
	})

	if len(got) != 1 {
		t.Fatalf("candidate removed unexpectedly: %+v", got)
	}
	if len(got[0].Majors) != 2 ||
		got[0].Majors[0].MajorName != "应用物理学" ||
		got[0].Majors[1].MajorName != "计算机科学与技术" {
		t.Fatalf("exact major filter result = %+v", got[0].Majors)
	}
}
