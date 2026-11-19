package biz

import (
	"context"
	"errors"
	"testing"

	errs "github.com/choucisan/ai-gaokao-counselor/internal/pkg/errors"
)

type fakeEligibilityRepo struct {
	set            EligibilityRuleSet
	err            error
	scopeSupported *bool
}

func (f fakeEligibilityRepo) LoadEligibilityRuleSet(context.Context, string, EligibilityScope) (EligibilityRuleSet, error) {
	if len(f.set.ProfileFields) == 0 {
		f.set.ProfileFields = BuiltinEligibilityProfileFields()
	}
	f.set.ReleaseScopeSupported = true
	if f.scopeSupported != nil {
		f.set.ReleaseScopeSupported = *f.scopeSupported
	}
	return f.set, f.err
}

func TestEvaluateEligibility_SubjectRulePassAndFail(t *testing.T) {
	if matched, reason, err := matchCandidateSubjects(
		[]string{"物理", "化学", "生物"},
		[]string{"首选物理，再选化学"},
	); !matched || err != nil {
		t.Fatalf("subject fixture invalid: matched=%v reason=%q err=%v", matched, reason, err)
	}
	set := eligibilityFixtureRuleSet()
	set = appendSubjectRule(
		set,
		"rule-subject-080902",
		"080902",
		"首选物理，再选思想政治",
	)
	uc := NewEligibilityUsecase(fakeEligibilityRepo{set: set})

	result, err := uc.EvaluateEligibility(context.Background(), EligibilityRequest{
		ReleaseID:               "rel-test",
		ConfirmedProfileVersion: "profile-v1",
		ProfileLifecycle:        ProfileLifecycleConfirmed,
		ProfileSnapshotSHA256: EligibilityProfileSnapshotSHA256(
			"profile-v1",
			[]ProfileFact{
				profileFactStrings(FieldSubjects, "物理", "化学", "生物"),
				profileFactString(FieldCategory, "物理类"),
			},
		),
		Scope: EligibilityScope{
			Province: "湖南", ApplicationYear: 2025, Category: "物理类", Batch: "本科批",
		},
		ProfileFacts: []ProfileFact{
			profileFactStrings(FieldSubjects, "物理", "化学", "生物"),
			profileFactString(FieldCategory, "物理类"),
		},
		CandidateGroups: []CandidateGroup{
			{
				UniversityID: 1, UniversityCode: "1001", UniversityName: "测试大学", MajorGroup: "01",
				Majors: []CandidateMajor{{
					MajorCode: "080901", MajorName: "计算机科学与技术",
					SubjectRequirement: "首选物理，再选化学", InCurrentPlan: true,
				}},
			},
			{
				UniversityID: 2, UniversityCode: "1002", UniversityName: "测试大学二", MajorGroup: "02",
				Majors: []CandidateMajor{{
					MajorCode: "080902", MajorName: "软件工程",
					SubjectRequirement: "首选物理，再选思想政治", InCurrentPlan: true,
				}},
			},
		},
	})
	if err != nil {
		t.Fatalf("EvaluateEligibility: %v", err)
	}
	if result.Status != EligibilityComplete {
		t.Fatalf("status = %q, want COMPLETE; groups=%+v", result.Status, result.Groups)
	}
	if len(result.Groups) != 2 {
		t.Fatalf("groups = %d, want 2", len(result.Groups))
	}
	if got := result.Groups[0].Majors[0].Status; got != MajorPass {
		t.Fatalf("matched major = %q, want PASS", got)
	}
	failed := result.Groups[1].Majors[0]
	if failed.Status != MajorFail {
		t.Fatalf("unmatched major = %q, want FAIL", failed.Status)
	}
	if len(failed.FailureRuleKeys) != 1 || failed.FailureRuleKeys[0] != "subject-requirement" {
		t.Fatalf("failure_rule_keys = %v", failed.FailureRuleKeys)
	}
	if len(failed.EvidenceRefs) != 1 ||
		failed.EvidenceRefs[0].EvidenceID != "ev-rule-subject-080902" {
		t.Fatalf("evidence_refs = %+v", failed.EvidenceRefs)
	}
}

func TestEvaluateEligibility_BlocksReleaseAndDoesNotTreatUnknownAsPass(t *testing.T) {
	t.Run("revoked release", func(t *testing.T) {
		uc := NewEligibilityUsecase(fakeEligibilityRepo{set: EligibilityRuleSet{ReleaseStatus: "REVOKED"}})
		result, err := uc.EvaluateEligibility(context.Background(), eligibilityFixtureRequest())
		if err != nil {
			t.Fatalf("EvaluateEligibility: %v", err)
		}
		if result.Status != EligibilityBlocked {
			t.Fatalf("status = %q, want BLOCKED", result.Status)
		}
		if got := result.Groups[0].Majors[0].Status; got != MajorUnknown {
			t.Fatalf("major status = %q, want UNKNOWN", got)
		}
	})

	t.Run("draft profile", func(t *testing.T) {
		req := eligibilityFixtureRequest()
		req.ProfileLifecycle = "DRAFT"
		uc := NewEligibilityUsecase(fakeEligibilityRepo{set: EligibilityRuleSet{ReleaseStatus: "PUBLISHED"}})
		_, err := uc.EvaluateEligibility(context.Background(), req)
		if !errors.Is(err, errs.ErrProfileNotConfirmed) {
			t.Fatalf("err = %v, want PROFILE_NOT_CONFIRMED", err)
		}
	})

	t.Run("missing coverage", func(t *testing.T) {
		uc := NewEligibilityUsecase(fakeEligibilityRepo{set: EligibilityRuleSet{
			ReleaseStatus: "PUBLISHED",
			Rules: []RuleVersion{{
				RuleID: "rule-subject", Version: 1, RuleKey: "subject-requirement",
				RuleType: RuleTypeSubjectRequirement, ReviewState: ReviewStatePublished,
				RequiredProfileFields: []string{FieldSubjects, FieldCategory},
				ExpressionVersion:     ExpressionVersionV1,
				When: RuleExpression{Operator: OperatorNot, Child: &RuleExpression{
					Operator:           OperatorSubjectMatch,
					SubjectRequirement: "首选物理，再选化学",
				}},
				OutcomeWhenTrue: OutcomeFail,
				ReasonCode:      "SUBJECT_NOT_MATCHED",
				SourceEvidence: []EvidenceRef{{
					EvidenceID: "ev-missing-coverage", EvidenceType: "DOCUMENT",
					ReleaseID: "rel-test", ContentSHA: "sha256:missing-coverage",
				}},
			}},
		}})
		result, err := uc.EvaluateEligibility(context.Background(), eligibilityFixtureRequest())
		if err != nil {
			t.Fatalf("EvaluateEligibility: %v", err)
		}
		if result.Status != EligibilityPartial {
			t.Fatalf("status = %q, want PARTIAL", result.Status)
		}
		if got := result.Groups[0].Majors[0].Status; got != MajorUnknown {
			t.Fatalf("major status = %q, want UNKNOWN", got)
		}
		if got := result.Groups[0].Majors[0].UnknownReasons; len(got) != 1 || got[0] != ReasonRuleCoverageMissing {
			t.Fatalf("unknown reasons = %v", got)
		}
	})

	t.Run("invalid outcome is unknown", func(t *testing.T) {
		uc := NewEligibilityUsecase(fakeEligibilityRepo{set: EligibilityRuleSet{
			ReleaseStatus: "PUBLISHED",
			Rules: []RuleVersion{{
				RuleID: "rule-subject", Version: 1, RuleKey: "subject-requirement",
				RuleType: RuleTypeSubjectRequirement, ReviewState: ReviewStatePublished,
				RequiredProfileFields: []string{FieldSubjects, FieldCategory},
				ExpressionVersion:     ExpressionVersionV1,
				OutcomeWhenTrue:       "PASS",
				When: RuleExpression{Operator: OperatorNot, Child: &RuleExpression{
					Operator:           OperatorSubjectMatch,
					SubjectRequirement: "首选物理，再选化学",
				}},
				SourceEvidence: []EvidenceRef{{
					EvidenceID: "ev-invalid-outcome", EvidenceType: "DOCUMENT",
					ReleaseID: "rel-test", ContentSHA: "sha256:invalid-outcome",
				}},
			}},
			Coverage: []RuleCoverage{{RuleType: RuleTypeSubjectRequirement, Status: CoverageFull}},
		}})
		req := eligibilityFixtureRequest()
		req.CandidateGroups[0].Majors[0].SubjectRequirement = "首选物理，再选思想政治"
		result, err := uc.EvaluateEligibility(context.Background(), req)
		if err != nil {
			t.Fatalf("EvaluateEligibility: %v", err)
		}
		if got := result.Groups[0].Majors[0].Status; got != MajorUnknown {
			t.Fatalf("major status = %q, want UNKNOWN", got)
		}
	})
}

func eligibilityFixtureRequest() EligibilityRequest {
	facts := []ProfileFact{
		profileFactStrings(FieldSubjects, "物理", "化学", "生物"),
		profileFactString(FieldCategory, "物理类"),
	}
	return EligibilityRequest{
		ReleaseID:               "rel-test",
		ConfirmedProfileVersion: "profile-v1",
		ProfileLifecycle:        ProfileLifecycleConfirmed,
		ProfileSnapshotSHA256:   EligibilityProfileSnapshotSHA256("profile-v1", facts),
		Scope: EligibilityScope{
			Province: "湖南", ApplicationYear: 2025, Category: "物理类", Batch: "本科批",
		},
		ProfileFacts: facts,
		CandidateGroups: []CandidateGroup{{
			UniversityID: 1, UniversityCode: "1001", UniversityName: "测试大学", MajorGroup: "01",
			Majors: []CandidateMajor{{
				MajorCode: "080901", MajorName: "计算机科学与技术",
				SubjectRequirement: "首选物理，再选化学", InCurrentPlan: true,
			}},
		}},
	}
}

func profileFactStrings(key string, values ...string) ProfileFact {
	return ProfileFact{FieldKey: key, State: ProfileProvided, StringValues: values}
}

func profileFactString(key, value string) ProfileFact {
	return ProfileFact{FieldKey: key, State: ProfileProvided, StringValue: value}
}
