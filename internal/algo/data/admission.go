package data

import (
	"context"

	"github.com/choucisan/ai-gaokao-counselor/internal/algo/biz"
	"github.com/choucisan/ai-gaokao-counselor/internal/algo/data/dbgen"
	errs "github.com/choucisan/ai-gaokao-counselor/internal/pkg/errors"
)

type admissionRepo struct {
	d *Data
}

// NewAdmissionRepo 返回 biz.AdmissionRepo 实现（wire 注入）。
func NewAdmissionRepo(d *Data) biz.AdmissionRepo {
	return &admissionRepo{d: d}
}

// LoadCandidates 先确认报考年有计划，再读取上一年的组骨架与专业明细，最后按组装配。
// 查询条件见 queries/candidates.sql；结构体转换与保序合并见 admission_mapping.go。
func (r *admissionRepo) LoadCandidates(ctx context.Context, releaseID, province, category, batch string, year int) ([]biz.Candidate, error) {
	if err := r.ensureEnrollmentPlan(ctx, releaseID, province, category, batch, year); err != nil {
		return nil, err
	}
	groups, err := r.d.queries.ListCandidateGroups(ctx, dbgen.ListCandidateGroupsParams{
		ReleaseID: releaseID,
		Province:  province, Category: category, Batch: batch,
		HistoryYear: int32(year - 1), ReportYear: int32(year),
	})
	if err != nil {
		return nil, err
	}
	majors, err := r.d.queries.ListCandidateMajors(ctx, dbgen.ListCandidateMajorsParams{
		ReleaseID: releaseID,
		Province:  province, Category: category, Batch: batch,
		HistoryYear: int32(year - 1), ReportYear: int32(year),
	})
	if err != nil {
		return nil, err
	}
	return assembleCandidates(groups, majors), nil
}

// ensureEnrollmentPlan 区分“整批计划未导入”和“有计划但没有匹配候选”。
func (r *admissionRepo) ensureEnrollmentPlan(ctx context.Context, releaseID, province, category, batch string, year int) error {
	hasPlan, err := r.d.queries.HasEnrollmentPlan(ctx, dbgen.HasEnrollmentPlanParams{
		ReleaseID: releaseID,
		Province:  province, Category: category, Batch: batch, ReportYear: int32(year),
	})
	if err != nil {
		return err
	}
	if !hasPlan {
		return errs.ErrMissingEnrollmentPlan
	}
	return nil
}

// BatchGroupHistory 一次读取 [year-windowYears, year-1] 的历史，按组归集并保持年份降序。
// 总人数取完整一分一段表；百分位交给 biz 计算，不使用报考年的录取结果。
func (r *admissionRepo) BatchGroupHistory(ctx context.Context, releaseID, province, category, batch string, year, windowYears int) (map[biz.GroupKey][]biz.AdmissionRecord, error) {
	rows, err := r.d.queries.ListGroupHistory(ctx, dbgen.ListGroupHistoryParams{
		ReleaseID: releaseID,
		Province:  province, Category: category, Batch: batch,
		LowerYear: int32(year - windowYears), UpperYear: int32(year - 1),
	})
	if err != nil {
		return nil, err
	}
	return groupHistoryRows(rows), nil
}

// BatchGroupPlan 一次读取两年计划，分别填入当前与上一年计划数，不逐组查库。
func (r *admissionRepo) BatchGroupPlan(ctx context.Context, releaseID, province, category string, year, prevYear int) (map[biz.GroupKey]biz.PlanChange, error) {
	rows, err := r.d.queries.ListGroupPlans(ctx, dbgen.ListGroupPlansParams{
		ReleaseID: releaseID,
		Province:  province, Category: category,
		ReportYear: int32(year), PreviousYear: int32(prevYear),
	})
	if err != nil {
		return nil, err
	}
	return groupPlanRows(rows, year, prevYear), nil
}
