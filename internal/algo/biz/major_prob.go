package biz

import "strings"

// attachMajorProbabilities 为当年计划专业回填映射组内同名称的历史参考与概率。
// 名称不一致的记录不得参与计算；保持 majors 输入序，避免 map 迭代影响输出。
func attachMajorProbabilities(
	candidates []Candidate,
	userPercentile float64,
	historyMap map[MajorKey][]MajorAdmissionRecord,
	config RecommendConfig,
) {
	for candidateIndex := range candidates {
		candidate := &candidates[candidateIndex]
		for majorIndex := range candidate.Majors {
			major := &candidate.Majors[majorIndex]
			key := MajorKey{
				UniversityCode: candidate.UniversityCode,
				MajorGroup:     candidate.MajorGroup,
				MajorCode:      major.MajorCode,
			}
			rawHistory := historyMap[key]
			history := make([]AdmissionRecord, 0, len(rawHistory))
			for _, record := range rawHistory {
				if !sameMajorName(major.MajorName, record.MajorName) ||
					record.TotalCount <= 0 ||
					record.MinRank <= 0 ||
					record.MinRank > record.TotalCount {
					continue
				}
				history = append(history, AdmissionRecord{
					Year:          record.Year,
					GroupMinScore: record.MinScore,
					GroupMinRank:  record.MinRank,
					TotalCount:    record.TotalCount,
				})
			}
			if len(history) == 0 {
				major.MinScore = 0
				major.MinRank = 0
				major.ReferenceYear = 0
				major.AdmitCount = 0
				major.AdmissionProbability = nil
				major.ProbabilityDataYears = 0
				major.ProbabilitySampleYears = nil
				continue
			}

			latest := history[0]
			major.MinScore = latest.GroupMinScore
			major.MinRank = latest.GroupMinRank
			major.ReferenceYear = latest.Year
			sampleYears := make([]int, 0, len(history))
			for index := range history {
				history[index].MinPercentile = RankToPercentile(
					history[index].GroupMinRank,
					history[index].TotalCount,
				)
				sampleYears = append(sampleYears, history[index].Year)
			}
			for left, right := 0, len(sampleYears)-1; left < right; left, right = left+1, right-1 {
				sampleYears[left], sampleYears[right] =
					sampleYears[right], sampleYears[left]
			}
			probability := calculateProbability(
				userPercentile,
				history,
				config,
			)
			major.AdmissionProbability = &probability
			major.ProbabilityDataYears = len(history)
			major.ProbabilitySampleYears = sampleYears
		}
	}
}

func sameMajorName(current, historical string) bool {
	current = strings.TrimSpace(current)
	return current != "" && current == strings.TrimSpace(historical)
}
