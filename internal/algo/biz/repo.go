package biz

import "context"

// —— biz/repo.go：biz 定义接口、data 实现（依赖倒置，wire 注入）——

// AdmissionRepo 录取/计划数据访问（data 层实现，封装所有 SQL）。
type AdmissionRepo interface {
	// LoadCandidates year=报考年：候选池=报考年 enrollment_plan 覆盖的组，统计取 year-1（§3.0/§3.2）。
	LoadCandidates(ctx context.Context, releaseID, province, category, batch string, year int) ([]Candidate, error)
	// BatchGroupHistory 窗口=[year-windowYears, year-1]（含上下界，绝不含报考年）。
	// 数据契约：key=GroupKey，value 按年份降序；每条只填 GroupMinRank+TotalCount，MinPercentile 留空由 biz 计算。
	BatchGroupHistory(ctx context.Context, releaseID, province, category, batch string, year, windowYears int) (map[GroupKey][]AdmissionRecord, error)
	// BatchMajorHistory 仅按映射历史组内的同专业代码读取专业级历史，按年份降序返回。
	BatchMajorHistory(ctx context.Context, releaseID, province, category, batch string, year, windowYears int) (map[MajorKey][]MajorAdmissionRecord, error)
	// BatchGroupPlan year=报考年, prevYear=报考年-1；按 GroupKey 组装 PlanChange{PlanCount:报考年, PrevPlanCount:报考年-1}。
	BatchGroupPlan(ctx context.Context, releaseID, province, category string, year, prevYear int) (map[GroupKey]PlanChange, error)
}

// RankRepo 一分一段表访问（data 层实现）。
type RankRepo interface {
	// LoadScoreRankMap 查【完整】一分一段表（不按 batch 过滤：位次为全省总排位、与批次无关；
	// 表内最低分 cumulative=全省总人数，供 RankToPercentile 分母）+ controlScore（本科线，标量，供 MVP 批次校验）。
	// 报考年数据缺失应返回 err（DY5，不静默回退）。
	LoadScoreRankMap(ctx context.Context, releaseID, province, category string, year int) (table []ScoreRankEntry, controlScore int, err error)
}
