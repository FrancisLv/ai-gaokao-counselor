package biz

import (
	"context"
	"errors"
	"testing"

	errs "github.com/choucisan/ai-gaokao-counselor/internal/pkg/errors"
)

func TestEvaluateEligibility_AnyDeclaredCoverageGapMakesResultPartial(t *testing.T) {
	set := eligibilityFixtureRuleSet()
	set.Coverage = append(set.Coverage, RuleCoverage{
		RuleType: "GENDER_RESTRICTION",
		Status:   CoverageNone,
		Notes:    "gender source not published",
	})

	result := evaluateCoverageFixture(t, set)
	if result.Status != EligibilityPartial {
		t.Fatalf("status = %q, want PARTIAL", result.Status)
	}
	if got := result.Groups[0].Majors[0].Status; got != MajorUnknown {
		t.Fatalf("major status = %q, want UNKNOWN", got)
	}
}

func TestEvaluateEligibility_UnsupportedBatchBlocksWithStableReason(t *testing.T) {
	set := eligibilityFixtureRuleSet()
	request := eligibilityFixtureRequest()
	request.Scope.Batch = "专科批"

	result, err := NewEligibilityUsecase(fakeEligibilityRepo{set: set}).EvaluateEligibility(
		context.Background(),
		request,
	)
	if err != nil {
		t.Fatalf("EvaluateEligibility: %v", err)
	}
	if result.Status != EligibilityBlocked {
		t.Fatalf("status = %q, want BLOCKED", result.Status)
	}
	reasons := result.Groups[0].Majors[0].UnknownReasons
	if len(reasons) != 1 || reasons[0] != "BATCH_UNSUPPORTED" {
		t.Fatalf("unknown reasons = %v, want [BATCH_UNSUPPORTED]", reasons)
	}
}

func TestEvaluateEligibility_UnsupportedReleaseScopeBlocksWithStableReason(t *testing.T) {
	supported := false
	result, err := NewEligibilityUsecase(fakeEligibilityRepo{
		set:            eligibilityFixtureRuleSet(),
		scopeSupported: &supported,
	}).EvaluateEligibility(context.Background(), eligibilityFixtureRequest())
	if err != nil {
		t.Fatalf("EvaluateEligibility: %v", err)
	}
	if result.Status != EligibilityBlocked {
		t.Fatalf("status = %q, want BLOCKED", result.Status)
	}
	reasons := result.Groups[0].Majors[0].UnknownReasons
	if len(reasons) != 1 || reasons[0] != ReasonProfileUnsupportedScope {
		t.Fatalf("unknown reasons = %v, want [%s]", reasons, ReasonProfileUnsupportedScope)
	}
}

func TestEvaluateEligibility_RuleWithoutSourceBlocksWithStableReason(t *testing.T) {
	set := eligibilityFixtureRuleSet()
	set.Rules[0].SourceEvidence = nil

	result := evaluateCoverageFixture(t, set)
	if result.Status != EligibilityBlocked {
		t.Fatalf("status = %q, want BLOCKED", result.Status)
	}
	reasons := result.Groups[0].Majors[0].UnknownReasons
	if len(reasons) != 1 || reasons[0] != "RULE_SOURCE_MISSING" {
		t.Fatalf("unknown reasons = %v, want [RULE_SOURCE_MISSING]", reasons)
	}
}

func TestValidateRuleVersionForLifecycle_AcceptsCanonicalPublishedRule(t *testing.T) {
	rule := eligibilityFixtureRuleSet().Rules[0]
	reason := ValidateRuleVersionForLifecycle(
		rule,
		profileFieldDefinitionIndex(BuiltinEligibilityProfileFields()),
	)
	if reason != "" {
		t.Fatalf("canonical rule validation reason = %q, want empty", reason)
	}
}

func TestValidateRuleVersionForLifecycle_RequiresExplicitSubjectRequirement(t *testing.T) {
	rule := eligibilityFixtureRuleSet().Rules[0]
	rule.When.Child.SubjectRequirement = ""
	reason := ValidateRuleVersionForLifecycle(
		rule,
		profileFieldDefinitionIndex(BuiltinEligibilityProfileFields()),
	)
	if reason != ReasonRuleExpression {
		t.Fatalf("missing subject_req reason = %q, want %q", reason, ReasonRuleExpression)
	}
}

func TestEvaluateEligibility_RejectsUnboundProfileSnapshot(t *testing.T) {
	request := eligibilityFixtureRequest()
	request.ProfileFacts[1].StringValue = "历史类"
	_, err := NewEligibilityUsecase(fakeEligibilityRepo{set: eligibilityFixtureRuleSet()}).
		EvaluateEligibility(context.Background(), request)
	if !errors.Is(err, errs.ErrInvalidQuery) {
		t.Fatalf("snapshot mismatch err = %v, want ErrInvalidQuery", err)
	}
}

func TestEvaluateEligibility_RejectsDuplicateProfileFacts(t *testing.T) {
	request := eligibilityFixtureRequest()
	request.ProfileFacts = append(request.ProfileFacts, request.ProfileFacts[0])
	request.ProfileSnapshotSHA256 = EligibilityProfileSnapshotSHA256(
		request.ConfirmedProfileVersion,
		request.ProfileFacts,
	)
	_, err := NewEligibilityUsecase(fakeEligibilityRepo{set: eligibilityFixtureRuleSet()}).
		EvaluateEligibility(context.Background(), request)
	if !errors.Is(err, errs.ErrInvalidQuery) {
		t.Fatalf("duplicate facts err = %v, want ErrInvalidQuery", err)
	}
}

func TestEligibilityProfileSnapshotSHA256_CrossLanguageContract(t *testing.T) {
	got := EligibilityProfileSnapshotSHA256("profile-v1", []ProfileFact{
		profileFactStrings(FieldSubjects, "物理", "化学", "生物"),
		profileFactString(FieldCategory, "物理类"),
	})
	const want = "f60bc119283ebe04b7ce6df5d3a5cc4edefa4535e2481ac3a9a1444981a465c4"
	if got != want {
		t.Fatalf("profile snapshot digest = %q, want %q", got, want)
	}
}
