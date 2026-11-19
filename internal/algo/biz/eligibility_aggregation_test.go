package biz

import (
	"context"
	"reflect"
	"testing"
)

func TestEvaluateEligibility_EmptyCandidateGroupIsBlocked(t *testing.T) {
	req := eligibilityFixtureRequest()
	req.CandidateGroups[0].Majors = nil

	result, err := NewEligibilityUsecase(fakeEligibilityRepo{
		set: eligibilityFixtureRuleSet(),
	}).
		EvaluateEligibility(context.Background(), req)
	if err != nil {
		t.Fatalf("EvaluateEligibility: %v", err)
	}
	if result.Status != EligibilityBlocked {
		t.Fatalf("status = %q, want BLOCKED", result.Status)
	}
	if len(result.Groups) != 1 || result.Groups[0].GroupStatus != GroupUnknown {
		t.Fatalf("groups = %+v, want one UNKNOWN group", result.Groups)
	}
	if result.Diagnostics.UnknownCount == 0 {
		t.Fatal("empty group must be counted as unknown")
	}
}

func TestEvaluateEligibility_DuplicateMajorIdentityIsBlocked(t *testing.T) {
	req := eligibilityFixtureRequest()
	req.CandidateGroups[0].Majors = append(
		req.CandidateGroups[0].Majors,
		req.CandidateGroups[0].Majors[0],
	)

	result, err := NewEligibilityUsecase(fakeEligibilityRepo{
		set: eligibilityFixtureRuleSet(),
	}).
		EvaluateEligibility(context.Background(), req)
	if err != nil {
		t.Fatalf("EvaluateEligibility: %v", err)
	}
	if result.Status != EligibilityBlocked {
		t.Fatalf("status = %q, want BLOCKED", result.Status)
	}
	majors := result.Groups[0].Majors
	if len(majors) != 2 || majors[0].Status != MajorUnknown || majors[1].Status != MajorUnknown {
		t.Fatalf("majors = %+v, want duplicate unknowns", majors)
	}
	if got := majors[0].UnknownReasons; len(got) != 1 || got[0] != ReasonCandidateMajorDuplicate {
		t.Fatalf("unknown reasons = %v, want [%s]", got, ReasonCandidateMajorDuplicate)
	}
}

func TestEvaluateEligibility_IncompleteScopeIsBlocked(t *testing.T) {
	req := eligibilityFixtureRequest()
	req.Scope.Batch = ""

	result, err := NewEligibilityUsecase(fakeEligibilityRepo{
		set: eligibilityFixtureRuleSet(),
	}).EvaluateEligibility(context.Background(), req)
	if err != nil {
		t.Fatalf("EvaluateEligibility: %v", err)
	}
	if result.Status != EligibilityBlocked {
		t.Fatalf("status = %q, want BLOCKED", result.Status)
	}
	major := result.Groups[0].Majors[0]
	if major.Status != MajorUnknown {
		t.Fatalf("major status = %q, want UNKNOWN", major.Status)
	}
	if got := major.UnknownReasons; len(got) != 1 || got[0] != ReasonEligibilityScopeInvalid {
		t.Fatalf("unknown reasons = %v, want [%s]", got, ReasonEligibilityScopeInvalid)
	}
}

func TestEvaluateEligibility_AggregatesMajorStatesIntoPassPartial(t *testing.T) {
	req := eligibilityFixtureRequest()
	req.CandidateGroups[0].Majors = []CandidateMajor{
		{
			MajorCode: "080903", MajorName: "计划外专业",
			SubjectRequirement: "首选物理，再选化学", InCurrentPlan: false,
		},
		{
			MajorCode: "080902", MajorName: "受限专业",
			SubjectRequirement: "首选物理，再选思想政治", InCurrentPlan: true,
		},
		{
			MajorCode: "080901", MajorName: "可报专业",
			SubjectRequirement: "首选物理，再选化学", InCurrentPlan: true,
		},
	}

	set := appendSubjectRule(
		eligibilityFixtureRuleSet(),
		"rule-subject-political",
		"080902",
		"首选物理，再选思想政治",
	)
	result, err := NewEligibilityUsecase(fakeEligibilityRepo{set: set}).
		EvaluateEligibility(context.Background(), req)
	if err != nil {
		t.Fatalf("EvaluateEligibility: %v", err)
	}
	if result.Status != EligibilityComplete {
		t.Fatalf("status = %q, want COMPLETE", result.Status)
	}
	group := result.Groups[0]
	if group.GroupStatus != GroupPassPartial {
		t.Fatalf("group status = %q, want PASS_PARTIAL", group.GroupStatus)
	}
	want := []struct {
		code   string
		status string
	}{
		{code: "080901", status: MajorPass},
		{code: "080902", status: MajorFail},
		{code: "080903", status: MajorNotApplicable},
	}
	if len(group.Majors) != len(want) {
		t.Fatalf("majors = %d, want %d", len(group.Majors), len(want))
	}
	for i, item := range want {
		if group.Majors[i].MajorCode != item.code || group.Majors[i].Status != item.status {
			t.Fatalf("major %d = %+v, want code=%s status=%s",
				i, group.Majors[i], item.code, item.status)
		}
	}
	if len(group.Majors[1].FailureRuleKeys) != 1 ||
		group.Majors[1].FailureRuleKeys[0] != "subject-requirement" {
		t.Fatalf("restricted major failure keys = %v", group.Majors[1].FailureRuleKeys)
	}
	if len(group.EvidenceRefs) != 2 ||
		group.EvidenceRefs[0].EvidenceID != "ev-subject" ||
		group.EvidenceRefs[1].EvidenceID != "ev-rule-subject-political" {
		t.Fatalf("group evidence = %+v", group.EvidenceRefs)
	}
}

func TestEvaluateEligibility_MissingFactMakesGroupUnknown(t *testing.T) {
	set := eligibilityFixtureRuleSet()
	set.Rules = append(set.Rules, RuleVersion{
		RuleID: "rule-math", Version: 1, RuleKey: "math-required",
		RuleType:              "SINGLE_SUBJECT_MIN",
		Scope:                 RuleScope{MajorCode: "080902"},
		RequiredProfileFields: []string{"single_subject_scores.数学"},
		When:                  RuleExpression{Operator: OperatorPRESENT, Field: "single_subject_scores.数学"},
		OutcomeWhenTrue:       OutcomeFail,
		ReasonCode:            "MATH_REQUIRED",
		ReviewState:           ReviewStatePublished,
		ExpressionVersion:     ExpressionVersionV1,
		SourceEvidence: []EvidenceRef{{
			EvidenceID:   "ev-math-required",
			EvidenceType: "DOCUMENT",
			ReleaseID:    "rel-test",
			ContentSHA:   "sha256:math-required",
		}},
	})
	set.Coverage = append(set.Coverage, RuleCoverage{
		RuleType: "SINGLE_SUBJECT_MIN",
		Status:   CoverageFull,
	})
	req := eligibilityFixtureRequest()
	req.CandidateGroups[0].Majors = []CandidateMajor{
		{
			MajorCode: "080901", MajorName: "可报专业",
			SubjectRequirement: "首选物理，再选化学", InCurrentPlan: true,
		},
		{
			MajorCode: "080902", MajorName: "待补事实专业",
			SubjectRequirement: "首选物理，再选化学", InCurrentPlan: true,
		},
	}

	result, err := NewEligibilityUsecase(fakeEligibilityRepo{set: set}).
		EvaluateEligibility(context.Background(), req)
	if err != nil {
		t.Fatalf("EvaluateEligibility: %v", err)
	}
	if result.Status != EligibilityPartial {
		t.Fatalf("status = %q, want PARTIAL", result.Status)
	}
	group := result.Groups[0]
	if group.GroupStatus != GroupUnknown {
		t.Fatalf("group status = %q, want UNKNOWN", group.GroupStatus)
	}
	if group.Majors[0].Status != MajorPass {
		t.Fatalf("passing major = %q, want PASS", group.Majors[0].Status)
	}
	missing := group.Majors[1]
	if missing.Status != MajorMissingRequiredFact {
		t.Fatalf("missing major = %q, want MISSING_REQUIRED_FACT", missing.Status)
	}
	if len(missing.MissingFields) != 1 || missing.MissingFields[0] != "single_subject_scores.数学" {
		t.Fatalf("missing fields = %v", missing.MissingFields)
	}
}

func TestEvaluateEligibility_GroupFailsWhenNoMajorPasses(t *testing.T) {
	req := eligibilityFixtureRequest()
	req.CandidateGroups[0].Majors = []CandidateMajor{
		{
			MajorCode: "080902", MajorName: "受限专业",
			SubjectRequirement: "首选物理，再选思想政治", InCurrentPlan: true,
		},
		{
			MajorCode: "080903", MajorName: "计划外专业",
			SubjectRequirement: "首选物理，再选化学", InCurrentPlan: false,
		},
	}

	set := appendSubjectRule(
		eligibilityFixtureRuleSet(),
		"rule-subject-political",
		"080902",
		"首选物理，再选思想政治",
	)
	result, err := NewEligibilityUsecase(fakeEligibilityRepo{set: set}).
		EvaluateEligibility(context.Background(), req)
	if err != nil {
		t.Fatalf("EvaluateEligibility: %v", err)
	}
	if result.Status != EligibilityComplete {
		t.Fatalf("status = %q, want COMPLETE", result.Status)
	}
	if got := result.Groups[0].GroupStatus; got != GroupFail {
		t.Fatalf("group status = %q, want FAIL", got)
	}
}

func TestEvaluateEligibility_GroupUnknownDominatesPass(t *testing.T) {
	set := eligibilityFixtureRuleSet()
	set.Coverage = append(set.Coverage, RuleCoverage{
		RuleType: RuleTypeSubjectRequirement,
		Scope:    RuleScope{MajorCode: "080902"},
		Status:   CoverageNone,
		Notes:    "candidate-level gap",
	})
	req := eligibilityFixtureRequest()
	req.CandidateGroups[0].Majors = []CandidateMajor{
		{
			MajorCode: "080901", MajorName: "可报专业",
			SubjectRequirement: "首选物理，再选化学", InCurrentPlan: true,
		},
		{
			MajorCode: "080902", MajorName: "未覆盖专业",
			SubjectRequirement: "首选物理，再选化学", InCurrentPlan: true,
		},
	}

	result, err := NewEligibilityUsecase(fakeEligibilityRepo{set: set}).
		EvaluateEligibility(context.Background(), req)
	if err != nil {
		t.Fatalf("EvaluateEligibility: %v", err)
	}
	if result.Status != EligibilityPartial {
		t.Fatalf("status = %q, want PARTIAL", result.Status)
	}
	group := result.Groups[0]
	if group.GroupStatus != GroupUnknown {
		t.Fatalf("group status = %q, want UNKNOWN", group.GroupStatus)
	}
	if group.Majors[0].Status != MajorPass || group.Majors[1].Status != MajorUnknown {
		t.Fatalf("majors = %+v, want PASS + UNKNOWN", group.Majors)
	}
}

func TestEvaluateEligibility_DuplicateGroupIdentityIsBlocked(t *testing.T) {
	req := eligibilityFixtureRequest()
	req.CandidateGroups = append(req.CandidateGroups, req.CandidateGroups[0])

	result, err := NewEligibilityUsecase(fakeEligibilityRepo{
		set: eligibilityFixtureRuleSet(),
	}).EvaluateEligibility(context.Background(), req)
	if err != nil {
		t.Fatalf("EvaluateEligibility: %v", err)
	}
	if result.Status != EligibilityBlocked {
		t.Fatalf("status = %q, want BLOCKED", result.Status)
	}
	if len(result.Groups) != 2 {
		t.Fatalf("groups = %d, want both malformed group entries", len(result.Groups))
	}
	for _, group := range result.Groups {
		if group.GroupStatus != GroupUnknown {
			t.Fatalf("group status = %q, want UNKNOWN", group.GroupStatus)
		}
		if got := group.Majors[0].UnknownReasons; len(got) != 1 || got[0] != ReasonCandidateGroupDuplicate {
			t.Fatalf("unknown reasons = %v, want [%s]", got, ReasonCandidateGroupDuplicate)
		}
	}
}

func TestEvaluateEligibility_IncompleteGroupIdentityIsBlocked(t *testing.T) {
	req := eligibilityFixtureRequest()
	req.CandidateGroups[0].MajorGroup = " "

	result, err := NewEligibilityUsecase(fakeEligibilityRepo{
		set: eligibilityFixtureRuleSet(),
	}).EvaluateEligibility(context.Background(), req)
	if err != nil {
		t.Fatalf("EvaluateEligibility: %v", err)
	}
	if result.Status != EligibilityBlocked {
		t.Fatalf("status = %q, want BLOCKED", result.Status)
	}
	if got := result.Groups[0].Majors[0].UnknownReasons; len(got) != 1 || got[0] != ReasonCandidateGroupInvalid {
		t.Fatalf("unknown reasons = %v, want [%s]", got, ReasonCandidateGroupInvalid)
	}
}

func TestEvaluateEligibility_IncompleteMajorIdentityIsBlocked(t *testing.T) {
	req := eligibilityFixtureRequest()
	req.CandidateGroups[0].Majors[0].MajorCode = " "

	result, err := NewEligibilityUsecase(fakeEligibilityRepo{
		set: eligibilityFixtureRuleSet(),
	}).EvaluateEligibility(context.Background(), req)
	if err != nil {
		t.Fatalf("EvaluateEligibility: %v", err)
	}
	if result.Status != EligibilityBlocked {
		t.Fatalf("status = %q, want BLOCKED", result.Status)
	}
	if got := result.Groups[0].Majors[0].UnknownReasons; len(got) != 1 || got[0] != ReasonCandidateMajorInvalid {
		t.Fatalf("unknown reasons = %v, want [%s]", got, ReasonCandidateMajorInvalid)
	}
}

func TestEvaluateEligibility_GroupAggregationIsDeterministicallySorted(t *testing.T) {
	req := eligibilityFixtureRequest()
	req.CandidateGroups = []CandidateGroup{
		{
			UniversityID: 2, UniversityCode: "1002", UniversityName: "测试大学二", MajorGroup: "02",
			Majors: []CandidateMajor{
				{
					MajorCode: "080902", MajorName: "软件工程",
					SubjectRequirement: "首选物理，再选化学", InCurrentPlan: true,
				},
				{
					MajorCode: "080901", MajorName: "计算机科学与技术",
					SubjectRequirement: "首选物理，再选化学", InCurrentPlan: true,
				},
			},
		},
		{
			UniversityID: 1, UniversityCode: "1001", UniversityName: "测试大学", MajorGroup: "01",
			Majors: []CandidateMajor{{
				MajorCode: "080904", MajorName: "信息安全",
				SubjectRequirement: "首选物理，再选化学", InCurrentPlan: true,
			}},
		},
	}
	uc := NewEligibilityUsecase(fakeEligibilityRepo{set: eligibilityFixtureRuleSet()})

	first, err := uc.EvaluateEligibility(context.Background(), req)
	if err != nil {
		t.Fatalf("first EvaluateEligibility: %v", err)
	}
	second, err := uc.EvaluateEligibility(context.Background(), req)
	if err != nil {
		t.Fatalf("second EvaluateEligibility: %v", err)
	}
	if !reflect.DeepEqual(first, second) {
		t.Fatalf("same input changed result:\nfirst=%+v\nsecond=%+v", first, second)
	}
	if first.Groups[0].GroupKey != "1001|01" || first.Groups[1].GroupKey != "1002|02" {
		t.Fatalf("group order = %q, %q", first.Groups[0].GroupKey, first.Groups[1].GroupKey)
	}
	if got := []string{
		first.Groups[1].Majors[0].MajorCode,
		first.Groups[1].Majors[1].MajorCode,
	}; !reflect.DeepEqual(got, []string{"080901", "080902"}) {
		t.Fatalf("major order = %v", got)
	}
}

func appendSubjectRule(
	set EligibilityRuleSet,
	ruleID string,
	majorCode string,
	subjectRequirement string,
) EligibilityRuleSet {
	rule := set.Rules[0]
	rule.RuleID = ruleID
	rule.Scope = RuleScope{MajorCode: majorCode}
	rule.When = RuleExpression{
		Operator: OperatorNot,
		Child: &RuleExpression{
			Operator:           OperatorSubjectMatch,
			SubjectRequirement: subjectRequirement,
		},
	}
	rule.SourceEvidence = []EvidenceRef{{
		EvidenceID:   "ev-" + ruleID,
		EvidenceType: "DOCUMENT",
		ReleaseID:    "rel-test",
		ContentSHA:   "sha256:" + ruleID,
	}}
	set.Rules = append(set.Rules, rule)
	return set
}
