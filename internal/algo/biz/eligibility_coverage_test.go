package biz

import (
	"context"
	"testing"
)

func TestEvaluateEligibility_NonFullCoverageNeverPasses(t *testing.T) {
	for _, status := range []string{CoveragePartial, CoverageNone} {
		t.Run(status, func(t *testing.T) {
			set := eligibilityFixtureRuleSet()
			set.Coverage[0].Status = status

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
			major := result.Groups[0].Majors[0]
			if major.Status != MajorUnknown {
				t.Fatalf("major status = %q, want UNKNOWN", major.Status)
			}
			if len(major.UnknownReasons) != 1 || major.UnknownReasons[0] != ReasonRuleCoverageMissing {
				t.Fatalf("unknown reasons = %v, want [%s]", major.UnknownReasons, ReasonRuleCoverageMissing)
			}
		})
	}
}

func TestEvaluateEligibility_UsesMostSpecificApplicableCoverage(t *testing.T) {
	t.Run("specific partial overrides broad full", func(t *testing.T) {
		set := eligibilityFixtureRuleSet()
		set.Coverage = append(set.Coverage, RuleCoverage{
			RuleType: RuleTypeSubjectRequirement,
			Scope:    RuleScope{UniversityID: 1},
			Status:   CoveragePartial,
			Notes:    "candidate-level gap",
		})

		result := evaluateCoverageFixture(t, set)
		if result.Status != EligibilityPartial {
			t.Fatalf("status = %q, want PARTIAL", result.Status)
		}
		if got := result.Groups[0].Majors[0].Status; got != MajorUnknown {
			t.Fatalf("major status = %q, want UNKNOWN", got)
		}
	})

	t.Run("specific full overrides broad none", func(t *testing.T) {
		set := eligibilityFixtureRuleSet()
		set.Coverage[0].Status = CoverageNone
		set.Coverage[0].Notes = "broad gap"
		set.Coverage = append(set.Coverage, RuleCoverage{
			RuleType:           RuleTypeSubjectRequirement,
			Scope:              RuleScope{UniversityID: 1, MajorGroup: "01", MajorCode: "080901"},
			Status:             CoverageFull,
			CoveredDataSources: []string{"enrollment_plan"},
			Notes:              "complete candidate coverage",
		})

		result := evaluateCoverageFixture(t, set)
		if result.Status != EligibilityComplete {
			t.Fatalf("status = %q, want COMPLETE", result.Status)
		}
		if got := result.Groups[0].Majors[0].Status; got != MajorPass {
			t.Fatalf("major status = %q, want PASS", got)
		}
	})
}

func TestEvaluateEligibility_BlocksEquallySpecificCoverageConflict(t *testing.T) {
	set := eligibilityFixtureRuleSet()
	set.Coverage = []RuleCoverage{
		{
			RuleType:           RuleTypeSubjectRequirement,
			Scope:              RuleScope{UniversityID: 1},
			Status:             CoverageFull,
			CoveredDataSources: []string{"enrollment_plan"},
			Notes:              "school coverage",
		},
		{
			RuleType: RuleTypeSubjectRequirement,
			Scope:    RuleScope{MajorGroup: "01"},
			Status:   CoverageNone,
			Notes:    "group gap",
		},
	}

	result := evaluateCoverageFixture(t, set)
	if result.Status != EligibilityBlocked {
		t.Fatalf("status = %q, want BLOCKED", result.Status)
	}
	if result.Diagnostics.ConflictCount != 1 {
		t.Fatalf("conflict count = %d, want 1", result.Diagnostics.ConflictCount)
	}
	major := result.Groups[0].Majors[0]
	if major.Status != MajorUnknown {
		t.Fatalf("major status = %q, want UNKNOWN", major.Status)
	}
	if len(major.UnknownReasons) != 1 || major.UnknownReasons[0] != ReasonRuleConflict {
		t.Fatalf("unknown reasons = %v, want [%s]", major.UnknownReasons, ReasonRuleConflict)
	}
}

func TestEvaluateEligibility_IgnoresDisjointCoverageAtEqualSpecificity(t *testing.T) {
	set := eligibilityFixtureRuleSet()
	set.Coverage = []RuleCoverage{{
		RuleType:           RuleTypeSubjectRequirement,
		Scope:              RuleScope{UniversityID: 2},
		Status:             CoverageFull,
		CoveredDataSources: []string{"enrollment_plan"},
		Notes:              "other school",
	}}

	result := evaluateCoverageFixture(t, set)
	if result.Status != EligibilityPartial {
		t.Fatalf("status = %q, want PARTIAL", result.Status)
	}
	if result.Diagnostics.ConflictCount != 0 {
		t.Fatalf("conflict count = %d, want 0", result.Diagnostics.ConflictCount)
	}
}

func evaluateCoverageFixture(t *testing.T, set EligibilityRuleSet) EligibilityResult {
	t.Helper()
	result, err := NewEligibilityUsecase(fakeEligibilityRepo{set: set}).EvaluateEligibility(
		context.Background(),
		eligibilityFixtureRequest(),
	)
	if err != nil {
		t.Fatalf("EvaluateEligibility: %v", err)
	}
	return result
}
