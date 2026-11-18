package data

import (
	"github.com/choucisan/ai-gaokao-counselor/internal/algo/biz"
	"github.com/choucisan/ai-gaokao-counselor/internal/algo/data/dbgen"
)

// assembleCandidates 将组骨架与专业明细合并；只遍历有序行，不用 map 决定输出顺序。
func assembleCandidates(groups []dbgen.ListCandidateGroupsRow, majors []dbgen.ListCandidateMajorsRow) []biz.Candidate {
	candidates := make([]biz.Candidate, 0, len(groups))
	index := make(map[biz.GroupKey]int, len(groups))
	for _, row := range groups {
		key := biz.GroupKey{UniversityCode: row.UniversityCode, MajorGroup: row.MajorGroup}
		index[key] = len(candidates)
		candidates = append(candidates, candidateFromRow(row))
	}
	attachMajors(candidates, index, majors)
	return candidates
}

// candidateFromRow 只做持久化类型到领域类型的映射，学校层次沿用 biz 的统一规则。
func candidateFromRow(row dbgen.ListCandidateGroupsRow) biz.Candidate {
	return biz.Candidate{
		// 表字段可空，但组查询的非空检查和 INNER JOIN 已保证这里有院校 ID。
		UniversityID:   int(row.UniversityID.Int64),
		UniversityCode: row.UniversityCode,
		UniversityName: row.UniversityName,
		MajorGroup:     row.MajorGroup,
		GroupMinRank:   int(row.GroupMinRank),
		SchoolTier:     biz.DeriveSchoolTier(row.Is985, row.Is211, row.IsDualClass),
		Location:       row.City,
		Province:       row.Province,
		NumMaster:      int(row.NumMaster),
		NumDoctor:      int(row.NumDoctor),
		RuankeRank:     int(row.RuankeRank),
	}
}

// attachMajors 只给已通过组筛选的候选挂明细，选科要求按首次出现顺序去重。
func attachMajors(candidates []biz.Candidate, index map[biz.GroupKey]int, rows []dbgen.ListCandidateMajorsRow) {
	subjectsSeen := make(map[biz.GroupKey]map[string]bool)
	for _, row := range rows {
		key := biz.GroupKey{UniversityCode: row.UniversityCode, MajorGroup: row.MajorGroup}
		idx, ok := index[key]
		if !ok {
			continue // 身份冲突等原因被组查询排除的明细，不重新加入候选池。
		}
		candidate := &candidates[idx]
		candidate.Majors = append(candidate.Majors, majorFromRow(row))
		if row.SubjectReq == "" {
			continue
		}
		if subjectsSeen[key] == nil {
			subjectsSeen[key] = make(map[string]bool)
		}
		if !subjectsSeen[key][row.SubjectReq] {
			subjectsSeen[key][row.SubjectReq] = true
			candidate.SubjectReq = append(candidate.SubjectReq, row.SubjectReq)
		}
	}
}

// majorFromRow 保留 SQL 已处理的空值兜底，不用专业级位次参与组概率计算。
func majorFromRow(row dbgen.ListCandidateMajorsRow) biz.MajorDetail {
	return biz.MajorDetail{
		MajorCode:  row.MajorCode,
		MajorName:  row.MajorName,
		SubjectReq: row.SubjectReq,
		Duration:   row.Duration,
		Tuition:    row.Tuition,
		MajorNote:  row.MajorNote,
		MinScore:   int(row.MinScore),
		MinRank:    int(row.MinRank),
		AdmitCount: int(row.AdmitCount),
	}
}

// groupHistoryRows 按完整组键归集；查询已按年份降序，直接追加即可保序。
func groupHistoryRows(rows []dbgen.ListGroupHistoryRow) map[biz.GroupKey][]biz.AdmissionRecord {
	history := make(map[biz.GroupKey][]biz.AdmissionRecord)
	for _, row := range rows {
		key := biz.GroupKey{UniversityCode: row.UniversityCode, MajorGroup: row.MajorGroup}
		history[key] = append(history[key], biz.AdmissionRecord{
			Year:         int(row.Year),
			GroupMinRank: int(row.GroupMinRank),
			TotalCount:   int(row.TotalCount),
		})
	}
	return history
}

// groupPlanRows 将两年的行折叠成一组计划变化；缺失年份保持零值。
func groupPlanRows(rows []dbgen.ListGroupPlansRow, year, prevYear int) map[biz.GroupKey]biz.PlanChange {
	plans := make(map[biz.GroupKey]biz.PlanChange)
	for _, row := range rows {
		key := biz.GroupKey{UniversityCode: row.UniversityCode, MajorGroup: row.MajorGroup}
		plan := plans[key]
		switch int(row.Year) {
		case year:
			plan.PlanCount = int(row.PlanCount)
		case prevYear:
			plan.PrevPlanCount = int(row.PlanCount)
		}
		plans[key] = plan
	}
	return plans
}
