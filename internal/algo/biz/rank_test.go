package biz

import (
	"context"
	"errors"
	"math"
	"testing"

	errs "github.com/choucisan/ai-gaokao-counselor/internal/pkg/errors"
)

// fakeTable 模拟真实 score_rank_map（湖南物理类）：顶段合并区间 + 单分行 + 本科线行 + 底段（全省总人数）。
// totalCount = MAX(CumulativeCount) = 313685（底段 0-99 那行）。
func fakeTable() []ScoreRankEntry {
	return []ScoreRankEntry{
		{Score: 693, ScoreRangeLow: 693, ScoreRangeHigh: 750, SegmentCount: 51, CumulativeCount: 51, RankRangeLow: 1, RankRangeHigh: 51},
		{Score: 450, ScoreRangeLow: 450, ScoreRangeHigh: 450, SegmentCount: 1000, CumulativeCount: 153585, RankRangeLow: 153585, RankRangeHigh: 153585},
		{Score: 405, ScoreRangeLow: 405, ScoreRangeHigh: 405, SegmentCount: 2000, CumulativeCount: 190233, RankRangeLow: 190233, RankRangeHigh: 190233},
		{Score: 0, ScoreRangeLow: 0, ScoreRangeHigh: 99, SegmentCount: 5000, CumulativeCount: 313685, RankRangeLow: 300000, RankRangeHigh: 313685},
	}
}

const fakeControl = 405 // 本科线

func almostEq(a, b float64) bool { return math.Abs(a-b) < 1e-9 }

func TestConvertByScore_SingleRow(t *testing.T) {
	// 450 分 → 单分行 cumulative=153585
	res, err := ConvertByScore(450, fakeTable(), fakeControl)
	if err != nil {
		t.Fatalf("unexpected err: %v", err)
	}
	if res.Rank != 153585 {
		t.Errorf("Rank = %d, want 153585", res.Rank)
	}
	if res.TotalCount != 313685 {
		t.Errorf("TotalCount = %d, want 313685（全省总人数=MAX cumulative）", res.TotalCount)
	}
	if res.ControlScore != fakeControl {
		t.Errorf("ControlScore = %d, want %d", res.ControlScore, fakeControl)
	}
	if !almostEq(res.Percentile, 153585.0/313685.0) {
		t.Errorf("Percentile = %f, want %f", res.Percentile, 153585.0/313685.0)
	}
}

func TestConvertByScore_InterpolatedTopSegment(t *testing.T) {
	// 700 分落在合并区间 693-750：ratio=(750-700)/(750-693)=50/57≈0.877；rank=1+round(0.877×50)=45（§3.1 示例）
	res, err := ConvertByScore(700, fakeTable(), fakeControl)
	if err != nil {
		t.Fatalf("unexpected err: %v", err)
	}
	if res.Rank != 45 {
		t.Errorf("Rank = %d, want 45（顶段插值）", res.Rank)
	}
}

func TestConvertByScore_FloatRounding(t *testing.T) {
	// DG2-A：入口 int(math.Round(score))；450.4→450（命中单分行），450.6→451（无独立行则落区间/缺口）
	// 450.4 应等价于 450
	res, err := ConvertByScore(450.4, fakeTable(), fakeControl)
	if err != nil {
		t.Fatalf("unexpected err: %v", err)
	}
	if res.Rank != 153585 {
		t.Errorf("Rank(450.4) = %d, want 153585（round→450）", res.Rank)
	}
}

func TestConvertByScore_BelowControlLine(t *testing.T) {
	// 404 < 本科线 405 → ErrBelowControlLine
	_, err := ConvertByScore(404, fakeTable(), fakeControl)
	if !errors.Is(err, errs.ErrBelowControlLine) {
		t.Errorf("err = %v, want ErrBelowControlLine", err)
	}
}

func TestConvertByScore_AtControlLine(t *testing.T) {
	// 405 == 本科线，应放行并命中本科线行
	res, err := ConvertByScore(405, fakeTable(), fakeControl)
	if err != nil {
		t.Fatalf("unexpected err: %v", err)
	}
	if res.Rank != 190233 {
		t.Errorf("Rank(405) = %d, want 190233", res.Rank)
	}
}

func TestConvertByScore_AboveMax(t *testing.T) {
	// 751 > 表内最高 750 → ErrScoreAboveMax（DG1-A：严禁外推）
	_, err := ConvertByScore(751, fakeTable(), fakeControl)
	if !errors.Is(err, errs.ErrScoreAboveMax) {
		t.Errorf("err = %v, want ErrScoreAboveMax", err)
	}
}

func TestConvertByScore_EmptyTable(t *testing.T) {
	_, err := ConvertByScore(500, nil, fakeControl)
	if !errors.Is(err, errs.ErrMissingScoreRankMap) {
		t.Errorf("err = %v, want ErrMissingScoreRankMap", err)
	}
}

func TestRankToPercentile(t *testing.T) {
	if got := RankToPercentile(45, 200000); !almostEq(got, 0.000225) {
		t.Errorf("RankToPercentile(45,200000) = %f, want 0.000225", got)
	}
	if got := RankToPercentile(10, 0); got != 0 {
		t.Errorf("RankToPercentile(10,0) = %f, want 0（分母≤0 保护）", got)
	}
}

// fakeRankRepo 实现 RankRepo，供 RankUsecase.Convert 测试（无需 DB）。
type fakeRankRepo struct {
	table   []ScoreRankEntry
	control int
	err     error
}

func (f *fakeRankRepo) LoadScoreRankMap(ctx context.Context, province, category string, year int) ([]ScoreRankEntry, int, error) {
	if f.err != nil {
		return nil, 0, f.err
	}
	return f.table, f.control, nil
}

func TestRankUsecase_Convert(t *testing.T) {
	uc := NewRankUsecase(&fakeRankRepo{table: fakeTable(), control: fakeControl})
	res, err := uc.Convert(context.Background(), RankQuery{Province: "湖南", Category: "物理类", Year: 2025, Score: 450})
	if err != nil {
		t.Fatalf("unexpected err: %v", err)
	}
	if res.Rank != 153585 {
		t.Errorf("Rank = %d, want 153585", res.Rank)
	}
}

func TestRankUsecase_Convert_RepoError(t *testing.T) {
	// DY5：报考年一分一段表缺失 → data 层返回 ErrMissingScoreRankMap，usecase 透传
	uc := NewRankUsecase(&fakeRankRepo{err: errs.ErrMissingScoreRankMap})
	_, err := uc.Convert(context.Background(), RankQuery{Province: "湖南", Category: "物理类", Year: 2025, Score: 450})
	if !errors.Is(err, errs.ErrMissingScoreRankMap) {
		t.Errorf("err = %v, want ErrMissingScoreRankMap", err)
	}
}
