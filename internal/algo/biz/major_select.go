package biz

import (
	"sort"
	"strings"
)

const maxRecommendedMajors = 6

// selectRecommendedMajors 为每个专业组确定性选择至多 6 个默认拟填专业。
// 专业组内专业原有顺序保持不变；只有 Recommended/RecommendationRank 被回填。
func selectRecommendedMajors(candidates []Candidate, profile UserProfile) {
	for candidateIndex := range candidates {
		majors := candidates[candidateIndex].Majors
		order := make([]int, len(majors))
		for index := range order {
			order[index] = index
			majors[index].Recommended = false
			majors[index].RecommendationRank = 0
		}
		sort.SliceStable(order, func(left, right int) bool {
			leftMajor := majors[order[left]]
			rightMajor := majors[order[right]]
			leftPreference := majorPreferenceRank(leftMajor.MajorName, profile)
			rightPreference := majorPreferenceRank(rightMajor.MajorName, profile)
			if leftPreference != rightPreference {
				return leftPreference < rightPreference
			}
			leftProbability := probabilitySortValue(leftMajor.AdmissionProbability)
			rightProbability := probabilitySortValue(rightMajor.AdmissionProbability)
			if leftProbability != rightProbability {
				return leftProbability > rightProbability
			}
			leftPlanCount := planCountSortValue(leftMajor.PlanCount)
			rightPlanCount := planCountSortValue(rightMajor.PlanCount)
			if leftPlanCount != rightPlanCount {
				return leftPlanCount > rightPlanCount
			}
			return leftMajor.MajorCode < rightMajor.MajorCode
		})
		limit := min(len(order), maxRecommendedMajors)
		for rank, majorIndex := range order[:limit] {
			majors[majorIndex].Recommended = true
			majors[majorIndex].RecommendationRank = rank + 1
		}
	}
}

func majorPreferenceRank(majorName string, profile UserProfile) int {
	name := strings.TrimSpace(majorName)
	for index, required := range profile.RequiredMajorNames {
		if name != "" && name == strings.TrimSpace(required) {
			return index
		}
	}
	for index, interest := range profile.Interests {
		if name != "" && name == strings.TrimSpace(interest) {
			return 100 + index
		}
	}
	for index, keyword := range profile.RequiredMajorKeywords {
		normalized := strings.TrimSpace(keyword)
		if name != "" && normalized != "" && strings.Contains(name, normalized) {
			return 200 + index
		}
	}
	return 1000
}

func probabilitySortValue(probability *float64) float64 {
	if probability == nil {
		return -1
	}
	return *probability
}

func planCountSortValue(planCount *int) int {
	if planCount == nil {
		return -1
	}
	return *planCount
}
