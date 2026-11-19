package biz

import (
	"context"
	"testing"
)

func TestEvaluateEligibility_BlocksOverlappingPublishedRuleKeys(t *testing.T) {
	set := eligibilityFixtureRuleSet()
	set.Rules = append(set.Rules, RuleVersion{
		RuleID: "rule-subject-copy", Version: 1, RuleKey: "subject-requirement",
		RuleType: RuleTypeSubjectRequirement, ReviewState: ReviewStatePublished,
		When: RuleExpression{Operator: OperatorNot, Child: &RuleExpression{
			Operator: OperatorSubjectMatch,
		}},
		OutcomeWhenTrue: OutcomeFail,
	})

	result, err := NewEligibilityUsecase(fakeEligibilityRepo{set: set}).EvaluateEligibility(
		context.Background(),
		eligibilityFixtureRequest(),
	)
	if err != nil {
		t.Fatalf("EvaluateEligibility: %v", err)
	}
	if result.Status != EligibilityBlocked {
		t.Fatalf("status = %q, want BLOCKED", result.Status)
	}
	if result.Diagnostics.ConflictCount != 1 {
		t.Fatalf("conflict count = %d, want 1", result.Diagnostics.ConflictCount)
	}
	if got := result.Groups[0].Majors[0].UnknownReasons; len(got) != 1 || got[0] != ReasonRuleConflict {
		t.Fatalf("unknown reasons = %v, want [%s]", got, ReasonRuleConflict)
	}
}

func TestEvaluateEligibility_AllowsSameRuleKeyWithDisjointScopes(t *testing.T) {
	set := eligibilityFixtureRuleSet()
	set.Rules[0].Scope.UniversityID = 1
	disjoint := set.Rules[0]
	disjoint.RuleID = "rule-subject-other-school"
	disjoint.Scope.UniversityID = 2
	set.Rules = append(set.Rules, disjoint)

	result, err := NewEligibilityUsecase(fakeEligibilityRepo{set: set}).EvaluateEligibility(
		context.Background(),
		eligibilityFixtureRequest(),
	)
	if err != nil {
		t.Fatalf("EvaluateEligibility: %v", err)
	}
	if result.Status != EligibilityComplete || result.Diagnostics.ConflictCount != 0 {
		t.Fatalf("disjoint scopes blocked: status=%q conflicts=%d",
			result.Status, result.Diagnostics.ConflictCount)
	}
	if got := result.Groups[0].Majors[0].Status; got != MajorPass {
		t.Fatalf("major status = %q, want PASS", got)
	}
}

func TestEvaluateEligibility_DraftAndReviewedRulesDoNotChangePublishedResult(t *testing.T) {
	published := eligibilityFixtureRuleSet()
	baseline, err := NewEligibilityUsecase(fakeEligibilityRepo{set: published}).EvaluateEligibility(
		context.Background(),
		eligibilityFixtureRequest(),
	)
	if err != nil {
		t.Fatalf("baseline EvaluateEligibility: %v", err)
	}

	withDrafts := published
	withDrafts.Rules = append(withDrafts.Rules,
		RuleVersion{
			RuleID: "rule-draft", Version: 1, RuleKey: "draft-only",
			RuleType: RuleTypeSubjectRequirement, ReviewState: "DRAFT",
			When: RuleExpression{Operator: OperatorNot, Child: &RuleExpression{
				Operator: OperatorSubjectMatch,
			}},
		},
		RuleVersion{
			RuleID: "rule-reviewed", Version: 1, RuleKey: "reviewed-only",
			RuleType: RuleTypeSubjectRequirement, ReviewState: "REVIEWED",
			When: RuleExpression{Operator: OperatorNot, Child: &RuleExpression{
				Operator: OperatorSubjectMatch,
			}},
		},
	)
	result, err := NewEligibilityUsecase(fakeEligibilityRepo{set: withDrafts}).EvaluateEligibility(
		context.Background(),
		eligibilityFixtureRequest(),
	)
	if err != nil {
		t.Fatalf("EvaluateEligibility with drafts: %v", err)
	}
	if result.Status != baseline.Status ||
		result.Groups[0].GroupStatus != baseline.Groups[0].GroupStatus ||
		result.Groups[0].Majors[0].Status != baseline.Groups[0].Majors[0].Status {
		t.Fatalf("DRAFT/REVIEWED changed result: got %+v, baseline %+v", result, baseline)
	}
	if result.Groups[0].Majors[0].Status != MajorPass {
		t.Fatalf("published rule should still PASS, got %q", result.Groups[0].Majors[0].Status)
	}
}

func TestEvaluateEligibility_RevokedRuleTypeInvalidatesCoverage(t *testing.T) {
	set := eligibilityFixtureRuleSet()
	set.RevokedRules = []RevokedRuleVersion{{
		RuleKey:  "subject-requirement",
		RuleType: RuleTypeSubjectRequirement,
		Scope:    RuleScope{UniversityID: 1},
	}}

	result, err := NewEligibilityUsecase(fakeEligibilityRepo{set: set}).EvaluateEligibility(
		context.Background(),
		eligibilityFixtureRequest(),
	)
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
		t.Fatalf("unknown reasons = %v, want [%s]", got, ReasonRuleCoverageMissing)
	}
}

func TestEvaluateEligibility_DisjointRevokedRuleDoesNotInvalidateCoverage(t *testing.T) {
	set := eligibilityFixtureRuleSet()
	set.RevokedRules = []RevokedRuleVersion{{
		RuleKey:  "subject-requirement",
		RuleType: RuleTypeSubjectRequirement,
		Scope:    RuleScope{UniversityID: 2},
	}}

	result, err := NewEligibilityUsecase(fakeEligibilityRepo{set: set}).EvaluateEligibility(
		context.Background(),
		eligibilityFixtureRequest(),
	)
	if err != nil {
		t.Fatalf("EvaluateEligibility: %v", err)
	}
	if result.Status != EligibilityComplete {
		t.Fatalf("status = %q, want COMPLETE", result.Status)
	}
	if got := result.Groups[0].Majors[0].Status; got != MajorPass {
		t.Fatalf("major status = %q, want PASS", got)
	}
}

func eligibilityFixtureRuleSet() EligibilityRuleSet {
	return EligibilityRuleSet{
		ReleaseStatus: "PUBLISHED",
		Rules: []RuleVersion{{
			RuleID: "rule-subject", Version: 1, RuleKey: "subject-requirement",
			RuleType: RuleTypeSubjectRequirement, ReviewState: ReviewStatePublished,
			RequiredProfileFields: []string{FieldSubjects, FieldCategory},
			ExpressionVersion:     ExpressionVersionV1,
			When: RuleExpression{Operator: OperatorNot, Child: &RuleExpression{
				Operator: OperatorSubjectMatch,
			}},
			OutcomeWhenTrue: OutcomeFail,
			SourceEvidence: []EvidenceRef{{
				EvidenceID: "ev-subject", EvidenceType: "DOCUMENT",
				ReleaseID: "rel-test", ContentSHA: "sha256:subject",
			}},
		}},
		Coverage: []RuleCoverage{{
			RuleType: RuleTypeSubjectRequirement,
			Status:   CoverageFull,
		}},
	}
}
