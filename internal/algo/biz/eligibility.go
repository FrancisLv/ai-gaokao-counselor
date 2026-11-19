package biz

import (
	"context"
	"encoding/json"
	"sort"
	"strconv"
	"strings"

	errs "github.com/choucisan/ai-gaokao-counselor/internal/pkg/errors"
)

// EligibilityRepo 读取 release 内已发布的规则与覆盖快照。规则解释和裁决不进入 data 层。
type EligibilityRepo interface {
	LoadEligibilityRuleSet(ctx context.Context, releaseID string) (EligibilityRuleSet, error)
}

// EligibilityRuleSet 是单个 release 的只读资格规则快照。
type EligibilityRuleSet struct {
	ReleaseStatus string
	Rules         []RuleVersion
	Coverage      []RuleCoverage
	ProfileFields []ProfileFieldDefinition
	RevokedRules  []RevokedRuleVersion
}

type RevokedRuleVersion struct {
	RuleKey  string
	RuleType string
	Scope    RuleScope
}

type EligibilityScope struct {
	Province        string
	ApplicationYear int
	Category        string
	Batch           string
	UniversityID    int
	MajorGroup      string
	MajorCode       string
}

type RuleScope struct {
	Province        string `json:"province,omitempty"`
	ApplicationYear int    `json:"application_year,omitempty"`
	Category        string `json:"category,omitempty"`
	Batch           string `json:"batch,omitempty"`
	UniversityID    int    `json:"university_id,omitempty"`
	MajorGroup      string `json:"major_group,omitempty"`
	MajorCode       string `json:"major_code,omitempty"`
}

type RuleExpression struct {
	Operator           string            `json:"op"`
	Field              string            `json:"field,omitempty"`
	Value              json.RawMessage   `json:"value,omitempty"`
	Values             []json.RawMessage `json:"values,omitempty"`
	SubjectRequirement string            `json:"subject_req,omitempty"`
	Child              *RuleExpression   `json:"child,omitempty"`
	Left               *RuleExpression   `json:"left,omitempty"`
	Right              *RuleExpression   `json:"right,omitempty"`
}

type EvidenceRef struct {
	EvidenceID   string `json:"evidence_id"`
	EvidenceType string `json:"evidence_type"`
	ReleaseID    string `json:"release_id"`
	ContentSHA   string `json:"content_sha256"`
}

type RuleVersion struct {
	RuleID                string
	Version               int
	RuleKey               string
	RuleType              string
	Scope                 RuleScope
	RequiredProfileFields []string
	When                  RuleExpression
	OutcomeWhenTrue       string
	ReasonCode            string
	ReviewState           string
	ExpressionVersion     int
	ValidationReason      string
	SourceEvidence        []EvidenceRef
}

type RuleCoverage struct {
	RuleType           string
	Scope              RuleScope
	Status             string
	CoveredDataSources []string
	Notes              string
}

type ProfileFact struct {
	FieldKey      string
	State         string
	StringValue   string
	IntValue      int
	StringValues  []string
	fieldKind     profileFieldKind
	allowedValues []string
}

type ProfileFieldDefinition struct {
	FieldKey      string
	ValueType     string
	AllowedValues []string
}

// BuiltinEligibilityProfileFields 仅用于无数据库的业务单测和测试替身；
// 生产评估以规则仓库读取的 eligibility_profile_field 目录为准。
func BuiltinEligibilityProfileFields() []ProfileFieldDefinition {
	return []ProfileFieldDefinition{
		{FieldKey: "province", ValueType: ProfileFieldTypeString, AllowedValues: []string{"湖南"}},
		{FieldKey: FieldApplicationYear, ValueType: ProfileFieldTypeInt},
		{FieldKey: FieldCategory, ValueType: ProfileFieldTypeString, AllowedValues: []string{"物理类", "历史类"}},
		{FieldKey: "batch", ValueType: ProfileFieldTypeString, AllowedValues: []string{"本科批"}},
		{FieldKey: "score", ValueType: ProfileFieldTypeInt},
		{FieldKey: FieldSubjects, ValueType: ProfileFieldTypeStringList},
		{FieldKey: "rank", ValueType: ProfileFieldTypeInt},
		{FieldKey: FieldColorVision, ValueType: ProfileFieldTypeString, AllowedValues: []string{"NORMAL", "COLOR_WEAK", "COLOR_BLIND"}},
		{FieldKey: "foreign_language", ValueType: ProfileFieldTypeString},
		{FieldKey: FieldGender, ValueType: ProfileFieldTypeString, AllowedValues: []string{"MALE", "FEMALE"}},
		{FieldKey: "graduate_status", ValueType: ProfileFieldTypeString, AllowedValues: []string{"FRESH", "PREVIOUS"}},
		{FieldKey: FieldHouseholdProvince, ValueType: ProfileFieldTypeString},
		{FieldKey: FieldSchoolRegistrationProvince, ValueType: ProfileFieldTypeString},
		{FieldKey: FieldSingleSubjectScoresPrefix + "语文", ValueType: ProfileFieldTypeInt},
		{FieldKey: FieldSingleSubjectScoresPrefix + "数学", ValueType: ProfileFieldTypeInt},
		{FieldKey: FieldSingleSubjectScoresPrefix + "英语", ValueType: ProfileFieldTypeInt},
		{FieldKey: FieldSingleSubjectScoresPrefix + "物理", ValueType: ProfileFieldTypeInt},
		{FieldKey: FieldSingleSubjectScoresPrefix + "历史", ValueType: ProfileFieldTypeInt},
		{FieldKey: FieldSingleSubjectScoresPrefix + "化学", ValueType: ProfileFieldTypeInt},
		{FieldKey: FieldSingleSubjectScoresPrefix + "生物", ValueType: ProfileFieldTypeInt},
		{FieldKey: FieldSingleSubjectScoresPrefix + "思想政治", ValueType: ProfileFieldTypeInt},
		{FieldKey: FieldSingleSubjectScoresPrefix + "地理", ValueType: ProfileFieldTypeInt},
	}
}

type CandidateMajor struct {
	MajorCode          string
	MajorName          string
	SubjectRequirement string
	InCurrentPlan      bool
}

type CandidateGroup struct {
	UniversityID   int
	UniversityCode string
	UniversityName string
	MajorGroup     string
	Majors         []CandidateMajor
}

type EligibilityRequest struct {
	ReleaseID               string
	ConfirmedProfileVersion string
	ProfileLifecycle        string
	Scope                   EligibilityScope
	ProfileFacts            []ProfileFact
	CandidateGroups         []CandidateGroup
}

type EligibilityResult struct {
	Status           string
	Groups           []GroupEligibility
	Coverage         []RuleCoverage
	EvidenceBundleID string
	Diagnostics      EligibilityDiagnostics
}

type EligibilityDiagnostics struct {
	EvaluatedRuleCount int
	ConflictCount      int
	UnknownCount       int
}

type GroupEligibility struct {
	GroupKey     string
	GroupStatus  string
	Majors       []MajorEligibility
	EvidenceRefs []EvidenceRef
}

type MajorEligibility struct {
	MajorCode       string
	Status          string
	FailureRuleKeys []string
	UnknownReasons  []string
	MissingFields   []string
	EvidenceRefs    []EvidenceRef
}

const (
	EligibilityComplete = "COMPLETE"
	EligibilityPartial  = "PARTIAL"
	EligibilityBlocked  = "BLOCKED"
)

const (
	MajorPass                = "PASS"
	MajorFail                = "FAIL"
	MajorUnknown             = "UNKNOWN"
	MajorNotApplicable       = "NOT_APPLICABLE"
	MajorMissingRequiredFact = "MISSING_REQUIRED_FACT"
)

const (
	GroupPass        = "PASS"
	GroupPassPartial = "PASS_PARTIAL"
	GroupFail        = "FAIL"
	GroupUnknown     = "UNKNOWN"
)

const (
	RuleTypeSubjectRequirement = "SUBJECT_REQUIREMENT"
	OperatorSubjectMatch       = "SUBJECT_MATCH"
	OperatorAND                = "AND"
	OperatorOR                 = "OR"
	OperatorNot                = "NOT"
	OperatorEQ                 = "EQ"
	OperatorNEQ                = "NEQ"
	OperatorIN                 = "IN"
	OperatorGTE                = "GTE"
	OperatorLTE                = "LTE"
	OperatorPRESENT            = "PRESENT"
	OperatorCONTAINS_ALL       = "CONTAINS_ALL"
	OperatorCONTAINS_ANY       = "CONTAINS_ANY"
	OutcomeFail                = "FAIL"

	ReviewStateDraft    = "DRAFT"
	ReviewStateReviewed = "REVIEWED"
	ExpressionVersionV1 = 1

	ReviewStatePublished = "PUBLISHED"
	ReviewStateRevoked   = "REVOKED"
	CoverageFull         = "FULL"
	CoveragePartial      = "PARTIAL"
	CoverageNone         = "NONE"

	ProfileLifecycleConfirmed = "CONFIRMED"
	ProfileProvided           = "PROVIDED"
	ProfileUnknown            = "UNKNOWN"
	ProfileDeclined           = "DECLINED"
	ProfileDerived            = "DERIVED"
	ProfileDefault            = "DEFAULT"

	FieldSubjects                   = "subjects"
	FieldCategory                   = "category"
	FieldApplicationYear            = "application_year"
	FieldColorVision                = "physical_exam.color_vision"
	FieldGender                     = "gender"
	FieldHouseholdProvince          = "household_province"
	FieldSchoolRegistrationProvince = "school_registration_province"
	FieldSingleSubjectScoresPrefix  = "single_subject_scores."

	ProfileFieldTypeString     = "STRING"
	ProfileFieldTypeInt        = "INT"
	ProfileFieldTypeStringList = "STRING_LIST"
)

const (
	ReasonRuleCoverageMissing     = "RULE_COVERAGE_MISSING"
	ReasonProfileFactMissing      = "PROFILE_FACT_MISSING"
	ReasonRuleExpression          = "RULE_EXPRESSION_INVALID"
	ReasonRuleConflict            = "RULE_CONFLICT"
	ReasonRuleUnknownField        = "RULE_UNKNOWN_FIELD"
	ReasonReleaseRevoked          = "RELEASE_REVOKED"
	ReasonReleaseUnavailable      = "RELEASE_UNAVAILABLE"
	ReasonCandidateGroupEmpty     = "CANDIDATE_GROUP_EMPTY"
	ReasonCandidateGroupDuplicate = "CANDIDATE_GROUP_DUPLICATE"
	ReasonCandidateGroupInvalid   = "CANDIDATE_GROUP_INVALID"
	ReasonCandidateMajorDuplicate = "CANDIDATE_MAJOR_DUPLICATE"
	ReasonCandidateMajorInvalid   = "CANDIDATE_MAJOR_INVALID"
	ReasonCandidateNotInPlan      = "CANDIDATE_NOT_IN_PLAN"
	ReasonEligibilityScopeInvalid = "ELIGIBILITY_SCOPE_INVALID"
)

type EligibilityUsecase struct {
	repo EligibilityRepo
}

func NewEligibilityUsecase(repo EligibilityRepo) *EligibilityUsecase {
	return &EligibilityUsecase{repo: repo}
}

// EvaluateEligibility 是本模块唯一公开业务 seam。调用方只提供已确认事实快照和候选；
// 规则选择、覆盖检查、表达式求值、状态汇总与稳定排序全部隐藏在这里。
func (u *EligibilityUsecase) EvaluateEligibility(ctx context.Context, req EligibilityRequest) (EligibilityResult, error) {
	if err := ctx.Err(); err != nil {
		return EligibilityResult{}, err
	}
	if strings.TrimSpace(req.ReleaseID) == "" {
		return EligibilityResult{}, errs.ErrInvalidQuery
	}
	if req.ProfileLifecycle != ProfileLifecycleConfirmed || strings.TrimSpace(req.ConfirmedProfileVersion) == "" {
		return EligibilityResult{}, errs.ErrProfileNotConfirmed
	}
	if !validEligibilityScope(req.Scope) {
		result := EligibilityResult{
			Status:           EligibilityBlocked,
			Groups:           blockedGroups(req.CandidateGroups, ReasonEligibilityScopeInvalid),
			EvidenceBundleID: "eligibility:" + req.ReleaseID + ":" + req.ConfirmedProfileVersion,
			Diagnostics: EligibilityDiagnostics{
				UnknownCount: maxInt(1, candidateMajorCount(req.CandidateGroups)),
			},
		}
		return result, nil
	}
	set, err := u.repo.LoadEligibilityRuleSet(ctx, req.ReleaseID)
	if err != nil {
		return EligibilityResult{}, err
	}
	result := EligibilityResult{
		Status:           EligibilityComplete,
		Coverage:         cloneCoverage(set.Coverage),
		EvidenceBundleID: "eligibility:" + req.ReleaseID + ":" + req.ConfirmedProfileVersion,
	}
	if set.ReleaseStatus == "" {
		result.Status = EligibilityBlocked
		result.Diagnostics.UnknownCount = 1
		result.Groups = blockedGroups(req.CandidateGroups, ReasonReleaseUnavailable)
		return result, nil
	}
	if set.ReleaseStatus != "PUBLISHED" {
		result.Status = EligibilityBlocked
		result.Diagnostics.UnknownCount = 1
		result.Groups = blockedGroups(req.CandidateGroups, ReasonReleaseRevoked)
		return result, nil
	}
	for _, group := range req.CandidateGroups {
		if len(group.Majors) == 0 {
			result.Status = EligibilityBlocked
			result.Diagnostics.UnknownCount = 1
			result.Groups = blockedGroups(req.CandidateGroups, ReasonCandidateGroupEmpty)
			return result, nil
		}
		if hasDuplicateMajorCodes(group.Majors) {
			result.Status = EligibilityBlocked
			result.Diagnostics.UnknownCount = candidateMajorCount(req.CandidateGroups)
			result.Groups = blockedGroups(req.CandidateGroups, ReasonCandidateMajorDuplicate)
			return result, nil
		}
		for _, major := range group.Majors {
			if strings.TrimSpace(major.MajorCode) == "" {
				result.Status = EligibilityBlocked
				result.Diagnostics.UnknownCount = candidateMajorCount(req.CandidateGroups)
				result.Groups = blockedGroups(req.CandidateGroups, ReasonCandidateMajorInvalid)
				return result, nil
			}
		}
		if !validCandidateGroupIdentity(group) {
			result.Status = EligibilityBlocked
			result.Diagnostics.UnknownCount = candidateMajorCount(req.CandidateGroups)
			result.Groups = blockedGroups(req.CandidateGroups, ReasonCandidateGroupInvalid)
			return result, nil
		}
	}
	if hasDuplicateGroupKeys(req.Scope, req.CandidateGroups) {
		result.Status = EligibilityBlocked
		result.Diagnostics.UnknownCount = candidateMajorCount(req.CandidateGroups)
		result.Groups = blockedGroups(req.CandidateGroups, ReasonCandidateGroupDuplicate)
		return result, nil
	}

	fieldDefinitions := profileFieldDefinitionIndex(set.ProfileFields)
	facts := profileFactIndex(req.ProfileFacts, fieldDefinitions)
	rules := sortedRules(set.Rules)
	result.Diagnostics.EvaluatedRuleCount = len(rules)
	conflicts := conflictingRuleKeys(rules)
	result.Diagnostics.ConflictCount = len(conflicts)
	if len(conflicts) > 0 {
		result.Status = EligibilityBlocked
		result.Diagnostics.UnknownCount = candidateMajorCount(req.CandidateGroups)
		result.Groups = blockedGroups(req.CandidateGroups, ReasonRuleConflict)
		return result, nil
	}
	if reason := validateEligibilityRules(rules, fieldDefinitions); reason != "" {
		result.Status = EligibilityBlocked
		result.Diagnostics.UnknownCount = candidateMajorCount(req.CandidateGroups)
		result.Groups = blockedGroups(req.CandidateGroups, reason)
		return result, nil
	}
	coverageConflicts := countCoverageConflicts(req, rules, set.Coverage, set.RevokedRules)
	result.Diagnostics.ConflictCount += coverageConflicts
	if coverageConflicts > 0 {
		result.Status = EligibilityBlocked
		result.Diagnostics.UnknownCount = candidateMajorCount(req.CandidateGroups)
		result.Groups = blockedGroups(req.CandidateGroups, ReasonRuleConflict)
		return result, nil
	}
	for _, group := range req.CandidateGroups {
		ground := evaluateGroup(req.Scope, group, rules, set.Coverage, set.RevokedRules, facts)
		result.Groups = append(result.Groups, ground)
	}
	sort.Slice(result.Groups, func(i, j int) bool { return result.Groups[i].GroupKey < result.Groups[j].GroupKey })
	for _, group := range result.Groups {
		for _, major := range group.Majors {
			if major.Status == MajorUnknown || major.Status == MajorMissingRequiredFact {
				result.Diagnostics.UnknownCount++
			}
		}
	}
	if result.Diagnostics.UnknownCount > 0 {
		result.Status = EligibilityPartial
	}
	return result, nil
}

func evaluateGroup(
	scope EligibilityScope,
	group CandidateGroup,
	rules []RuleVersion,
	coverage []RuleCoverage,
	revokedRules []RevokedRuleVersion,
	facts map[string]ProfileFact,
) GroupEligibility {
	out := GroupEligibility{
		GroupKey: eligibilityGroupKey(scope, group),
	}
	majors := append([]CandidateMajor(nil), group.Majors...)
	sort.Slice(majors, func(i, j int) bool { return majors[i].MajorCode < majors[j].MajorCode })
	for _, major := range majors {
		evaluated := evaluateMajor(scope, group, major, rules, coverage, revokedRules, facts)
		out.Majors = append(out.Majors, evaluated)
		out.EvidenceRefs = appendUniqueEvidence(out.EvidenceRefs, evaluated.EvidenceRefs...)
	}
	pass, fail, unknown := 0, 0, 0
	for _, major := range out.Majors {
		switch major.Status {
		case MajorPass:
			pass++
		case MajorFail, MajorNotApplicable:
			fail++
		default:
			unknown++
		}
	}
	switch {
	case unknown > 0:
		out.GroupStatus = GroupUnknown
	case len(out.Majors) > 0 && pass == len(out.Majors):
		out.GroupStatus = GroupPass
	case pass > 0 && fail > 0:
		out.GroupStatus = GroupPassPartial
	default:
		out.GroupStatus = GroupFail
	}
	return out
}

func evaluateMajor(
	scope EligibilityScope,
	group CandidateGroup,
	major CandidateMajor,
	rules []RuleVersion,
	coverage []RuleCoverage,
	revokedRules []RevokedRuleVersion,
	facts map[string]ProfileFact,
) MajorEligibility {
	out := MajorEligibility{MajorCode: major.MajorCode}
	if !major.InCurrentPlan {
		out.Status = MajorNotApplicable
		out.UnknownReasons = []string{ReasonCandidateNotInPlan}
		return out
	}

	var applicable []RuleVersion
	for _, rule := range rules {
		if rule.ReviewState != ReviewStatePublished || !rule.Scope.Applies(scope, group, major) {
			continue
		}
		applicable = append(applicable, rule)
		out.EvidenceRefs = appendUniqueEvidence(out.EvidenceRefs, rule.SourceEvidence...)
	}

	hasFailure := false
	hasMissing := false
	hasUnknown := false
	for _, rule := range applicable {
		decision, missing := evaluateExpression(rule.When, major, facts)
		switch {
		case len(missing) > 0:
			hasMissing = true
			out.MissingFields = append(out.MissingFields, missing...)
			out.UnknownReasons = append(out.UnknownReasons, ReasonProfileFactMissing)
		case decision == "MATCH" && (rule.OutcomeWhenTrue == "" || rule.OutcomeWhenTrue == OutcomeFail):
			hasFailure = true
			out.FailureRuleKeys = append(out.FailureRuleKeys, rule.RuleKey)
		case decision == "MATCH":
			hasUnknown = true
			out.UnknownReasons = append(out.UnknownReasons, ReasonRuleExpression)
		case decision == "INDETERMINATE":
			hasUnknown = true
			out.UnknownReasons = append(out.UnknownReasons, ReasonRuleExpression)
		}
	}
	if hasFailure {
		out.Status = MajorFail
		out.FailureRuleKeys = stableStrings(out.FailureRuleKeys)
		out.UnknownReasons = nil
		out.MissingFields = nil
		return out
	}
	if hasMissing || hasUnknown {
		if hasMissing {
			out.Status = MajorMissingRequiredFact
		} else {
			out.Status = MajorUnknown
		}
		out.FailureRuleKeys = stableStrings(out.FailureRuleKeys)
		out.UnknownReasons = stableStrings(out.UnknownReasons)
		out.MissingFields = stableStrings(out.MissingFields)
		return out
	}

	invalidatedRuleTypes := invalidatedRuleTypesFor(revokedRules, scope, group, major)
	for _, ruleType := range requiredRuleTypes(applicable, invalidatedRuleTypes) {
		declaration, found, _ := resolveCoverage(coverage, ruleType, scope, group, major)
		if containsString(invalidatedRuleTypes, ruleType) ||
			!found ||
			declaration.Status != CoverageFull {
			out.Status = MajorUnknown
			out.UnknownReasons = append(out.UnknownReasons, ReasonRuleCoverageMissing)
		}
	}
	if out.Status == MajorUnknown {
		out.UnknownReasons = stableStrings(out.UnknownReasons)
		return out
	}
	out.Status = MajorPass
	return out
}

func (s RuleScope) Applies(scope EligibilityScope, group CandidateGroup, major CandidateMajor) bool {
	return (s.Province == "" || s.Province == scope.Province) &&
		(s.ApplicationYear == 0 || s.ApplicationYear == scope.ApplicationYear) &&
		(s.Category == "" || s.Category == scope.Category) &&
		(s.Batch == "" || s.Batch == scope.Batch) &&
		(s.UniversityID == 0 || s.UniversityID == group.UniversityID) &&
		(s.MajorGroup == "" || s.MajorGroup == group.MajorGroup) &&
		(s.MajorCode == "" || s.MajorCode == major.MajorCode)
}

func (s RuleScope) Overlaps(other RuleScope) bool {
	return scopesOverlap(s.Province, other.Province) &&
		scopesOverlapInt(s.ApplicationYear, other.ApplicationYear) &&
		scopesOverlap(s.Category, other.Category) &&
		scopesOverlap(s.Batch, other.Batch) &&
		scopesOverlapInt(s.UniversityID, other.UniversityID) &&
		scopesOverlap(s.MajorGroup, other.MajorGroup) &&
		scopesOverlap(s.MajorCode, other.MajorCode)
}

func (s RuleScope) Covers(other RuleScope) bool {
	return scopeCovers(s.Province, other.Province) &&
		scopeCoversInt(s.ApplicationYear, other.ApplicationYear) &&
		scopeCovers(s.Category, other.Category) &&
		scopeCovers(s.Batch, other.Batch) &&
		scopeCoversInt(s.UniversityID, other.UniversityID) &&
		scopeCovers(s.MajorGroup, other.MajorGroup) &&
		scopeCovers(s.MajorCode, other.MajorCode)
}

func (s RuleScope) Specificity() int {
	values := []string{
		s.Province,
		s.Category,
		s.Batch,
		s.MajorGroup,
		s.MajorCode,
	}
	specificity := 0
	for _, value := range values {
		if value != "" {
			specificity++
		}
	}
	if s.ApplicationYear != 0 {
		specificity++
	}
	if s.UniversityID != 0 {
		specificity++
	}
	return specificity
}

func scopesOverlap(left, right string) bool {
	return left == "" || right == "" || left == right
}

func scopesOverlapInt(left, right int) bool {
	return left == 0 || right == 0 || left == right
}

func scopeCovers(container, target string) bool {
	return container == "" || container == target
}

func scopeCoversInt(container, target int) bool {
	return container == 0 || container == target
}

func conflictingRuleKeys(rules []RuleVersion) []string {
	var conflicts []string
	for i := 0; i < len(rules); i++ {
		if rules[i].ReviewState != ReviewStatePublished {
			continue
		}
		for j := i + 1; j < len(rules); j++ {
			if rules[j].ReviewState != ReviewStatePublished ||
				rules[i].RuleKey != rules[j].RuleKey ||
				!rules[i].Scope.Overlaps(rules[j].Scope) {
				continue
			}
			conflicts = append(conflicts, rules[i].RuleKey)
			break
		}
	}
	return stableStrings(conflicts)
}

func profileFactIndex(
	facts []ProfileFact,
	definitions map[string]ProfileFieldDefinition,
) map[string]ProfileFact {
	index := make(map[string]ProfileFact, len(facts))
	for _, fact := range facts {
		if definition, ok := definitions[fact.FieldKey]; ok {
			fact.fieldKind = profileFieldKindForValueType(definition.ValueType)
			fact.allowedValues = append([]string(nil), definition.AllowedValues...)
		}
		index[fact.FieldKey] = fact
	}
	return index
}

func usableProfileState(state string) bool {
	switch state {
	case ProfileProvided, ProfileDerived, ProfileDefault:
		return true
	default:
		return false
	}
}

func stableStrings(values []string) []string {
	seen := make(map[string]struct{}, len(values))
	out := make([]string, 0, len(values))
	for _, value := range values {
		if value == "" {
			continue
		}
		if _, ok := seen[value]; ok {
			continue
		}
		seen[value] = struct{}{}
		out = append(out, value)
	}
	sort.Strings(out)
	return out
}

func containsString(values []string, want string) bool {
	for _, value := range values {
		if value == want {
			return true
		}
	}
	return false
}

func invalidatedRuleTypesFor(
	revoked []RevokedRuleVersion,
	scope EligibilityScope,
	group CandidateGroup,
	major CandidateMajor,
) []string {
	var ruleTypes []string
	for _, item := range revoked {
		if item.Scope.Applies(scope, group, major) {
			ruleTypes = append(ruleTypes, item.RuleType)
		}
	}
	return stableStrings(ruleTypes)
}

func requiredRuleTypes(applicable []RuleVersion, invalidated []string) []string {
	required := append([]string{RuleTypeSubjectRequirement}, invalidated...)
	for _, rule := range applicable {
		required = append(required, rule.RuleType)
	}
	return stableStrings(required)
}

func countCoverageConflicts(
	req EligibilityRequest,
	rules []RuleVersion,
	coverage []RuleCoverage,
	revokedRules []RevokedRuleVersion,
) int {
	conflicts := 0
	for _, group := range req.CandidateGroups {
		for _, major := range group.Majors {
			if !major.InCurrentPlan {
				continue
			}
			var applicable []RuleVersion
			for _, rule := range rules {
				if rule.ReviewState == ReviewStatePublished &&
					rule.Scope.Applies(req.Scope, group, major) {
					applicable = append(applicable, rule)
				}
			}
			invalidated := invalidatedRuleTypesFor(revokedRules, req.Scope, group, major)
			for _, ruleType := range requiredRuleTypes(applicable, invalidated) {
				_, _, conflict := resolveCoverage(
					coverage,
					ruleType,
					req.Scope,
					group,
					major,
				)
				if conflict {
					conflicts++
				}
			}
		}
	}
	return conflicts
}

// resolveCoverage 选择适用于当前专业的最高具体度声明。覆盖范围越窄越优先；
// 同具体度出现多个适用声明时无法确定唯一事实，必须阻断而不是按输入顺序任选。
func resolveCoverage(
	coverage []RuleCoverage,
	ruleType string,
	scope EligibilityScope,
	group CandidateGroup,
	major CandidateMajor,
) (RuleCoverage, bool, bool) {
	var applicable []RuleCoverage
	maxSpecificity := -1
	for _, item := range coverage {
		if item.RuleType != ruleType || !item.Scope.Applies(scope, group, major) {
			continue
		}
		specificity := item.Scope.Specificity()
		switch {
		case specificity > maxSpecificity:
			maxSpecificity = specificity
			applicable = append(applicable[:0], item)
		case specificity == maxSpecificity:
			applicable = append(applicable, item)
		}
	}
	if len(applicable) == 0 {
		return RuleCoverage{}, false, false
	}
	if len(applicable) > 1 {
		return RuleCoverage{}, true, true
	}
	return applicable[0], true, false
}

func sortedRules(rules []RuleVersion) []RuleVersion {
	out := append([]RuleVersion(nil), rules...)
	sort.Slice(out, func(i, j int) bool {
		if out[i].RuleKey != out[j].RuleKey {
			return out[i].RuleKey < out[j].RuleKey
		}
		if out[i].RuleID != out[j].RuleID {
			return out[i].RuleID < out[j].RuleID
		}
		return out[i].Version < out[j].Version
	})
	return out
}

func cloneCoverage(coverage []RuleCoverage) []RuleCoverage {
	out := append([]RuleCoverage(nil), coverage...)
	sort.Slice(out, func(i, j int) bool {
		if out[i].RuleType != out[j].RuleType {
			return out[i].RuleType < out[j].RuleType
		}
		return out[i].Scope.String() < out[j].Scope.String()
	})
	return out
}

func (s RuleScope) String() string {
	return strings.Join([]string{
		s.Province,
		strconv.Itoa(s.ApplicationYear),
		s.Category,
		s.Batch,
		strconv.Itoa(s.UniversityID),
		s.MajorGroup,
		s.MajorCode,
	}, "|")
}

func eligibilityGroupKey(scope EligibilityScope, group CandidateGroup) string {
	return group.UniversityCode + "|" + group.MajorGroup
}

func appendUniqueEvidence(current []EvidenceRef, refs ...EvidenceRef) []EvidenceRef {
	seen := make(map[string]struct{}, len(current))
	for _, ref := range current {
		seen[ref.EvidenceID] = struct{}{}
	}
	for _, ref := range refs {
		if ref.EvidenceID == "" {
			continue
		}
		if _, ok := seen[ref.EvidenceID]; ok {
			continue
		}
		seen[ref.EvidenceID] = struct{}{}
		current = append(current, ref)
	}
	return current
}

func blockedGroups(groups []CandidateGroup, reason string) []GroupEligibility {
	out := make([]GroupEligibility, 0, len(groups))
	for _, group := range groups {
		ground := GroupEligibility{GroupKey: group.UniversityCode + "|" + group.MajorGroup, GroupStatus: GroupUnknown}
		for _, major := range group.Majors {
			ground.Majors = append(ground.Majors, MajorEligibility{
				MajorCode:      major.MajorCode,
				Status:         MajorUnknown,
				UnknownReasons: []string{reason},
			})
		}
		sort.Slice(ground.Majors, func(i, j int) bool { return ground.Majors[i].MajorCode < ground.Majors[j].MajorCode })
		out = append(out, ground)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].GroupKey < out[j].GroupKey })
	return out
}

func candidateMajorCount(groups []CandidateGroup) int {
	count := 0
	for _, group := range groups {
		count += len(group.Majors)
	}
	return count
}

func validEligibilityScope(scope EligibilityScope) bool {
	return strings.TrimSpace(scope.Province) != "" &&
		scope.ApplicationYear > 0 &&
		strings.TrimSpace(scope.Category) != "" &&
		strings.TrimSpace(scope.Batch) != ""
}

func maxInt(left, right int) int {
	if left > right {
		return left
	}
	return right
}

func hasDuplicateMajorCodes(majors []CandidateMajor) bool {
	seen := make(map[string]struct{}, len(majors))
	for _, major := range majors {
		if _, ok := seen[major.MajorCode]; ok {
			return true
		}
		seen[major.MajorCode] = struct{}{}
	}
	return false
}

func hasDuplicateGroupKeys(scope EligibilityScope, groups []CandidateGroup) bool {
	seen := make(map[string]struct{}, len(groups))
	for _, group := range groups {
		key := eligibilityGroupKey(scope, group)
		if _, ok := seen[key]; ok {
			return true
		}
		seen[key] = struct{}{}
	}
	return false
}

func validCandidateGroupIdentity(group CandidateGroup) bool {
	return group.UniversityID > 0 &&
		strings.TrimSpace(group.UniversityCode) != "" &&
		strings.TrimSpace(group.MajorGroup) != ""
}
