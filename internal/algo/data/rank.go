package data

import (
	"context"
	"strconv"
	"strings"

	"github.com/choucisan/ai-gaokao-counselor/internal/algo/biz"
	"github.com/choucisan/ai-gaokao-counselor/internal/algo/data/dbgen"
	errs "github.com/choucisan/ai-gaokao-counselor/internal/pkg/errors"
)

// batchUndergrad MVP 固定本科批（controlScore 取本科线；candidate SQL 固定本科批）。
const batchUndergrad = "本科批"

type rankRepo struct {
	d *Data
}

// NewRankRepo 返回 biz.RankRepo 实现（wire 注入）。
func NewRankRepo(d *Data) biz.RankRepo {
	return &rankRepo{d: d}
}

// LoadScoreRankMap 读取完整一分一段表并提取本科线；缺报考年数据时明确报错，不回退年份。
func (r *rankRepo) LoadScoreRankMap(ctx context.Context, releaseID, province, category string, year int) ([]biz.ScoreRankEntry, int, error) {
	rows, err := r.d.queries.ListScoreRanks(ctx, dbgen.ListScoreRanksParams{
		ReleaseID: releaseID, Province: province, Category: category, ReportYear: int32(year),
	})
	if err != nil {
		return nil, 0, err
	}
	if len(rows) == 0 {
		return nil, 0, errs.ErrMissingScoreRankMap
	}
	table, controlScore := scoreRankTable(rows)
	return table, controlScore, nil
}

// scoreRankTable 保留所有批次的分数行，本科线单独取本科批首个有效值。
func scoreRankTable(rows []dbgen.ListScoreRanksRow) ([]biz.ScoreRankEntry, int) {
	table := make([]biz.ScoreRankEntry, 0, len(rows))
	controlScore := 0
	for _, row := range rows {
		if row.Batch == batchUndergrad && row.ControlScore > 0 && controlScore == 0 {
			controlScore = int(row.ControlScore)
		}
		table = append(table, scoreRankEntryFromRow(row))
	}
	return table, controlScore
}

// scoreRankEntryFromRow 解析分数段与位次段，空值或坏格式回退到该行的单值。
func scoreRankEntryFromRow(row dbgen.ListScoreRanksRow) biz.ScoreRankEntry {
	score, cumulative := int(row.Score), int(row.CumulativeCount)
	scoreLow, scoreHigh := parseRange(row.ScoreRange, score, score)
	rankLow, rankHigh := parseRange(row.RankRange, cumulative, cumulative)
	return biz.ScoreRankEntry{
		Score:         score,
		ScoreRangeLow: scoreLow, ScoreRangeHigh: scoreHigh,
		SegmentCount: int(row.SegmentCount), CumulativeCount: cumulative,
		RankRangeLow: rankLow, RankRangeHigh: rankHigh,
	}
}

// parseRange 解析 "low-high" 区间字符串；无 '-' 视为单值（low==high）。
// 解析失败时对应端回退到 defLow/defHigh（防御脏数据，不让单行毁掉整表）。
func parseRange(s string, defLow, defHigh int) (low, high int) {
	parts := strings.Split(s, "-")
	if len(parts) > 2 {
		return defLow, defHigh
	}
	lo, err := strconv.Atoi(strings.TrimSpace(parts[0]))
	if err != nil || lo < 0 {
		return defLow, defHigh
	}
	hi := lo
	if len(parts) == 2 {
		hi, err = strconv.Atoi(strings.TrimSpace(parts[1]))
		if err != nil || hi < lo {
			return defLow, defHigh
		}
	}
	return lo, hi
}
