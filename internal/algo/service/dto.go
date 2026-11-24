package service

import (
	algov1 "github.com/choucisan/ai-gaokao-counselor/api/algo/v1"
	"github.com/choucisan/ai-gaokao-counselor/internal/algo/biz"
)

// 本文件集中 biz↔pb 映射（toPb*/fromPb*，每类型一对，H5/Q35 归 service 层）。
// 映射是纯机械转换（无业务逻辑），可单测：fromPb(toPb(x)) == x（round-trip 保真）。
// biz 层只认 §2 struct，签名里不出现 pb（§4.3）。
//
// nil/空一致约定（保证 round-trip 保真）：slice/map 仅在非空时分配，空输入 → nil 输出；
// 故 toPb(nil slice) → nil pb slice，fromPb(nil/空 pb slice) → nil biz slice。
// 形状差异（§4.3）：biz 用 Go 嵌入扁平化（cwp.Probability 直达），pb 用嵌套（cwp.Prob.Probability）。

// ======== 标量转换 ========

func i32(v int) int32 { return int32(v) }

func toInt(v int32) int { return int(v) }

// ======== MajorDetail ========

func toPbMajorDetail(m biz.MajorDetail) *algov1.MajorDetail {
	pb := &algov1.MajorDetail{
		MajorCode:            m.MajorCode,
		MajorName:            m.MajorName,
		SubjectReq:           m.SubjectReq,
		Duration:             m.Duration,
		Tuition:              m.Tuition,
		HasTuition:           m.HasTuition,
		MajorNote:            m.MajorNote,
		MinScore:             i32(m.MinScore),
		MinRank:              i32(m.MinRank),
		AdmitCount:           i32(m.AdmitCount),
		Recommended:          m.Recommended,
		RecommendationRank:   i32(m.RecommendationRank),
		ProbabilityDataYears: i32(m.ProbabilityDataYears),
		Selectable:           m.Selectable,
		UnavailableReason:    m.UnavailableReason,
		InCurrentPlan:        m.InCurrentPlan,
	}
	if len(m.ProbabilitySampleYears) > 0 {
		pb.ProbabilitySampleYears = make([]int32, 0, len(m.ProbabilitySampleYears))
		for _, year := range m.ProbabilitySampleYears {
			pb.ProbabilitySampleYears = append(pb.ProbabilitySampleYears, i32(year))
		}
	}
	if m.PlanCount != nil {
		value := i32(*m.PlanCount)
		pb.PlanCount = &value
	}
	if m.AdmissionProbability != nil {
		value := *m.AdmissionProbability
		pb.AdmissionProbability = &value
	}
	if m.ReferenceYear > 0 {
		value := i32(m.ReferenceYear)
		pb.ReferenceYear = &value
	}
	return pb
}

func fromPbMajorDetail(m *algov1.MajorDetail) biz.MajorDetail {
	if m == nil {
		return biz.MajorDetail{}
	}
	result := biz.MajorDetail{
		MajorCode:            m.GetMajorCode(),
		MajorName:            m.GetMajorName(),
		SubjectReq:           m.GetSubjectReq(),
		Duration:             m.GetDuration(),
		Tuition:              m.GetTuition(),
		HasTuition:           m.GetHasTuition(),
		MajorNote:            m.GetMajorNote(),
		MinScore:             toInt(m.GetMinScore()),
		MinRank:              toInt(m.GetMinRank()),
		AdmitCount:           toInt(m.GetAdmitCount()),
		Recommended:          m.GetRecommended(),
		RecommendationRank:   toInt(m.GetRecommendationRank()),
		ProbabilityDataYears: toInt(m.GetProbabilityDataYears()),
		Selectable:           m.GetSelectable(),
		UnavailableReason:    m.GetUnavailableReason(),
		InCurrentPlan:        m.GetInCurrentPlan(),
	}
	if len(m.GetProbabilitySampleYears()) > 0 {
		result.ProbabilitySampleYears = make(
			[]int,
			0,
			len(m.GetProbabilitySampleYears()),
		)
		for _, year := range m.GetProbabilitySampleYears() {
			result.ProbabilitySampleYears = append(
				result.ProbabilitySampleYears,
				toInt(year),
			)
		}
	}
	if m.PlanCount != nil {
		value := toInt(*m.PlanCount)
		result.PlanCount = &value
	}
	if m.AdmissionProbability != nil {
		value := *m.AdmissionProbability
		result.AdmissionProbability = &value
	}
	if m.ReferenceYear != nil {
		result.ReferenceYear = toInt(*m.ReferenceYear)
	}
	return result
}

// ======== AdmissionRecord ========

func toPbAdmissionRecord(r biz.AdmissionRecord) *algov1.AdmissionRecord {
	return &algov1.AdmissionRecord{
		Year:              i32(r.Year),
		GroupMinScore:     i32(r.GroupMinScore),
		GroupMinRank:      i32(r.GroupMinRank),
		TotalCount:        i32(r.TotalCount),
		MinPercentile:     r.MinPercentile,
		EnrollCount:       i32(r.EnrollCount),
		MappingMethod:     r.MappingMethod,
		MappingConfidence: r.MappingConfidence,
	}
}

func fromPbAdmissionRecord(r *algov1.AdmissionRecord) biz.AdmissionRecord {
	if r == nil {
		return biz.AdmissionRecord{}
	}
	return biz.AdmissionRecord{
		Year:              toInt(r.GetYear()),
		GroupMinScore:     toInt(r.GetGroupMinScore()),
		GroupMinRank:      toInt(r.GetGroupMinRank()),
		TotalCount:        toInt(r.GetTotalCount()),
		MinPercentile:     r.GetMinPercentile(),
		EnrollCount:       toInt(r.GetEnrollCount()),
		MappingMethod:     r.GetMappingMethod(),
		MappingConfidence: r.GetMappingConfidence(),
	}
}

// ======== Candidate ========

func toPbCandidate(c biz.Candidate) *algov1.Candidate {
	pb := &algov1.Candidate{
		UniversityId:   i32(c.UniversityID),
		UniversityCode: c.UniversityCode,
		UniversityName: c.UniversityName,
		MajorGroup:     c.MajorGroup,
		GroupMinRank:   i32(c.GroupMinRank),
		SchoolTier:     c.SchoolTier,
		Location:       c.Location,
		Province:       c.Province,
		NumMaster:      i32(c.NumMaster),
		NumDoctor:      i32(c.NumDoctor),
		RuankeRank:     i32(c.RuankeRank),
	}
	if c.PlanCountKnown {
		planCount := i32(c.PlanCount)
		pb.PlanCount = &planCount
	}
	if c.PreviousPlanCountKnown {
		previousPlanCount := i32(c.PreviousPlanCount)
		pb.PreviousPlanCount = &previousPlanCount
	}
	if c.PlanAdjustFactor != nil {
		factor := *c.PlanAdjustFactor
		pb.PlanAdjustFactor = &factor
	}
	if len(c.Majors) > 0 {
		pb.Majors = make([]*algov1.MajorDetail, 0, len(c.Majors))
		for _, m := range c.Majors {
			pb.Majors = append(pb.Majors, toPbMajorDetail(m))
		}
	}
	if len(c.SubjectReq) > 0 {
		pb.SubjectReq = append([]string(nil), c.SubjectReq...)
	}
	if len(c.HistoryData) > 0 {
		pb.HistoryData = make([]*algov1.AdmissionRecord, 0, len(c.HistoryData))
		for _, r := range c.HistoryData {
			pb.HistoryData = append(pb.HistoryData, toPbAdmissionRecord(r))
		}
	}
	return pb
}

func fromPbCandidate(c *algov1.Candidate) biz.Candidate {
	if c == nil {
		return biz.Candidate{}
	}
	bc := biz.Candidate{
		UniversityID:   toInt(c.GetUniversityId()),
		UniversityCode: c.GetUniversityCode(),
		UniversityName: c.GetUniversityName(),
		MajorGroup:     c.GetMajorGroup(),
		GroupMinRank:   toInt(c.GetGroupMinRank()),
		SchoolTier:     c.GetSchoolTier(),
		Location:       c.GetLocation(),
		Province:       c.GetProvince(),
		NumMaster:      toInt(c.GetNumMaster()),
		NumDoctor:      toInt(c.GetNumDoctor()),
		RuankeRank:     toInt(c.GetRuankeRank()),
	}
	if c.PlanCount != nil {
		bc.PlanCount = toInt(c.GetPlanCount())
		bc.PlanCountKnown = true
	}
	if c.PreviousPlanCount != nil {
		bc.PreviousPlanCount = toInt(c.GetPreviousPlanCount())
		bc.PreviousPlanCountKnown = true
	}
	if c.PlanAdjustFactor != nil {
		factor := c.GetPlanAdjustFactor()
		bc.PlanAdjustFactor = &factor
	}
	if ms := c.GetMajors(); len(ms) > 0 {
		bc.Majors = make([]biz.MajorDetail, 0, len(ms))
		for _, m := range ms {
			bc.Majors = append(bc.Majors, fromPbMajorDetail(m))
		}
	}
	if sr := c.GetSubjectReq(); len(sr) > 0 {
		bc.SubjectReq = append([]string(nil), sr...)
	}
	if hd := c.GetHistoryData(); len(hd) > 0 {
		bc.HistoryData = make([]biz.AdmissionRecord, 0, len(hd))
		for _, r := range hd {
			bc.HistoryData = append(bc.HistoryData, fromPbAdmissionRecord(r))
		}
	}
	return bc
}

// ======== ProbEstimate ========

func toPbProbEstimate(p biz.ProbEstimate) *algov1.ProbEstimate {
	return &algov1.ProbEstimate{
		Probability: p.Probability,
		Method:      p.Method,
		DataYears:   i32(p.DataYears),
		Category:    p.Category,
	}
}

func fromPbProbEstimate(p *algov1.ProbEstimate) biz.ProbEstimate {
	if p == nil {
		return biz.ProbEstimate{}
	}
	return biz.ProbEstimate{
		Probability: p.GetProbability(),
		Method:      p.GetMethod(),
		DataYears:   toInt(p.GetDataYears()),
		Category:    p.GetCategory(),
	}
}

// ======== CandidateWithProb（扁平↔嵌套）========

func toPbCandidateWithProb(c biz.CandidateWithProb) *algov1.CandidateWithProb {
	return &algov1.CandidateWithProb{
		Candidate: toPbCandidate(c.Candidate),
		Prob:      toPbProbEstimate(c.ProbEstimate),
	}
}

func fromPbCandidateWithProb(c *algov1.CandidateWithProb) biz.CandidateWithProb {
	if c == nil {
		return biz.CandidateWithProb{}
	}
	return biz.CandidateWithProb{
		Candidate:    fromPbCandidate(c.GetCandidate()),
		ProbEstimate: fromPbProbEstimate(c.GetProb()),
	}
}

func toPbCandidateWithProbSlice(list []biz.CandidateWithProb) []*algov1.CandidateWithProb {
	if len(list) == 0 {
		return nil
	}
	out := make([]*algov1.CandidateWithProb, 0, len(list))
	for _, c := range list {
		out = append(out, toPbCandidateWithProb(c))
	}
	return out
}

func fromPbCandidateWithProbSlice(list []*algov1.CandidateWithProb) []biz.CandidateWithProb {
	if len(list) == 0 {
		return nil
	}
	out := make([]biz.CandidateWithProb, 0, len(list))
	for _, c := range list {
		out = append(out, fromPbCandidateWithProb(c))
	}
	return out
}

// ======== UtilityScore ========

func toPbUtilityScore(u biz.UtilityScore) *algov1.UtilityScore {
	pb := &algov1.UtilityScore{Score: u.Score}
	if len(u.Breakdown) > 0 {
		pb.Breakdown = make(map[string]float64, len(u.Breakdown))
		for k, v := range u.Breakdown {
			pb.Breakdown[k] = v
		}
	}
	if len(u.Weights) > 0 {
		pb.Weights = make(map[string]float64, len(u.Weights))
		for k, v := range u.Weights {
			pb.Weights[k] = v
		}
	}
	return pb
}

func fromPbUtilityScore(u *algov1.UtilityScore) biz.UtilityScore {
	if u == nil {
		return biz.UtilityScore{}
	}
	bu := biz.UtilityScore{Score: u.GetScore()}
	if b := u.GetBreakdown(); len(b) > 0 {
		bu.Breakdown = make(map[string]float64, len(b))
		for k, v := range b {
			bu.Breakdown[k] = v
		}
	}
	if w := u.GetWeights(); len(w) > 0 {
		bu.Weights = make(map[string]float64, len(w))
		for k, v := range w {
			bu.Weights[k] = v
		}
	}
	return bu
}

// ======== CandidateWithScore（三层嵌入↔三级嵌套）========

func toPbCandidateWithScore(c biz.CandidateWithScore) *algov1.CandidateWithScore {
	return &algov1.CandidateWithScore{
		Cwp:       toPbCandidateWithProb(c.CandidateWithProb),
		Utility:   toPbUtilityScore(c.UtilityScore),
		FinalRank: i32(c.FinalRank),
	}
}

func fromPbCandidateWithScore(c *algov1.CandidateWithScore) biz.CandidateWithScore {
	if c == nil {
		return biz.CandidateWithScore{}
	}
	return biz.CandidateWithScore{
		CandidateWithProb: fromPbCandidateWithProb(c.GetCwp()),
		UtilityScore:      fromPbUtilityScore(c.GetUtility()),
		FinalRank:         toInt(c.GetFinalRank()),
	}
}

// ======== UserProfile ========

func fromPbUserProfile(p *algov1.UserProfile) biz.UserProfile {
	if p == nil {
		return biz.UserProfile{}
	}
	return biz.UserProfile{
		RiskAppetite:          p.GetRiskAppetite(),
		PriorityDims:          copyStrSlice(p.GetPriorityDims()),
		Interests:             copyStrSlice(p.GetInterests()),
		PreferredCities:       copyStrSlice(p.GetPreferredCities()),
		PreferredTiers:        copyStrSlice(p.GetPreferredTiers()),
		RequiredProvinces:     copyStrSlice(p.GetRequiredProvinces()),
		ExcludedProvinces:     copyStrSlice(p.GetExcludedProvinces()),
		RequiredCities:        copyStrSlice(p.GetRequiredCities()),
		ExcludedCities:        copyStrSlice(p.GetExcludedCities()),
		RequiredTiers:         copyStrSlice(p.GetRequiredTiers()),
		RequiredMajorNames:    copyStrSlice(p.GetRequiredMajorNames()),
		RequiredMajorKeywords: copyStrSlice(p.GetRequiredMajorKeywords()),
		ExcludedMajorKeywords: copyStrSlice(p.GetExcludedMajorKeywords()),
		ExcludedMajorNames:    copyStrSlice(p.GetExcludedMajorNames()),
		ExcludedUniversities:  copyStrSlice(p.GetExcludedUniversities()),
	}
}

func toPbUserProfile(p biz.UserProfile) *algov1.UserProfile {
	return &algov1.UserProfile{
		RiskAppetite:          p.RiskAppetite,
		PriorityDims:          copyStrSlice(p.PriorityDims),
		Interests:             copyStrSlice(p.Interests),
		PreferredCities:       copyStrSlice(p.PreferredCities),
		PreferredTiers:        copyStrSlice(p.PreferredTiers),
		RequiredProvinces:     copyStrSlice(p.RequiredProvinces),
		ExcludedProvinces:     copyStrSlice(p.ExcludedProvinces),
		RequiredCities:        copyStrSlice(p.RequiredCities),
		ExcludedCities:        copyStrSlice(p.ExcludedCities),
		RequiredTiers:         copyStrSlice(p.RequiredTiers),
		RequiredMajorNames:    copyStrSlice(p.RequiredMajorNames),
		RequiredMajorKeywords: copyStrSlice(p.RequiredMajorKeywords),
		ExcludedMajorKeywords: copyStrSlice(p.ExcludedMajorKeywords),
		ExcludedMajorNames:    copyStrSlice(p.ExcludedMajorNames),
		ExcludedUniversities:  copyStrSlice(p.ExcludedUniversities),
	}
}

// copyStrSlice 拷贝字符串 slice（空 → nil，保证 round-trip nil 一致）。
func copyStrSlice(s []string) []string {
	if len(s) == 0 {
		return nil
	}
	return append([]string(nil), s...)
}

// ======== OptimizationReport / WishlistResult ========

func toPbOptimizationReport(r biz.OptimizationReport) *algov1.OptimizationReport {
	return &algov1.OptimizationReport{
		RushCount:            i32(r.RushCount),
		StableCount:          i32(r.StableCount),
		SafeCount:            i32(r.SafeCount),
		SafeGuardOk:          r.SafeGuardOK,
		SlipRate:             r.SlipRate,
		ExpectedSatisfaction: r.ExpectedSatisfaction,
	}
}

func toPbWishlistResult(w biz.WishlistResult) *algov1.WishlistResult {
	pb := &algov1.WishlistResult{Report: toPbOptimizationReport(w.Report)}
	if len(w.Candidates) > 0 {
		pb.Candidates = make([]*algov1.CandidateWithScore, 0, len(w.Candidates))
		for _, c := range w.Candidates {
			pb.Candidates = append(pb.Candidates, toPbCandidateWithScore(c))
		}
	}
	return pb
}

func fromPbDraftGroupRiskInputs(
	groups []*algov1.DraftGroupRiskInput,
) []biz.DraftGroupRiskInput {
	if len(groups) == 0 {
		return nil
	}
	result := make([]biz.DraftGroupRiskInput, 0, len(groups))
	for _, group := range groups {
		if group == nil {
			continue
		}
		item := biz.DraftGroupRiskInput{
			UniversityCode: group.GetUniversityCode(),
			MajorGroup:     group.GetMajorGroup(),
			Probability:    group.GetProbability(),
			Category:       group.GetCategory(),
		}
		if group.UtilityScore != nil {
			value := group.GetUtilityScore()
			item.UtilityScore = &value
		}
		result = append(result, item)
	}
	return result
}

func toPbWishlistDraftRisk(r biz.WishlistDraftRisk) *algov1.WishlistDraftRisk {
	pb := &algov1.WishlistDraftRisk{
		GroupCount:      i32(r.GroupCount),
		RushCount:       i32(r.RushCount),
		StableCount:     i32(r.StableCount),
		SafeTierCount:   i32(r.SafeTierCount),
		SafeGuardCount:  i32(r.SafeGuardCount),
		SafeGuardTarget: i32(r.SafeGuardTarget),
		SafeGuardOk:     r.SafeGuardOK,
		SlipRate:        r.SlipRate,
	}
	if r.ExpectedSatisfaction != nil {
		value := *r.ExpectedSatisfaction
		pb.ExpectedSatisfaction = &value
	}
	return pb
}

// ======== AuditBasis ========

func toPbAuditBasis(b biz.AuditBasis) *algov1.AuditBasis {
	pb := &algov1.AuditBasis{
		UserRank:       i32(b.UserRank),
		UserPercentile: b.UserPercentile,
		Method:         b.Method,
		Province:       b.Province,
		Category:       b.Category,
	}
	if len(b.SampleYears) > 0 {
		pb.SampleYears = make([]int32, 0, len(b.SampleYears))
		for _, y := range b.SampleYears {
			pb.SampleYears = append(pb.SampleYears, i32(y))
		}
	}
	return pb
}

// ======== RejectedCandidate（嵌入↔嵌套）========

func toPbRejectedCandidate(r biz.RejectedCandidate) *algov1.RejectedCandidate {
	return &algov1.RejectedCandidate{
		Candidate: toPbCandidateWithProb(r.CandidateWithProb),
		Rule:      r.Rule,
		Reason:    r.Reason,
	}
}

func toPbRejectedCandidateSlice(list []biz.RejectedCandidate) []*algov1.RejectedCandidate {
	if len(list) == 0 {
		return nil
	}
	out := make([]*algov1.RejectedCandidate, 0, len(list))
	for _, r := range list {
		out = append(out, toPbRejectedCandidate(r))
	}
	return out
}

// ======== Reply 组装 ========

// toPbRankConvertReply biz.RankResult → pb.RankConvertReply（薄契约：只回 rank+percentile，
// TotalCount/ControlScore 为内部派生量不外泄，G1）。
func toPbRankConvertReply(r biz.RankResult) *algov1.RankConvertReply {
	return &algov1.RankConvertReply{
		Rank:       int32(r.Rank),
		Percentile: r.Percentile,
	}
}

// toPbEstimateProbReply biz.RecommendResult → pb.EstimateProbReply（三档候选 + *_total + basis，§4.2②）。
func toPbEstimateProbReply(res biz.RecommendResult) *algov1.EstimateProbReply {
	return &algov1.EstimateProbReply{
		RushCandidates:   toPbCandidateWithProbSlice(res.RushCandidates),
		StableCandidates: toPbCandidateWithProbSlice(res.StableCandidates),
		SafeCandidates:   toPbCandidateWithProbSlice(res.SafeCandidates),
		RushTotal:        i32(res.RushTotal),
		StableTotal:      i32(res.StableTotal),
		SafeTotal:        i32(res.SafeTotal),
		Basis:            toPbAuditBasis(res.Basis),
	}
}

// toPbCheckConstraintsReply kept + rejected → pb.CheckConstraintsReply（§4.2③）。
func toPbCheckConstraintsReply(kept []biz.CandidateWithProb, rejected []biz.RejectedCandidate) *algov1.CheckConstraintsReply {
	return &algov1.CheckConstraintsReply{
		Kept:     toPbCandidateWithProbSlice(kept),
		Rejected: toPbRejectedCandidateSlice(rejected),
	}
}
