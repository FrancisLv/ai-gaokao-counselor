package biz

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"slices"
	"strconv"
	"strings"
)

func (expr *RuleExpression) UnmarshalJSON(data []byte) error {
	type wireRuleExpression RuleExpression
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	var decoded wireRuleExpression
	if err := decoder.Decode(&decoded); err != nil {
		return err
	}
	var extra any
	if err := decoder.Decode(&extra); err != io.EOF {
		if err == nil {
			return fmt.Errorf("表达式 JSON 后存在额外内容")
		}
		return err
	}
	*expr = RuleExpression(decoded)
	return nil
}

const (
	maxExpressionDepth         = 16
	maxExpressionNodes         = 64
	maxExpressionValues        = 32
	maxExpressionStringLength  = 256
	maxExpressionJSONByteCount = 32 * 1024
)

type expressionDecision string

const (
	expressionMatch         expressionDecision = "MATCH"
	expressionNoMatch       expressionDecision = "NO_MATCH"
	expressionIndeterminate expressionDecision = "INDETERMINATE"
)

type expressionOutcome struct {
	decision expressionDecision
	missing  []string
}

func validateEligibilityRules(rules []RuleVersion) string {
	for _, rule := range rules {
		if rule.ReviewState != ReviewStatePublished {
			continue
		}
		if rule.ValidationReason != "" {
			return rule.ValidationReason
		}
		if rule.ExpressionVersion != ExpressionVersionV1 {
			return ReasonRuleExpression
		}
		raw, err := json.Marshal(rule.When)
		if err != nil || len(raw) > maxExpressionJSONByteCount {
			return ReasonRuleExpression
		}
		nodes := 0
		fields := make([]string, 0)
		if reason := validateRuleExpression(rule.When, 1, &nodes, &fields); reason != "" {
			return reason
		}
		if !sameProfileFieldSet(rule.RequiredProfileFields, fields) {
			return ReasonRuleExpression
		}
	}
	return ""
}

func validateRuleExpression(expr RuleExpression, depth int, nodes *int, fields *[]string) string {
	if depth > maxExpressionDepth {
		return ReasonRuleExpression
	}
	(*nodes)++
	if *nodes > maxExpressionNodes {
		return ReasonRuleExpression
	}
	if len(expr.Field) > maxExpressionStringLength ||
		len(expr.SubjectRequirement) > maxExpressionStringLength {
		return ReasonRuleExpression
	}
	if len(expr.Values) > maxExpressionValues {
		return ReasonRuleExpression
	}

	switch expr.Operator {
	case OperatorAND, OperatorOR:
		if !emptyExceptBoolChildren(expr) || expr.Left == nil || expr.Right == nil {
			return ReasonRuleExpression
		}
		if reason := validateRuleExpression(*expr.Left, depth+1, nodes, fields); reason != "" {
			return reason
		}
		return validateRuleExpression(*expr.Right, depth+1, nodes, fields)
	case OperatorNot:
		if !emptyExceptChild(expr) || expr.Child == nil {
			return ReasonRuleExpression
		}
		return validateRuleExpression(*expr.Child, depth+1, nodes, fields)
	case OperatorEQ, OperatorNEQ:
		if !emptyExceptFieldAndValue(expr) || len(expr.Value) == 0 {
			return ReasonRuleExpression
		}
		kind, ok := profileFieldType(expr.Field)
		if !ok {
			return ReasonRuleUnknownField
		}
		if kind == profileFieldString {
			value, err := decodeStringLiteral(expr.Value)
			if err != nil || len(value) > maxExpressionStringLength {
				return ReasonRuleExpression
			}
		} else if kind == profileFieldInt {
			if _, err := decodeIntLiteral(expr.Value); err != nil {
				return ReasonRuleExpression
			}
		} else {
			return ReasonRuleExpression
		}
		*fields = append(*fields, expr.Field)
		return ""
	case OperatorIN:
		if !emptyExceptFieldAndValues(expr) || len(expr.Values) == 0 {
			return ReasonRuleExpression
		}
		kind, ok := profileFieldType(expr.Field)
		if !ok {
			return ReasonRuleUnknownField
		}
		if kind == profileFieldString {
			values, err := decodeStringValues(expr.Values)
			if err != nil || !allStringsWithinLimit(values, maxExpressionStringLength) {
				return ReasonRuleExpression
			}
		} else if kind == profileFieldInt {
			for _, raw := range expr.Values {
				if _, err := decodeIntLiteral(raw); err != nil {
					return ReasonRuleExpression
				}
			}
		} else {
			return ReasonRuleExpression
		}
		*fields = append(*fields, expr.Field)
		return ""
	case OperatorGTE, OperatorLTE:
		if !emptyExceptFieldAndValue(expr) || len(expr.Value) == 0 {
			return ReasonRuleExpression
		}
		kind, ok := profileFieldType(expr.Field)
		if !ok {
			return ReasonRuleUnknownField
		}
		if kind != profileFieldInt {
			return ReasonRuleExpression
		}
		if _, err := decodeIntLiteral(expr.Value); err != nil {
			return ReasonRuleExpression
		}
		*fields = append(*fields, expr.Field)
		return ""
	case OperatorPRESENT:
		if !emptyExceptField(expr) {
			return ReasonRuleExpression
		}
		if _, ok := profileFieldType(expr.Field); !ok {
			return ReasonRuleUnknownField
		}
		*fields = append(*fields, expr.Field)
		return ""
	case OperatorCONTAINS_ALL, OperatorCONTAINS_ANY:
		if !emptyExceptFieldAndValues(expr) || len(expr.Values) == 0 {
			return ReasonRuleExpression
		}
		kind, ok := profileFieldType(expr.Field)
		if !ok {
			return ReasonRuleUnknownField
		}
		if kind != profileFieldStringList {
			return ReasonRuleExpression
		}
		values, err := decodeStringValues(expr.Values)
		if err != nil || !allStringsWithinLimit(values, maxExpressionStringLength) {
			return ReasonRuleExpression
		}
		*fields = append(*fields, expr.Field)
		return ""
	case OperatorSubjectMatch:
		if expr.Field != "" || len(expr.Value) > 0 || len(expr.Values) > 0 ||
			expr.Child != nil || expr.Left != nil || expr.Right != nil {
			return ReasonRuleExpression
		}
		if expr.SubjectRequirement != "" {
			if _, _, err := matchCandidateSubjects(
				[]string{"物理", "化学", "生物"},
				[]string{expr.SubjectRequirement},
			); err != nil {
				return ReasonRuleExpression
			}
		}
		*fields = append(*fields, FieldSubjects, FieldCategory)
		return ""
	default:
		return ReasonRuleExpression
	}
}

func emptyExceptBoolChildren(expr RuleExpression) bool {
	return expr.Field == "" && len(expr.Value) == 0 && len(expr.Values) == 0 &&
		expr.SubjectRequirement == "" && expr.Child == nil
}

func emptyExceptChild(expr RuleExpression) bool {
	return expr.Field == "" && len(expr.Value) == 0 && len(expr.Values) == 0 &&
		expr.SubjectRequirement == "" && expr.Left == nil && expr.Right == nil
}

func emptyExceptFieldAndValue(expr RuleExpression) bool {
	return expr.Field != "" && len(expr.Values) == 0 && expr.SubjectRequirement == "" &&
		expr.Child == nil && expr.Left == nil && expr.Right == nil
}

func emptyExceptFieldAndValues(expr RuleExpression) bool {
	return expr.Field != "" && len(expr.Value) == 0 && expr.SubjectRequirement == "" &&
		expr.Child == nil && expr.Left == nil && expr.Right == nil
}

func emptyExceptField(expr RuleExpression) bool {
	return expr.Field != "" && len(expr.Value) == 0 && len(expr.Values) == 0 &&
		expr.SubjectRequirement == "" && expr.Child == nil && expr.Left == nil && expr.Right == nil
}

func sameProfileFieldSet(declared []string, referenced []string) bool {
	declaredSet := stableStrings(declared)
	if len(declaredSet) != len(declared) {
		return false
	}
	referencedSet := stableStrings(referenced)
	return slices.Equal(declaredSet, referencedSet)
}

func allStringsWithinLimit(values []string, limit int) bool {
	for _, value := range values {
		if len(value) > limit {
			return false
		}
	}
	return true
}

func evaluateExpression(expr RuleExpression, major CandidateMajor, facts map[string]ProfileFact) (string, []string) {
	outcome := evaluateRuleExpression(expr, major, facts)
	return string(outcome.decision), outcome.missing
}

func evaluateRuleExpression(expr RuleExpression, major CandidateMajor, facts map[string]ProfileFact) expressionOutcome {
	switch expr.Operator {
	case OperatorAND:
		return evaluateAnd(expr, major, facts)
	case OperatorOR:
		return evaluateOr(expr, major, facts)
	case OperatorNot:
		if expr.Child == nil {
			return expressionOutcome{decision: expressionIndeterminate}
		}
		child := evaluateRuleExpression(*expr.Child, major, facts)
		switch child.decision {
		case expressionMatch:
			child.decision = expressionNoMatch
		case expressionNoMatch:
			child.decision = expressionMatch
		default:
			child.decision = expressionIndeterminate
		}
		return child
	case OperatorSubjectMatch:
		return evaluateSubjectMatch(expr, major, facts)
	case OperatorEQ, OperatorNEQ:
		return evaluateEquality(expr, facts)
	case OperatorIN:
		return evaluateMembership(expr, facts)
	case OperatorGTE, OperatorLTE:
		return evaluateOrdering(expr, facts)
	case OperatorPRESENT:
		return evaluatePresent(expr, facts)
	case OperatorCONTAINS_ALL, OperatorCONTAINS_ANY:
		return evaluateContains(expr, facts)
	default:
		return expressionOutcome{decision: expressionIndeterminate}
	}
}

func evaluateAnd(expr RuleExpression, major CandidateMajor, facts map[string]ProfileFact) expressionOutcome {
	if expr.Left == nil || expr.Right == nil {
		return expressionOutcome{decision: expressionIndeterminate}
	}
	left := evaluateRuleExpression(*expr.Left, major, facts)
	right := evaluateRuleExpression(*expr.Right, major, facts)
	if left.decision == expressionNoMatch || right.decision == expressionNoMatch {
		return expressionOutcome{decision: expressionNoMatch}
	}
	if left.decision == expressionMatch && right.decision == expressionMatch {
		return expressionOutcome{decision: expressionMatch}
	}
	return expressionOutcome{
		decision: expressionIndeterminate,
		missing:  stableStrings(append(left.missing, right.missing...)),
	}
}

func evaluateOr(expr RuleExpression, major CandidateMajor, facts map[string]ProfileFact) expressionOutcome {
	if expr.Left == nil || expr.Right == nil {
		return expressionOutcome{decision: expressionIndeterminate}
	}
	left := evaluateRuleExpression(*expr.Left, major, facts)
	right := evaluateRuleExpression(*expr.Right, major, facts)
	if left.decision == expressionMatch || right.decision == expressionMatch {
		return expressionOutcome{decision: expressionMatch}
	}
	if left.decision == expressionNoMatch && right.decision == expressionNoMatch {
		return expressionOutcome{decision: expressionNoMatch}
	}
	return expressionOutcome{
		decision: expressionIndeterminate,
		missing:  stableStrings(append(left.missing, right.missing...)),
	}
}

func evaluateSubjectMatch(expr RuleExpression, major CandidateMajor, facts map[string]ProfileFact) expressionOutcome {
	missing := make([]string, 0, 2)
	subjectsFact, subjectsExists := facts[FieldSubjects]
	if !subjectsExists || !usableProfileState(subjectsFact.State) {
		missing = append(missing, FieldSubjects)
	}
	categoryFact, categoryExists := facts[FieldCategory]
	if !categoryExists || !usableProfileState(categoryFact.State) {
		missing = append(missing, FieldCategory)
	}
	if len(missing) > 0 {
		return expressionOutcome{
			decision: expressionIndeterminate,
			missing:  stableStrings(missing),
		}
	}
	subjects, subjectsState := resolveStringListFact(subjectsFact)
	_, categoryState := resolveStringFact(categoryFact)
	if subjectsState != factResolved || categoryState != factResolved {
		return expressionOutcome{decision: expressionIndeterminate}
	}
	requirements := []string{major.SubjectRequirement}
	if strings.TrimSpace(expr.SubjectRequirement) != "" {
		requirements = []string{expr.SubjectRequirement}
	}
	matched, _, parseErr := matchCandidateSubjects(subjects, requirements)
	if parseErr != nil {
		return expressionOutcome{decision: expressionIndeterminate}
	}
	if matched {
		return expressionOutcome{decision: expressionMatch}
	}
	return expressionOutcome{decision: expressionNoMatch}
}

func evaluateEquality(expr RuleExpression, facts map[string]ProfileFact) expressionOutcome {
	field, ok := profileFieldType(expr.Field)
	if !ok || len(expr.Value) == 0 {
		return expressionOutcome{decision: expressionIndeterminate}
	}
	fact, exists := facts[expr.Field]
	if !exists || !usableProfileState(fact.State) {
		return expressionOutcome{
			decision: expressionIndeterminate,
			missing:  []string{expr.Field},
		}
	}

	var equal bool
	switch field {
	case profileFieldString:
		actual, state := resolveStringFact(fact)
		expected, err := decodeStringLiteral(expr.Value)
		if state != factResolved || err != nil {
			return expressionOutcome{decision: expressionIndeterminate}
		}
		equal = actual == expected
	case profileFieldInt:
		actual, state := resolveIntFact(fact)
		expected, err := decodeIntLiteral(expr.Value)
		if state != factResolved || err != nil {
			return expressionOutcome{decision: expressionIndeterminate}
		}
		equal = actual == expected
	default:
		return expressionOutcome{decision: expressionIndeterminate}
	}

	if expr.Operator == OperatorNEQ {
		equal = !equal
	}
	if equal {
		return expressionOutcome{decision: expressionMatch}
	}
	return expressionOutcome{decision: expressionNoMatch}
}

func evaluateMembership(expr RuleExpression, facts map[string]ProfileFact) expressionOutcome {
	field, ok := profileFieldType(expr.Field)
	if !ok || len(expr.Values) == 0 {
		return expressionOutcome{decision: expressionIndeterminate}
	}
	fact, exists := facts[expr.Field]
	if !exists || !usableProfileState(fact.State) {
		return expressionOutcome{decision: expressionIndeterminate, missing: []string{expr.Field}}
	}

	matched := false
	switch field {
	case profileFieldString:
		actual, state := resolveStringFact(fact)
		if state != factResolved {
			return expressionOutcome{decision: expressionIndeterminate}
		}
		for _, raw := range expr.Values {
			expected, err := decodeStringLiteral(raw)
			if err != nil {
				return expressionOutcome{decision: expressionIndeterminate}
			}
			if actual == expected {
				matched = true
			}
		}
	case profileFieldInt:
		actual, state := resolveIntFact(fact)
		if state != factResolved {
			return expressionOutcome{decision: expressionIndeterminate}
		}
		for _, raw := range expr.Values {
			expected, err := decodeIntLiteral(raw)
			if err != nil {
				return expressionOutcome{decision: expressionIndeterminate}
			}
			if actual == expected {
				matched = true
			}
		}
	default:
		return expressionOutcome{decision: expressionIndeterminate}
	}
	if matched {
		return expressionOutcome{decision: expressionMatch}
	}
	return expressionOutcome{decision: expressionNoMatch}
}

func evaluateOrdering(expr RuleExpression, facts map[string]ProfileFact) expressionOutcome {
	if field, ok := profileFieldType(expr.Field); !ok || field != profileFieldInt || len(expr.Value) == 0 {
		return expressionOutcome{decision: expressionIndeterminate}
	}
	fact, exists := facts[expr.Field]
	if !exists || !usableProfileState(fact.State) {
		return expressionOutcome{decision: expressionIndeterminate, missing: []string{expr.Field}}
	}
	actual, state := resolveIntFact(fact)
	expected, err := decodeIntLiteral(expr.Value)
	if state != factResolved || err != nil {
		return expressionOutcome{decision: expressionIndeterminate}
	}
	matched := actual >= expected
	if expr.Operator == OperatorLTE {
		matched = actual <= expected
	}
	if matched {
		return expressionOutcome{decision: expressionMatch}
	}
	return expressionOutcome{decision: expressionNoMatch}
}

func evaluatePresent(expr RuleExpression, facts map[string]ProfileFact) expressionOutcome {
	field, ok := profileFieldType(expr.Field)
	if !ok {
		return expressionOutcome{decision: expressionIndeterminate}
	}
	fact, exists := facts[expr.Field]
	if !exists || !usableProfileState(fact.State) {
		return expressionOutcome{decision: expressionIndeterminate, missing: []string{expr.Field}}
	}
	switch field {
	case profileFieldString:
		_, state := resolveStringFact(fact)
		if state == factResolved {
			return expressionOutcome{decision: expressionMatch}
		}
	case profileFieldInt:
		_, state := resolveIntFact(fact)
		if state == factResolved {
			return expressionOutcome{decision: expressionMatch}
		}
	case profileFieldStringList:
		_, state := resolveStringListFact(fact)
		if state == factResolved {
			return expressionOutcome{decision: expressionMatch}
		}
	}
	return expressionOutcome{decision: expressionIndeterminate}
}

func evaluateContains(expr RuleExpression, facts map[string]ProfileFact) expressionOutcome {
	if field, ok := profileFieldType(expr.Field); !ok || field != profileFieldStringList || len(expr.Values) == 0 {
		return expressionOutcome{decision: expressionIndeterminate}
	}
	fact, exists := facts[expr.Field]
	if !exists || !usableProfileState(fact.State) {
		return expressionOutcome{decision: expressionIndeterminate, missing: []string{expr.Field}}
	}
	actual, state := resolveStringListFact(fact)
	if state != factResolved {
		return expressionOutcome{decision: expressionIndeterminate}
	}
	expected, err := decodeStringValues(expr.Values)
	if err != nil {
		return expressionOutcome{decision: expressionIndeterminate}
	}
	present := make(map[string]struct{}, len(actual))
	for _, value := range actual {
		present[value] = struct{}{}
	}
	matchedCount := 0
	for _, value := range expected {
		if _, ok := present[value]; ok {
			matchedCount++
		}
	}
	matched := matchedCount == len(expected)
	if expr.Operator == OperatorCONTAINS_ANY {
		matched = matchedCount > 0
	}
	if matched {
		return expressionOutcome{decision: expressionMatch}
	}
	return expressionOutcome{decision: expressionNoMatch}
}

type profileFieldKind int

const (
	profileFieldUnknown profileFieldKind = iota
	profileFieldString
	profileFieldInt
	profileFieldStringList
)

func profileFieldType(field string) (profileFieldKind, bool) {
	switch field {
	case FieldCategory, "province", "batch", "physical_exam.color_vision", "foreign_language",
		"gender", "graduate_status", "household_province", "school_registration_province":
		return profileFieldString, true
	case FieldApplicationYear, "score", "rank":
		return profileFieldInt, true
	case FieldSubjects:
		return profileFieldStringList, true
	default:
		if strings.HasPrefix(field, "single_subject_scores.") {
			subject := strings.TrimPrefix(field, "single_subject_scores.")
			if subject == firstPhysics || subject == firstHistory || isReselect(subject) {
				return profileFieldInt, true
			}
		}
		return profileFieldUnknown, false
	}
}

type factResolution int

const (
	factMissing factResolution = iota
	factInvalid
	factResolved
)

func resolveStringFact(fact ProfileFact) (string, factResolution) {
	if strings.TrimSpace(fact.StringValue) == "" || len(fact.StringValues) > 0 || fact.IntValue != 0 {
		return "", factInvalid
	}
	return fact.StringValue, factResolved
}

func resolveIntFact(fact ProfileFact) (int, factResolution) {
	if fact.StringValue != "" || len(fact.StringValues) > 0 {
		return 0, factInvalid
	}
	return fact.IntValue, factResolved
}

func resolveStringListFact(fact ProfileFact) ([]string, factResolution) {
	if fact.StringValue != "" || fact.IntValue != 0 || len(fact.StringValues) == 0 {
		return nil, factInvalid
	}
	return append([]string(nil), fact.StringValues...), factResolved
}

func decodeStringLiteral(raw json.RawMessage) (string, error) {
	var value string
	decoder := json.NewDecoder(bytes.NewReader(raw))
	if err := decoder.Decode(&value); err != nil {
		return "", err
	}
	if strings.TrimSpace(value) == "" {
		return "", fmt.Errorf("空字符串")
	}
	return value, nil
}

func decodeIntLiteral(raw json.RawMessage) (int, error) {
	var number json.Number
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if err := decoder.Decode(&number); err != nil {
		return 0, err
	}
	value, err := strconv.Atoi(number.String())
	if err != nil {
		return 0, err
	}
	return value, nil
}

func decodeStringValues(values []json.RawMessage) ([]string, error) {
	out := make([]string, 0, len(values))
	for _, raw := range values {
		value, err := decodeStringLiteral(raw)
		if err != nil {
			return nil, err
		}
		out = append(out, value)
	}
	return out, nil
}
