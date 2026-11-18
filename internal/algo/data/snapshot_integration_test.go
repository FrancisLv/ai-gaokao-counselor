//go:build integration

package data

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"sort"
	"testing"
	"time"

	"github.com/choucisan/ai-gaokao-counselor/internal/algo/biz"
	errs "github.com/choucisan/ai-gaokao-counselor/internal/pkg/errors"
)

// 输出完整结果摘要，供同一数据库快照上的重构前后对照；不把可变业务数据写成永久断言。
func TestIntegration_RepositorySnapshot(t *testing.T) {
	repo := setupRepo(t)
	rank := NewRankRepo(repo.d)
	for _, category := range []string{"物理类", "历史类"} {
		t.Run(category, func(t *testing.T) {
			ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
			defer cancel()
			candidates, err := repo.LoadCandidates(ctx, itProvince, category, itBatch, itYear)
			if err != nil {
				t.Fatal(err)
			}
			table, control, err := rank.LoadScoreRankMap(ctx, itProvince, category, itYear)
			if err != nil {
				t.Fatal(err)
			}
			history, err := repo.BatchGroupHistory(ctx, itProvince, category, itBatch, itYear, itWindow)
			if err != nil {
				t.Fatal(err)
			}
			plans, err := repo.BatchGroupPlan(ctx, itProvince, category, itYear, itYear-1)
			if err != nil {
				t.Fatal(err)
			}

			// map 按完整组键转为有序列表，保留每组历史的原始年序。
			type historyEntry struct {
				Key     biz.GroupKey
				Records []biz.AdmissionRecord
			}
			type planEntry struct {
				Key  biz.GroupKey
				Plan biz.PlanChange
			}
			var histories []historyEntry
			for key, records := range history {
				histories = append(histories, historyEntry{key, records})
			}
			var planList []planEntry
			for key, plan := range plans {
				planList = append(planList, planEntry{key, plan})
			}
			less := func(a, b biz.GroupKey) bool {
				if a.UniversityCode != b.UniversityCode {
					return a.UniversityCode < b.UniversityCode
				}
				return a.MajorGroup < b.MajorGroup
			}
			sort.Slice(histories, func(i, j int) bool { return less(histories[i].Key, histories[j].Key) })
			sort.Slice(planList, func(i, j int) bool { return less(planList[i].Key, planList[j].Key) })
			for _, item := range []struct {
				name  string
				value any
			}{
				{"候选", candidates}, {"一分一段", table}, {"历史", histories}, {"计划", planList},
			} {
				encoded, err := json.Marshal(item.value)
				if err != nil {
					t.Fatal(err)
				}
				t.Logf("%s sha256=%x", item.name, sha256.Sum256(encoded))
			}
			t.Logf("候选=%d 一分一段=%d 本科线=%d 历史组=%d 计划组=%d", len(candidates), len(table), control, len(history), len(plans))
		})
	}
}

func TestIntegration_RepositoryEmptyAndCanceled(t *testing.T) {
	repo := setupRepo(t)
	ctx := context.Background()
	_, _, err := NewRankRepo(repo.d).LoadScoreRankMap(ctx, itProvince, itCategory, 9999)
	if !errors.Is(err, errs.ErrMissingScoreRankMap) {
		t.Fatalf("缺表错误: %v", err)
	}
	history, err := repo.BatchGroupHistory(ctx, itProvince, itCategory, itBatch, 9999, 3)
	if err != nil || len(history) != 0 {
		t.Fatalf("空历史: %v %v", history, err)
	}
	plans, err := repo.BatchGroupPlan(ctx, itProvince, itCategory, 9999, 9998)
	if err != nil || len(plans) != 0 {
		t.Fatalf("空计划: %v %v", plans, err)
	}
	canceled, cancel := context.WithCancel(ctx)
	cancel()
	_, err = repo.LoadCandidates(canceled, itProvince, itCategory, itBatch, itYear)
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("取消未透传: %v", err)
	}
}
