package biz

import (
	"context"
	"testing"
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
