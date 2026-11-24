package data

import (
	"reflect"
	"testing"

	"github.com/choucisan/ai-gaokao-counselor/internal/algo/biz"
	"github.com/choucisan/ai-gaokao-counselor/internal/algo/data/dbgen"
)

func TestCandidateFromRow_AllFields(t *testing.T) {
	row := dbgen.ListCandidateGroupsRow{
		UniversityID:   123,
		UniversityCode: "A", UniversityName: "示例大学", MajorGroup: "101",
		GroupMinRank: 12000, Is985: true, Is211: true, IsDualClass: true,
		City: "长沙", Province: "湖南", NumMaster: 10, NumDoctor: 8, RuankeRank: 20,
	}
	want := biz.Candidate{
		UniversityID: 123, UniversityCode: "A", UniversityName: "示例大学", MajorGroup: "101",
		GroupMinRank: 12000, SchoolTier: "985", Location: "长沙", Province: "湖南",
		NumMaster: 10, NumDoctor: 8, RuankeRank: 20,
	}
	if got := candidateFromRow(row); !reflect.DeepEqual(got, want) {
		t.Fatalf("组字段映射错误: got=%+v want=%+v", got, want)
	}
}

func TestMajorFromRow_AllFields(t *testing.T) {
	planCount := 30
	row := dbgen.ListCandidateMajorsRow{
		MajorCode: "01", MajorName: "计算机", SubjectReq: "首选物理，再选化学",
		Duration: "四年", Tuition: 5200.5, MajorNote: "示例备注",
		MinScore: 600, MinRank: 10000, ReferenceYear: 2024,
		AdmitCount: 25, PlanCount: 30, InCurrentPlan: true,
	}
	want := biz.MajorDetail{
		MajorCode: "01", MajorName: "计算机", SubjectReq: "首选物理，再选化学",
		Duration: "四年", Tuition: 5200.5, HasTuition: true, MajorNote: "示例备注",
		MinScore: 600, MinRank: 10000, ReferenceYear: 2024,
		AdmitCount: 25, PlanCount: &planCount, Selectable: true,
		InCurrentPlan: true,
	}
	if got := majorFromRow(row); !reflect.DeepEqual(got, want) {
		t.Fatalf("专业字段映射错误: %+v", got)
	}
	empty := majorFromRow(dbgen.ListCandidateMajorsRow{})
	if empty.HasTuition || empty.ReferenceYear != 0 || empty.PlanCount == nil || *empty.PlanCount != 0 {
		t.Fatalf("空行三态映射错误: %+v", empty)
	}
}

func TestAssembleCandidates_GroupKeyOrderAndSubjects(t *testing.T) {
	groups := []dbgen.ListCandidateGroupsRow{
		{UniversityCode: "A", UniversityName: "同名大学", MajorGroup: "101"},
		{UniversityCode: "A", UniversityName: "同名大学", MajorGroup: "102"},
		{UniversityCode: "B", UniversityName: "同名大学", MajorGroup: "101"},
	}
	majors := []dbgen.ListCandidateMajorsRow{
		{UniversityCode: "A", MajorGroup: "101", MajorCode: "21", SubjectReq: "化学"},
		{UniversityCode: "A", MajorGroup: "101", MajorCode: "24", SubjectReq: "化学"},
		{UniversityCode: "A", MajorGroup: "101", MajorCode: "25", SubjectReq: "生物"},
		{UniversityCode: "A", MajorGroup: "101", MajorCode: "26"},
		{UniversityCode: "A", MajorGroup: "101", MajorCode: "", SubjectReq: "不能混入"},
		{UniversityCode: "A", MajorGroup: "101", MajorCode: "   ", SubjectReq: "不能混入"},
		{UniversityCode: "A", MajorGroup: "102", MajorCode: "01", SubjectReq: "化学"},
		{UniversityCode: "X", MajorGroup: "101", MajorCode: "99", SubjectReq: "不能混入"},
	}
	got := assembleCandidates(groups, majors)
	if len(got) != 3 || got[0].MajorGroup != "101" || got[1].MajorGroup != "102" || got[2].UniversityCode != "B" {
		t.Fatalf("组键或次序丢失: %+v", got)
	}
	var codes []string
	for _, major := range got[0].Majors {
		codes = append(codes, major.MajorCode)
	}
	if !reflect.DeepEqual(codes, []string{"21", "24", "25", "26"}) {
		t.Fatalf("专业顺序改变: %v", codes)
	}
	if !reflect.DeepEqual(got[0].SubjectReq, []string{"化学", "生物"}) {
		t.Fatalf("选科保序去重失败: %v", got[0].SubjectReq)
	}
	if len(got[1].Majors) != 1 || !reflect.DeepEqual(got[1].SubjectReq, []string{"化学"}) {
		t.Fatal("同校不同组串数据")
	}
	if got[2].Majors != nil || got[2].SubjectReq != nil {
		t.Fatal("无明细的组应保留 nil 列表")
	}
	if empty := assembleCandidates(nil, majors); empty == nil || len(empty) != 0 {
		t.Fatal("无组时应返回非 nil 空候选列表，不允许仅有明细的组混入")
	}
}

func TestGroupHistoryRows_YearOrderAndOriginalValues(t *testing.T) {
	rows := []dbgen.ListGroupHistoryRow{
		{UniversityCode: "A", MajorGroup: "101", Year: 2024, GroupMinScore: 620, GroupMinRank: 100, TotalCount: 1000, MappingMethod: "EXACT_CODE", MappingConfidence: 1},
		{UniversityCode: "A", MajorGroup: "101", Year: 2022, GroupMinScore: 610, GroupMinRank: 200, TotalCount: 2000, MappingMethod: "MAJOR_NAME_JACCARD", MappingConfidence: 0.75},
		{UniversityCode: "A", MajorGroup: "102", Year: 2023, GroupMinScore: 600, GroupMinRank: 300, TotalCount: 3000, MappingMethod: "EXACT_CODE", MappingConfidence: 1},
	}
	want := map[biz.GroupKey][]biz.AdmissionRecord{
		{UniversityCode: "A", MajorGroup: "101"}: {{Year: 2024, GroupMinScore: 620, GroupMinRank: 100, TotalCount: 1000, MappingMethod: "EXACT_CODE", MappingConfidence: 1}, {Year: 2022, GroupMinScore: 610, GroupMinRank: 200, TotalCount: 2000, MappingMethod: "MAJOR_NAME_JACCARD", MappingConfidence: 0.75}},
		{UniversityCode: "A", MajorGroup: "102"}: {{Year: 2023, GroupMinScore: 600, GroupMinRank: 300, TotalCount: 3000, MappingMethod: "EXACT_CODE", MappingConfidence: 1}},
	}
	if got := groupHistoryRows(rows); !reflect.DeepEqual(got, want) {
		t.Fatalf("历史分组映射错误: %+v", got)
	}
	if empty := groupHistoryRows(nil); empty == nil || len(empty) != 0 {
		t.Fatal("空历史应返回非 nil 空 map")
	}
}

func TestGroupPlanRows_MissingYearAndSameYear(t *testing.T) {
	rows := []dbgen.ListGroupPlansRow{
		{UniversityCode: "A", MajorGroup: "101", Year: 2024, PlanCount: 10},
		{UniversityCode: "A", MajorGroup: "101", Year: 2025, PlanCount: 20},
		{UniversityCode: "A", MajorGroup: "102", Year: 2025, PlanCount: 5},
		{UniversityCode: "B", MajorGroup: "101", Year: 2024, PlanCount: 8},
	}
	want := map[biz.GroupKey]biz.PlanChange{
		{UniversityCode: "A", MajorGroup: "101"}: {PlanCount: 20, PrevPlanCount: 10},
		{UniversityCode: "A", MajorGroup: "102"}: {PlanCount: 5},
		{UniversityCode: "B", MajorGroup: "101"}: {PrevPlanCount: 8},
	}
	if got := groupPlanRows(rows, 2025, 2024); !reflect.DeepEqual(got, want) {
		t.Fatalf("两年计划映射错误: %+v", got)
	}
	same := groupPlanRows(rows[:1], 2024, 2024)[biz.GroupKey{UniversityCode: "A", MajorGroup: "101"}]
	if same.PlanCount != 10 || same.PrevPlanCount != 0 {
		t.Fatal("同年时应保持原 switch 的当前年优先语义")
	}
	if empty := groupPlanRows(nil, 2025, 2024); empty == nil || len(empty) != 0 {
		t.Fatal("空计划应返回非 nil 空 map")
	}
}

func TestScoreRankTable_AllBatchesAndFirstControl(t *testing.T) {
	rows := []dbgen.ListScoreRanksRow{
		{Score: 693, ScoreRange: "693-750", SegmentCount: 51, CumulativeCount: 51, RankRange: "1-51", Batch: "本科批", ControlScore: 405},
		{Score: 600, CumulativeCount: 15860, Batch: "本科批", ControlScore: 406},
		{Score: 200, CumulativeCount: 300000, Batch: "专科批", ControlScore: 200},
	}
	table, control := scoreRankTable(rows)
	if len(table) != 3 || control != 405 || table[2].CumulativeCount != 300000 {
		t.Fatal("完整表或本科线口径改变")
	}
	want := biz.ScoreRankEntry{Score: 693, ScoreRangeLow: 693, ScoreRangeHigh: 750, SegmentCount: 51, CumulativeCount: 51, RankRangeLow: 1, RankRangeHigh: 51}
	if table[0] != want {
		t.Fatalf("区间映射错误: %+v", table[0])
	}
	if table[1].ScoreRangeLow != 600 || table[1].RankRangeLow != 15860 {
		t.Fatal("空区间未回退单值")
	}
	bad := scoreRankEntryFromRow(dbgen.ListScoreRanksRow{Score: 450, ScoreRange: "坏数据", CumulativeCount: 123, RankRange: "2-1"})
	if bad.ScoreRangeLow != 450 || bad.ScoreRangeHigh != 450 || bad.RankRangeLow != 123 || bad.RankRangeHigh != 123 {
		t.Fatalf("坏区间回退错误: %+v", bad)
	}
	_, control = scoreRankTable(rows[2:])
	if control != 0 {
		t.Fatal("没有本科批时不能取专科线")
	}
}
