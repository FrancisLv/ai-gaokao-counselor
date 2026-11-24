//go:build integration

package data

import (
	"context"
	"fmt"
	"testing"

	"github.com/choucisan/ai-gaokao-counselor/internal/algo/biz"
)

func TestIntegration_StableCandidatesHaveMultiYearHistory(t *testing.T) {
	repo := setupRepo(t)
	ctx := context.Background()

	candidates, err := repo.LoadCandidates(
		ctx,
		itRelease,
		itProvince,
		itCategory,
		itBatch,
		itYear,
	)
	if err != nil {
		t.Fatalf("LoadCandidates: %v", err)
	}
	history, err := repo.BatchGroupHistory(
		ctx,
		itRelease,
		itProvince,
		itCategory,
		itBatch,
		itYear,
		itWindow,
	)
	if err != nil {
		t.Fatalf("BatchGroupHistory: %v", err)
	}
	plans, err := repo.BatchGroupPlan(
		ctx,
		itRelease,
		itProvince,
		itCategory,
		itYear,
		itYear-1,
	)
	if err != nil {
		t.Fatalf("BatchGroupPlan: %v", err)
	}

	rank := biz.NewRankUsecase(NewRankRepo(repo.d))
	cfg := biz.RecommendConfig{
		WindowYears:     itWindow,
		DecayFactor:     0.7,
		RushThreshold:   0.2,
		StableThreshold: 0.5,
		SafeThreshold:   0.8,
	}
	totalStable := 0
	totalMultiYearStable := 0
	for _, score := range []float64{480, 520, 560, 600, 640} {
		rankResult, err := rank.Convert(ctx, biz.RankQuery{
			ReleaseID: itRelease,
			Province:  itProvince,
			Category:  itCategory,
			Year:      itYear,
			Score:     score,
		})
		if err != nil {
			t.Fatalf("RankConvert(%v): %v", score, err)
		}
		result := biz.RecommendCandidates(
			candidates,
			rankResult.Percentile,
			history,
			plans,
			cfg,
		)
		multiYear := 0
		singleYear := 0
		for _, candidate := range result.StableCandidates {
			if len(candidate.HistoryData) > 1 {
				multiYear++
			} else {
				singleYear++
			}
		}
		totalStable += len(result.StableCandidates)
		totalMultiYearStable += multiYear
		t.Logf(
			"score=%v percentile=%.6f stable=%d multi_year_stable=%d single_year_stable=%d",
			score,
			rankResult.Percentile,
			len(result.StableCandidates),
			multiYear,
			singleYear,
		)
		if len(result.StableCandidates) > 0 && multiYear == 0 {
			for index, candidate := range result.StableCandidates {
				if index >= 5 {
					break
				}
				years := make([]int, 0, len(candidate.HistoryData))
				for _, item := range candidate.HistoryData {
					years = append(years, item.Year)
				}
				t.Logf(
					"stable sample %s %s years=%v probability=%.4f",
					candidate.UniversityCode,
					candidate.MajorGroup,
					years,
					candidate.Probability,
				)
			}
		}
	}
	if totalStable == 0 {
		t.Fatal("所有测试分数都没有稳档候选")
	}
	if totalMultiYearStable == 0 {
		t.Fatal(fmt.Sprintf("稳档共 %d 个候选，但没有任何候选具备多年历史", totalStable))
	}
}
