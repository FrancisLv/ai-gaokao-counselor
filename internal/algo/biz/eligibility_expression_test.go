package biz

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
)

func TestEvaluateEligibility_KleeneThreeValuedLogic(t *testing.T) {
	match := RuleExpression{
		Operator: OperatorEQ,
		Field:    FieldCategory,
		Value:    expressionLiteral(t, "物理类"),
	}
	noMatch := RuleExpression{
		Operator: OperatorEQ,
		Field:    FieldCategory,
		Value:    expressionLiteral(t, "历史类"),
	}
	indeterminate := RuleExpression{
		Operator: OperatorEQ,
		Field:    FieldApplicationYear,
		Value:    expressionLiteral(t, 2025),
	}
	atoms := []struct {
		name       string
		expression RuleExpression
		decision   string
	}{
		{name: "MATCH", expression: match, decision: "MATCH"},
		{name: "NO_MATCH", expression: noMatch, decision: "NO_MATCH"},
		{name: "INDETERMINATE", expression: indeterminate, decision: "INDETERMINATE"},
	}

	for _, operator := range []string{OperatorAND, OperatorOR} {
		for _, left := range atoms {
			for _, right := range atoms {
				t.Run(operator+"/"+left.name+"/"+right.name, func(t *testing.T) {
					expression := RuleExpression{
						Operator: operator,
						Left:     &left.expression,
						Right:    &right.expression,
					}
					assertExpressionStatus(t, expression, majorStatusForDecision(kleeneDecision(
						operator, left.decision, right.decision,
					)))
				})
			}
		}
	}

	for _, atom := range atoms {
		t.Run(OperatorNot+"/"+atom.name, func(t *testing.T) {
			child := atom.expression
			assertExpressionStatus(t, RuleExpression{
				Operator: OperatorNot,
				Child:    &child,
			}, majorStatusForDecision(invertDecision(atom.decision)))
		})
	}
}

func TestEvaluateEligibility_TypedOperators(t *testing.T) {
	scoreFacts := []ProfileFact{
		profileFactInt(FieldApplicationYear, 2025),
		profileFactInt("score", 650),
	}
	subjectMatch := RuleExpression{
		Operator:           OperatorSubjectMatch,
		SubjectRequirement: "首选物理，再选化学",
	}
	subjectMismatch := RuleExpression{
		Operator:           OperatorSubjectMatch,
		SubjectRequirement: "首选物理，再选思想政治",
	}
	cases := []struct {
		name       string
		expression RuleExpression
		facts      []ProfileFact
		expected   string
	}{
		{
			name: "EQ string match",
			expression: RuleExpression{
				Operator: OperatorEQ, Field: FieldCategory, Value: expressionLiteral(t, "物理类"),
			},
			expected: MajorFail,
		},
		{
			name: "EQ no match",
			expression: RuleExpression{
				Operator: OperatorEQ, Field: FieldCategory, Value: expressionLiteral(t, "历史类"),
			},
			expected: MajorPass,
		},
		{
			name: "NEQ string match",
			expression: RuleExpression{
				Operator: OperatorNEQ, Field: FieldCategory, Value: expressionLiteral(t, "历史类"),
			},
			expected: MajorFail,
		},
		{
			name: "NEQ no match",
			expression: RuleExpression{
				Operator: OperatorNEQ, Field: FieldCategory, Value: expressionLiteral(t, "物理类"),
			},
			expected: MajorPass,
		},
		{
			name: "IN string",
			expression: RuleExpression{
				Operator: OperatorIN, Field: FieldCategory,
				Values: []json.RawMessage{expressionLiteral(t, "历史类"), expressionLiteral(t, "物理类")},
			},
			expected: MajorFail,
		},
		{
			name: "IN int",
			expression: RuleExpression{
				Operator: OperatorIN, Field: "score",
				Values: []json.RawMessage{expressionLiteral(t, 600), expressionLiteral(t, 650)},
			},
			facts:    scoreFacts,
			expected: MajorFail,
		},
		{
			name: "IN no match",
			expression: RuleExpression{
				Operator: OperatorIN, Field: FieldCategory,
				Values: []json.RawMessage{expressionLiteral(t, "历史类")},
			},
			expected: MajorPass,
		},
		{
			name: "GTE int",
			expression: RuleExpression{
				Operator: OperatorGTE, Field: "score", Value: expressionLiteral(t, 600),
			},
			facts:    scoreFacts,
			expected: MajorFail,
		},
		{
			name: "GTE no match",
			expression: RuleExpression{
				Operator: OperatorGTE, Field: "score", Value: expressionLiteral(t, 700),
			},
			facts:    scoreFacts,
			expected: MajorPass,
		},
		{
			name: "LTE no match",
			expression: RuleExpression{
				Operator: OperatorLTE, Field: "score", Value: expressionLiteral(t, 600),
			},
			facts:    scoreFacts,
			expected: MajorPass,
		},
		{
			name: "LTE match",
			expression: RuleExpression{
				Operator: OperatorLTE, Field: "score", Value: expressionLiteral(t, 650),
			},
			facts:    scoreFacts,
			expected: MajorFail,
		},
		{
			name: "PRESENT missing",
			expression: RuleExpression{
				Operator: OperatorPRESENT, Field: "gender",
			},
			expected: MajorMissingRequiredFact,
		},
		{
			name: "PRESENT provided",
			expression: RuleExpression{
				Operator: OperatorPRESENT, Field: FieldCategory,
			},
			expected: MajorFail,
		},
		{
			name: "CONTAINS_ALL",
			expression: RuleExpression{
				Operator: OperatorCONTAINS_ALL, Field: FieldSubjects,
				Values: []json.RawMessage{expressionLiteral(t, "物理"), expressionLiteral(t, "化学")},
			},
			expected: MajorFail,
		},
		{
			name: "CONTAINS_ALL no match",
			expression: RuleExpression{
				Operator: OperatorCONTAINS_ALL, Field: FieldSubjects,
				Values: []json.RawMessage{expressionLiteral(t, "物理"), expressionLiteral(t, "政治")},
			},
			expected: MajorPass,
		},
		{
			name: "CONTAINS_ANY",
			expression: RuleExpression{
				Operator: OperatorCONTAINS_ANY, Field: FieldSubjects,
				Values: []json.RawMessage{expressionLiteral(t, "政治"), expressionLiteral(t, "化学")},
			},
			expected: MajorFail,
		},
		{
			name: "CONTAINS_ANY no match",
			expression: RuleExpression{
				Operator: OperatorCONTAINS_ANY, Field: FieldSubjects,
				Values: []json.RawMessage{expressionLiteral(t, "政治"), expressionLiteral(t, "地理")},
			},
			expected: MajorPass,
		},
		{
			name:       "SUBJECT_MATCH",
			expression: subjectMatch,
			expected:   MajorFail,
		},
		{
			name:       "SUBJECT_MATCH no match",
			expression: subjectMismatch,
			expected:   MajorPass,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			assertExpressionStatus(t, tc.expression, tc.expected, tc.facts...)
		})
	}
}

func TestEvaluateEligibility_ProfileTypeMismatchIsUnknown(t *testing.T) {
	t.Run("EQ", func(t *testing.T) {
		expression := RuleExpression{
			Operator: OperatorEQ,
			Field:    FieldCategory,
			Value:    expressionLiteral(t, "物理类"),
		}
		assertExpressionStatusWithFacts(
			t,
			expression,
			MajorUnknown,
			[]ProfileFact{{FieldKey: FieldCategory, State: ProfileProvided, IntValue: 1}},
		)
	})
	t.Run("SUBJECT_MATCH", func(t *testing.T) {
		assertExpressionStatusWithFacts(
			t,
			RuleExpression{Operator: OperatorSubjectMatch},
			MajorUnknown,
			[]ProfileFact{{FieldKey: FieldCategory, State: ProfileProvided, IntValue: 1}},
		)
	})
}

func TestEvaluateEligibility_PresentHonorsUnavailableFacts(t *testing.T) {
	for _, state := range []string{ProfileUnknown, ProfileDeclined} {
		t.Run(state, func(t *testing.T) {
			assertExpressionStatusWithFacts(
				t,
				RuleExpression{Operator: OperatorPRESENT, Field: "gender"},
				MajorMissingRequiredFact,
				[]ProfileFact{{FieldKey: "gender", State: state}},
			)
		})
	}
	t.Run("type mismatch", func(t *testing.T) {
		assertExpressionStatusWithFacts(
			t,
			RuleExpression{Operator: OperatorPRESENT, Field: "gender"},
			MajorUnknown,
			[]ProfileFact{{FieldKey: "gender", State: ProfileProvided, IntValue: 1}},
		)
	})
}

func TestEvaluateEligibility_UnavailableFactsNeverPass(t *testing.T) {
	for _, state := range []string{ProfileUnknown, ProfileDeclined} {
		t.Run(state, func(t *testing.T) {
			assertExpressionStatusWithFacts(
				t,
				RuleExpression{
					Operator: OperatorEQ,
					Field:    "gender",
					Value:    expressionLiteral(t, "FEMALE"),
				},
				MajorMissingRequiredFact,
				[]ProfileFact{{FieldKey: "gender", State: state}},
			)
		})
	}
}

func TestEvaluateEligibility_DeterministicFailDominatesUnknownFact(t *testing.T) {
	failRule := eligibilityExpressionRule(RuleExpression{
		Operator: OperatorEQ,
		Field:    FieldCategory,
		Value:    expressionLiteral(t, "物理类"),
	}, []string{FieldCategory})
	failRule.RuleKey = "a-fail"
	missingRule := eligibilityExpressionRule(RuleExpression{
		Operator: OperatorPRESENT,
		Field:    "gender",
	}, []string{"gender"})
	missingRule.RuleKey = "b-missing"

	result := evaluateEligibilityWithRules(t, []RuleVersion{failRule, missingRule})
	got := result.Groups[0].Majors[0]
	if got.Status != MajorFail {
		t.Fatalf("major status = %q, want FAIL; unknown=%v missing=%v",
			got.Status, got.UnknownReasons, got.MissingFields)
	}
	if len(got.FailureRuleKeys) != 1 || got.FailureRuleKeys[0] != "a-fail" {
		t.Fatalf("failure rules = %v", got.FailureRuleKeys)
	}
}

func TestEvaluateEligibility_IgnoresInvalidDraftExpression(t *testing.T) {
	published := eligibilityExpressionRule(RuleExpression{
		Operator: OperatorEQ,
		Field:    FieldCategory,
		Value:    expressionLiteral(t, "历史类"),
	}, []string{FieldCategory})
	published.RuleKey = "a-published"
	draft := eligibilityExpressionRule(RuleExpression{
		Operator: "SQL",
		Field:    "secret",
	}, []string{"secret"})
	draft.RuleKey = "b-draft"
	draft.ReviewState = "DRAFT"

	result := evaluateEligibilityWithRules(t, []RuleVersion{published, draft})
	if result.Status != EligibilityComplete {
		t.Fatalf("status = %q, want COMPLETE", result.Status)
	}
	if got := result.Groups[0].Majors[0].Status; got != MajorPass {
		t.Fatalf("major status = %q, want PASS", got)
	}
}

func TestEvaluateEligibility_BlocksInvalidExpressionContract(t *testing.T) {
	valid := RuleExpression{
		Operator: OperatorEQ,
		Field:    FieldCategory,
		Value:    expressionLiteral(t, "物理类"),
	}
	invalidArity := valid
	invalidArity.Child = &valid
	unknownField := RuleExpression{
		Operator: OperatorEQ,
		Field:    "sql",
		Value:    expressionLiteral(t, "SELECT 1"),
	}
	deep := valid
	for range 17 {
		child := deep
		deep = RuleExpression{Operator: OperatorNot, Child: &child}
	}
	manyNodes := expressionTree(66)

	cases := []struct {
		name              string
		expression        RuleExpression
		expressionVersion int
		requiredFields    []string
		reason            string
	}{
		{
			name: "unsupported operator",
			expression: RuleExpression{
				Operator: "SQL", Field: FieldCategory,
			},
			expressionVersion: ExpressionVersionV1,
			reason:            ReasonRuleExpression,
		},
		{
			name:              "unknown field",
			expression:        unknownField,
			expressionVersion: ExpressionVersionV1,
			requiredFields:    []string{"sql"},
			reason:            ReasonRuleUnknownField,
		},
		{
			name: "unknown single-subject field",
			expression: RuleExpression{
				Operator: OperatorGTE,
				Field:    "single_subject_scores.数学",
				Value:    expressionLiteral(t, 120),
			},
			expressionVersion: ExpressionVersionV1,
			requiredFields:    []string{"single_subject_scores.数学"},
			reason:            ReasonRuleUnknownField,
		},
		{
			name:              "required field mismatch",
			expression:        valid,
			expressionVersion: ExpressionVersionV1,
			requiredFields:    []string{},
			reason:            ReasonRuleExpression,
		},
		{
			name:              "invalid arity",
			expression:        invalidArity,
			expressionVersion: ExpressionVersionV1,
			requiredFields:    []string{FieldCategory},
			reason:            ReasonRuleExpression,
		},
		{
			name: "IN mixed literal types",
			expression: RuleExpression{
				Operator: OperatorIN,
				Field:    FieldCategory,
				Values:   []json.RawMessage{expressionLiteral(t, "物理类"), expressionLiteral(t, 1)},
			},
			expressionVersion: ExpressionVersionV1,
			requiredFields:    []string{FieldCategory},
			reason:            ReasonRuleExpression,
		},
		{
			name: "ordering on string field",
			expression: RuleExpression{
				Operator: OperatorGTE,
				Field:    FieldCategory,
				Value:    expressionLiteral(t, 600),
			},
			expressionVersion: ExpressionVersionV1,
			requiredFields:    []string{FieldCategory},
			reason:            ReasonRuleExpression,
		},
		{
			name: "contains on scalar field",
			expression: RuleExpression{
				Operator: OperatorCONTAINS_ALL,
				Field:    FieldCategory,
				Values:   []json.RawMessage{expressionLiteral(t, "物理类")},
			},
			expressionVersion: ExpressionVersionV1,
			requiredFields:    []string{FieldCategory},
			reason:            ReasonRuleExpression,
		},
		{
			name: "PRESENT with value",
			expression: RuleExpression{
				Operator: OperatorPRESENT,
				Field:    FieldCategory,
				Value:    expressionLiteral(t, "物理类"),
			},
			expressionVersion: ExpressionVersionV1,
			requiredFields:    []string{FieldCategory},
			reason:            ReasonRuleExpression,
		},
		{
			name:              "unsupported expression version",
			expression:        valid,
			expressionVersion: 2,
			requiredFields:    []string{FieldCategory},
			reason:            ReasonRuleExpression,
		},
		{
			name:              "depth limit",
			expression:        deep,
			expressionVersion: ExpressionVersionV1,
			requiredFields:    []string{FieldCategory},
			reason:            ReasonRuleExpression,
		},
		{
			name:              "node limit",
			expression:        manyNodes,
			expressionVersion: ExpressionVersionV1,
			requiredFields:    []string{FieldCategory},
			reason:            ReasonRuleExpression,
		},
		{
			name: "string limit",
			expression: RuleExpression{
				Operator: OperatorEQ,
				Field:    FieldCategory,
				Value:    expressionLiteral(t, strings.Repeat("x", maxExpressionStringLength+1)),
			},
			expressionVersion: ExpressionVersionV1,
			requiredFields:    []string{FieldCategory},
			reason:            ReasonRuleExpression,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rule := eligibilityExpressionRule(tc.expression, tc.requiredFields)
			rule.ExpressionVersion = tc.expressionVersion
			result := evaluateEligibilityWithRule(t, rule)
			if result.Status != EligibilityBlocked {
				t.Fatalf("status = %q, want BLOCKED", result.Status)
			}
			reasons := result.Groups[0].Majors[0].UnknownReasons
			if len(reasons) != 1 || reasons[0] != tc.reason {
				t.Fatalf("unknown reasons = %v, want [%s]", reasons, tc.reason)
			}
		})
	}
}

func assertExpressionStatus(t *testing.T, expression RuleExpression, expected string, extraFacts ...ProfileFact) {
	t.Helper()
	assertExpressionStatusWithFacts(t, expression, expected, extraFacts)
}

func assertExpressionStatusWithFacts(t *testing.T, expression RuleExpression, expected string, extraFacts []ProfileFact) {
	t.Helper()
	requiredFields := expressionFields(expression, nil)
	rule := eligibilityExpressionRule(expression, stableStrings(requiredFields))
	result := evaluateEligibilityWithRule(t, rule, extraFacts...)

	got := result.Groups[0].Majors[0]
	if got.Status != expected {
		t.Fatalf("major status = %q, want %q; unknown=%v missing=%v",
			got.Status, expected, got.UnknownReasons, got.MissingFields)
	}
}

func eligibilityExpressionRule(expression RuleExpression, requiredFields []string) RuleVersion {
	return RuleVersion{
		RuleID:                "rule-expression",
		Version:               1,
		RuleKey:               "expression-test",
		RuleType:              RuleTypeSubjectRequirement,
		RequiredProfileFields: requiredFields,
		When:                  expression,
		OutcomeWhenTrue:       OutcomeFail,
		ReviewState:           ReviewStatePublished,
		ExpressionVersion:     ExpressionVersionV1,
	}
}

func evaluateEligibilityWithRule(t *testing.T, rule RuleVersion, extraFacts ...ProfileFact) EligibilityResult {
	t.Helper()
	return evaluateEligibilityWithRules(t, []RuleVersion{rule}, extraFacts...)
}

func evaluateEligibilityWithRules(t *testing.T, rules []RuleVersion, extraFacts ...ProfileFact) EligibilityResult {
	t.Helper()
	ruleSet := EligibilityRuleSet{
		ReleaseStatus: "PUBLISHED",
		Rules:         rules,
		Coverage: []RuleCoverage{{
			RuleType: RuleTypeSubjectRequirement,
			Status:   CoverageFull,
		}},
	}
	uc := NewEligibilityUsecase(fakeEligibilityRepo{set: ruleSet})
	request := eligibilityFixtureRequest()
	request.ProfileFacts = append(request.ProfileFacts, extraFacts...)
	result, err := uc.EvaluateEligibility(context.Background(), request)
	if err != nil {
		t.Fatalf("EvaluateEligibility: %v", err)
	}
	return result
}

func expressionFields(expression RuleExpression, fields []string) []string {
	if expression.Field != "" {
		fields = append(fields, expression.Field)
	}
	if expression.Operator == OperatorSubjectMatch {
		fields = append(fields, FieldSubjects, FieldCategory)
	}
	if expression.Left != nil {
		fields = expressionFields(*expression.Left, fields)
	}
	if expression.Right != nil {
		fields = expressionFields(*expression.Right, fields)
	}
	if expression.Child != nil {
		fields = expressionFields(*expression.Child, fields)
	}
	return fields
}

func kleeneDecision(operator, left, right string) string {
	switch operator {
	case OperatorAND:
		if left == "NO_MATCH" || right == "NO_MATCH" {
			return "NO_MATCH"
		}
		if left == "MATCH" && right == "MATCH" {
			return "MATCH"
		}
		return "INDETERMINATE"
	case OperatorOR:
		if left == "MATCH" || right == "MATCH" {
			return "MATCH"
		}
		if left == "NO_MATCH" && right == "NO_MATCH" {
			return "NO_MATCH"
		}
		return "INDETERMINATE"
	default:
		return "INDETERMINATE"
	}
}

func invertDecision(decision string) string {
	switch decision {
	case "MATCH":
		return "NO_MATCH"
	case "NO_MATCH":
		return "MATCH"
	default:
		return "INDETERMINATE"
	}
}

func majorStatusForDecision(decision string) string {
	switch decision {
	case "MATCH":
		return MajorFail
	case "NO_MATCH":
		return MajorPass
	default:
		return MajorMissingRequiredFact
	}
}

func expressionLiteral(t *testing.T, value any) json.RawMessage {
	t.Helper()
	raw, err := json.Marshal(value)
	if err != nil {
		t.Fatalf("marshal expression literal: %v", err)
	}
	return raw
}

func profileFactInt(key string, value int) ProfileFact {
	return ProfileFact{FieldKey: key, State: ProfileProvided, IntValue: value}
}

func expressionTree(nodes int) RuleExpression {
	leaf := RuleExpression{
		Operator: OperatorEQ,
		Field:    FieldCategory,
		Value:    json.RawMessage(`"物理类"`),
	}
	if nodes <= 1 {
		return leaf
	}
	left := leaf
	right := leaf
	root := RuleExpression{Operator: OperatorAND, Left: &left, Right: &right}
	for i := 3; i < nodes; i++ {
		next := root
		root = RuleExpression{Operator: OperatorAND, Left: &next, Right: &left}
	}
	return root
}
