package service

import (
	algov1 "github.com/choucisan/ai-gaokao-counselor/api/algo/v1"
	"github.com/choucisan/ai-gaokao-counselor/internal/algo/biz"
)

func fromPbEligibilityRequest(req *algov1.EvaluateEligibilityRequest) biz.EligibilityRequest {
	if req == nil {
		return biz.EligibilityRequest{}
	}
	out := biz.EligibilityRequest{
		ReleaseID:               req.GetReleaseId(),
		ConfirmedProfileVersion: req.GetConfirmedProfileVersion(),
		ProfileLifecycle:        fromPbProfileLifecycle(req.GetProfileLifecycle()),
		ProfileSnapshotSHA256:   req.GetProfileSnapshotSha256(),
		Scope:                   fromPbEligibilityScope(req.GetScope()),
	}
	for _, fact := range req.GetProfileFacts() {
		out.ProfileFacts = append(out.ProfileFacts, biz.ProfileFact{
			FieldKey:     fact.GetFieldKey(),
			State:        fromPbProfileFieldState(fact.GetState()),
			StringValue:  fact.GetStringValue(),
			IntValue:     int(fact.GetIntValue()),
			StringValues: append([]string(nil), fact.GetStringValues()...),
		})
	}
	for _, group := range req.GetCandidateGroups() {
		bg := biz.CandidateGroup{
			UniversityID:   int(group.GetUniversityId()),
			UniversityCode: group.GetUniversityCode(),
			UniversityName: group.GetUniversityName(),
			MajorGroup:     group.GetMajorGroup(),
		}
		for _, major := range group.GetMajors() {
			bg.Majors = append(bg.Majors, biz.CandidateMajor{
				MajorCode:          major.GetMajorCode(),
				MajorName:          major.GetMajorName(),
				SubjectRequirement: major.GetSubjectRequirement(),
				InCurrentPlan:      major.GetInCurrentPlan(),
			})
		}
		out.CandidateGroups = append(out.CandidateGroups, bg)
	}
	return out
}

func toPbEligibilityReply(result biz.EligibilityResult) *algov1.EvaluateEligibilityReply {
	reply := &algov1.EvaluateEligibilityReply{
		Status:           toPbEligibilityResultStatus(result.Status),
		EvidenceBundleId: result.EvidenceBundleID,
		Diagnostics: &algov1.EligibilityDiagnostics{
			EvaluatedRuleCount: int32(result.Diagnostics.EvaluatedRuleCount),
			ConflictCount:      int32(result.Diagnostics.ConflictCount),
			UnknownCount:       int32(result.Diagnostics.UnknownCount),
		},
	}
	for _, coverage := range result.Coverage {
		reply.Coverage = append(reply.Coverage, &algov1.EligibilityRuleCoverage{
			RuleType:           coverage.RuleType,
			Scope:              toPbEligibilityScope(coverage.Scope),
			Status:             toPbCoverageStatus(coverage.Status),
			CoveredDataSources: append([]string(nil), coverage.CoveredDataSources...),
			Notes:              coverage.Notes,
		})
	}
	for _, group := range result.Groups {
		pg := &algov1.GroupEligibility{
			GroupKey:     group.GroupKey,
			Status:       toPbGroupStatus(group.GroupStatus),
			EvidenceRefs: toPbEligibilityEvidenceRefs(group.EvidenceRefs),
		}
		for _, major := range group.Majors {
			pg.Majors = append(pg.Majors, &algov1.MajorEligibility{
				MajorCode:       major.MajorCode,
				Status:          toPbMajorStatus(major.Status),
				FailureRuleKeys: append([]string(nil), major.FailureRuleKeys...),
				UnknownReasons:  append([]string(nil), major.UnknownReasons...),
				MissingFields:   append([]string(nil), major.MissingFields...),
				EvidenceRefs:    toPbEligibilityEvidenceRefs(major.EvidenceRefs),
			})
		}
		reply.Groups = append(reply.Groups, pg)
	}
	return reply
}

func fromPbEligibilityScope(scope *algov1.EligibilityScope) biz.EligibilityScope {
	if scope == nil {
		return biz.EligibilityScope{}
	}
	return biz.EligibilityScope{
		Province:        scope.GetProvince(),
		ApplicationYear: int(scope.GetApplicationYear()),
		Category:        scope.GetCategory(),
		Batch:           scope.GetBatch(),
		UniversityID:    int(scope.GetUniversityId()),
		MajorGroup:      scope.GetMajorGroup(),
		MajorCode:       scope.GetMajorCode(),
	}
}

func toPbEligibilityScope(scope biz.RuleScope) *algov1.EligibilityScope {
	return &algov1.EligibilityScope{
		Province:        scope.Province,
		ApplicationYear: int32(scope.ApplicationYear),
		Category:        scope.Category,
		Batch:           scope.Batch,
		UniversityId:    int64(scope.UniversityID),
		MajorGroup:      scope.MajorGroup,
		MajorCode:       scope.MajorCode,
	}
}

func fromPbProfileLifecycle(value algov1.EligibilityProfileLifecycle) string {
	switch value {
	case algov1.EligibilityProfileLifecycle_ELIGIBILITY_PROFILE_LIFECYCLE_CONFIRMED:
		return biz.ProfileLifecycleConfirmed
	default:
		return ""
	}
}

func fromPbProfileFieldState(value algov1.EligibilityProfileFieldState) string {
	switch value {
	case algov1.EligibilityProfileFieldState_ELIGIBILITY_PROFILE_FIELD_STATE_PROVIDED:
		return biz.ProfileProvided
	case algov1.EligibilityProfileFieldState_ELIGIBILITY_PROFILE_FIELD_STATE_UNKNOWN:
		return biz.ProfileUnknown
	case algov1.EligibilityProfileFieldState_ELIGIBILITY_PROFILE_FIELD_STATE_DECLINED:
		return biz.ProfileDeclined
	case algov1.EligibilityProfileFieldState_ELIGIBILITY_PROFILE_FIELD_STATE_DERIVED:
		return biz.ProfileDerived
	case algov1.EligibilityProfileFieldState_ELIGIBILITY_PROFILE_FIELD_STATE_DEFAULT:
		return biz.ProfileDefault
	default:
		return ""
	}
}

func toPbEligibilityResultStatus(value string) algov1.EligibilityResultStatus {
	switch value {
	case biz.EligibilityComplete:
		return algov1.EligibilityResultStatus_ELIGIBILITY_RESULT_STATUS_COMPLETE
	case biz.EligibilityPartial:
		return algov1.EligibilityResultStatus_ELIGIBILITY_RESULT_STATUS_PARTIAL
	case biz.EligibilityBlocked:
		return algov1.EligibilityResultStatus_ELIGIBILITY_RESULT_STATUS_BLOCKED
	default:
		return algov1.EligibilityResultStatus_ELIGIBILITY_RESULT_STATUS_UNSPECIFIED
	}
}

func toPbMajorStatus(value string) algov1.MajorEligibilityStatus {
	switch value {
	case biz.MajorPass:
		return algov1.MajorEligibilityStatus_MAJOR_ELIGIBILITY_STATUS_PASS
	case biz.MajorFail:
		return algov1.MajorEligibilityStatus_MAJOR_ELIGIBILITY_STATUS_FAIL
	case biz.MajorUnknown:
		return algov1.MajorEligibilityStatus_MAJOR_ELIGIBILITY_STATUS_UNKNOWN
	case biz.MajorNotApplicable:
		return algov1.MajorEligibilityStatus_MAJOR_ELIGIBILITY_STATUS_NOT_APPLICABLE
	case biz.MajorMissingRequiredFact:
		return algov1.MajorEligibilityStatus_MAJOR_ELIGIBILITY_STATUS_MISSING_REQUIRED_FACT
	default:
		return algov1.MajorEligibilityStatus_MAJOR_ELIGIBILITY_STATUS_UNSPECIFIED
	}
}

func toPbGroupStatus(value string) algov1.GroupEligibilityStatus {
	switch value {
	case biz.GroupPass:
		return algov1.GroupEligibilityStatus_GROUP_ELIGIBILITY_STATUS_PASS
	case biz.GroupPassPartial:
		return algov1.GroupEligibilityStatus_GROUP_ELIGIBILITY_STATUS_PASS_PARTIAL
	case biz.GroupFail:
		return algov1.GroupEligibilityStatus_GROUP_ELIGIBILITY_STATUS_FAIL
	case biz.GroupUnknown:
		return algov1.GroupEligibilityStatus_GROUP_ELIGIBILITY_STATUS_UNKNOWN
	default:
		return algov1.GroupEligibilityStatus_GROUP_ELIGIBILITY_STATUS_UNSPECIFIED
	}
}

func toPbCoverageStatus(value string) algov1.RuleCoverageStatus {
	switch value {
	case biz.CoverageFull:
		return algov1.RuleCoverageStatus_RULE_COVERAGE_STATUS_FULL
	case biz.CoveragePartial:
		return algov1.RuleCoverageStatus_RULE_COVERAGE_STATUS_PARTIAL
	case biz.CoverageNone:
		return algov1.RuleCoverageStatus_RULE_COVERAGE_STATUS_NONE
	default:
		return algov1.RuleCoverageStatus_RULE_COVERAGE_STATUS_UNSPECIFIED
	}
}

func toPbEligibilityEvidenceRefs(refs []biz.EvidenceRef) []*algov1.EligibilityEvidenceRef {
	if len(refs) == 0 {
		return nil
	}
	out := make([]*algov1.EligibilityEvidenceRef, 0, len(refs))
	for _, ref := range refs {
		out = append(out, &algov1.EligibilityEvidenceRef{
			EvidenceId:    ref.EvidenceID,
			EvidenceType:  ref.EvidenceType,
			ReleaseId:     ref.ReleaseID,
			ContentSha256: ref.ContentSHA,
		})
	}
	return out
}
