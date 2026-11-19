package biz

import (
	"context"
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
	Operator           string          `json:"op"`
	SubjectRequirement string          `json:"subject_req,omitempty"`
	Child              *RuleExpression `json:"child,omitempty"`
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
	FieldKey     string
	State        string
	StringValue  string
	IntValue     int
	StringValues []string
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
	OperatorNot                = "NOT"
	OutcomeFail                = "FAIL"

	ReviewStatePublished = "PUBLISHED"
	CoverageFull         = "FULL"
	CoveragePartial      = "PARTIAL"
	CoverageNone         = "NONE"

	ProfileLifecycleConfirmed = "CONFIRMED"
	ProfileProvided           = "PROVIDED"
	ProfileUnknown            = "UNKNOWN"
	ProfileDeclined           = "DECLINED"
	ProfileDerived            = "DERIVED"
	ProfileDefault            = "DEFAULT"

	FieldSubjects = "subjects"
	FieldCategory = "category"
)

const (
	ReasonRuleCoverageMissing = "RULE_COVERAGE_MISSING"
	ReasonProfileFactMissing  = "PROFILE_FACT_MISSING"
	ReasonRuleExpression      = "RULE_EXPRESSION_INVALID"
	ReasonReleaseRevoked      = "RELEASE_REVOKED"
	ReasonReleaseUnavailable  = "RELEASE_UNAVAILABLE"
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

	facts := profileFactIndex(req.ProfileFacts)
	rules := sortedRules(set.Rules)
	result.Diagnostics.EvaluatedRuleCount = len(rules)
	for _, group := range req.CandidateGroups {
		ground := evaluateGroup(req.Scope, group, rules, set.Coverage, facts)
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

func evaluateGroup(scope EligibilityScope, group CandidateGroup, rules []RuleVersion, coverage []RuleCoverage, facts map[string]ProfileFact) GroupEligibility {
	out := GroupEligibility{
		GroupKey: eligibilityGroupKey(scope, group),
	}
	majors := append([]CandidateMajor(nil), group.Majors...)
	sort.Slice(majors, func(i, j int) bool { return majors[i].MajorCode < majors[j].MajorCode })
	for _, major := range majors {
		evaluated := evaluateMajor(scope, group, major, rules, coverage, facts)
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

func evaluateMajor(scope EligibilityScope, group CandidateGroup, major CandidateMajor, rules []RuleVersion, coverage []RuleCoverage, facts map[string]ProfileFact) MajorEligibility {
	out := MajorEligibility{MajorCode: major.MajorCode}
	if !major.InCurrentPlan {
		out.Status = MajorNotApplicable
		out.UnknownReasons = []string{"CANDIDATE_NOT_IN_PLAN"}
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

	for _, rule := range applicable {
		decision, missing := evaluateExpression(rule.When, major, facts)
		switch {
		case len(missing) > 0:
			out.Status = MajorMissingRequiredFact
			out.MissingFields = append(out.MissingFields, missing...)
			out.UnknownReasons = append(out.UnknownReasons, ReasonProfileFactMissing)
		case decision == "MATCH" && (rule.OutcomeWhenTrue == "" || rule.OutcomeWhenTrue == OutcomeFail):
			out.Status = MajorFail
			out.FailureRuleKeys = append(out.FailureRuleKeys, rule.RuleKey)
		case decision == "MATCH":
			out.Status = MajorUnknown
			out.UnknownReasons = append(out.UnknownReasons, ReasonRuleExpression)
		case decision == "INDETERMINATE":
			out.Status = MajorUnknown
			out.UnknownReasons = append(out.UnknownReasons, ReasonRuleExpression)
		}
	}
	if out.Status == MajorFail || out.Status == MajorMissingRequiredFact || out.Status == MajorUnknown {
		out.FailureRuleKeys = stableStrings(out.FailureRuleKeys)
		out.UnknownReasons = stableStrings(out.UnknownReasons)
		out.MissingFields = stableStrings(out.MissingFields)
		return out
	}

	required := []string{RuleTypeSubjectRequirement}
	for _, rule := range applicable {
		required = append(required, rule.RuleType)
	}
	for _, ruleType := range stableStrings(required) {
		if !hasFullCoverage(coverage, ruleType) {
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

func evaluateExpression(expr RuleExpression, major CandidateMajor, facts map[string]ProfileFact) (string, []string) {
	switch expr.Operator {
	case OperatorNot:
		if expr.Child == nil {
			return "INDETERMINATE", nil
		}
		decision, missing := evaluateExpression(*expr.Child, major, facts)
		switch decision {
		case "MATCH":
			return "NO_MATCH", missing
		case "NO_MATCH":
			return "MATCH", missing
		default:
			return "INDETERMINATE", missing
		}
	case OperatorSubjectMatch:
		subjects, missingSubjects := profileStringList(facts, FieldSubjects)
		_, missingCategory := profileString(facts, FieldCategory)
		missing := append(missingSubjects, missingCategory...)
		if len(missing) > 0 {
			return "INDETERMINATE", missing
		}
		requirements := []string{major.SubjectRequirement}
		if strings.TrimSpace(expr.SubjectRequirement) != "" {
			requirements = []string{expr.SubjectRequirement}
		}
		matched, _, parseErr := matchCandidateSubjects(subjects, requirements)
		if parseErr != nil {
			return "INDETERMINATE", nil
		}
		if matched {
			return "MATCH", nil
		}
		return "NO_MATCH", nil
	default:
		return "INDETERMINATE", nil
	}
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

func hasFullCoverage(coverage []RuleCoverage, ruleType string) bool {
	for _, item := range coverage {
		if item.RuleType == ruleType && item.Status == CoverageFull {
			return true
		}
	}
	return false
}

func profileFactIndex(facts []ProfileFact) map[string]ProfileFact {
	index := make(map[string]ProfileFact, len(facts))
	for _, fact := range facts {
		index[fact.FieldKey] = fact
	}
	return index
}

func profileString(facts map[string]ProfileFact, key string) (string, []string) {
	fact, ok := facts[key]
	if !ok || !usableProfileState(fact.State) || strings.TrimSpace(fact.StringValue) == "" {
		return "", []string{key}
	}
	return fact.StringValue, nil
}

func profileStringList(facts map[string]ProfileFact, key string) ([]string, []string) {
	fact, ok := facts[key]
	if !ok || !usableProfileState(fact.State) || len(fact.StringValues) == 0 {
		return nil, []string{key}
	}
	return append([]string(nil), fact.StringValues...), nil
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
