package data

import (
	"context"
	"encoding/json"
	"errors"

	"github.com/jackc/pgx/v5"

	"github.com/choucisan/ai-gaokao-counselor/internal/algo/biz"
)

type eligibilityRepo struct {
	d *Data
}

func NewEligibilityRepo(d *Data) biz.EligibilityRepo {
	return &eligibilityRepo{d: d}
}

func (r *eligibilityRepo) LoadEligibilityRuleSet(ctx context.Context, releaseID string) (biz.EligibilityRuleSet, error) {
	status, err := r.d.queries.GetEligibilityRelease(ctx, releaseID)
	if errors.Is(err, pgx.ErrNoRows) {
		return biz.EligibilityRuleSet{}, nil
	}
	if err != nil {
		return biz.EligibilityRuleSet{}, err
	}
	if status != "PUBLISHED" {
		return biz.EligibilityRuleSet{ReleaseStatus: status}, nil
	}

	ruleRows, err := r.d.queries.ListPublishedEligibilityRules(ctx, releaseID)
	if err != nil {
		return biz.EligibilityRuleSet{}, err
	}
	rules := make([]biz.RuleVersion, 0, len(ruleRows))
	for _, row := range ruleRows {
		var scope biz.RuleScope
		if err := json.Unmarshal(row.Scope, &scope); err != nil {
			return biz.EligibilityRuleSet{}, err
		}
		var required []string
		if err := json.Unmarshal(row.RequiredProfileFields, &required); err != nil {
			return biz.EligibilityRuleSet{}, err
		}
		var expression biz.RuleExpression
		if err := json.Unmarshal(row.Expression, &expression); err != nil {
			return biz.EligibilityRuleSet{}, err
		}
		var evidence []biz.EvidenceRef
		if err := json.Unmarshal(row.SourceEvidence, &evidence); err != nil {
			return biz.EligibilityRuleSet{}, err
		}
		rules = append(rules, biz.RuleVersion{
			RuleID:                row.RuleID,
			Version:               int(row.Version),
			RuleKey:               row.RuleKey,
			RuleType:              row.RuleType,
			Scope:                 scope,
			RequiredProfileFields: required,
			When:                  expression,
			OutcomeWhenTrue:       row.OutcomeWhenTrue,
			ReasonCode:            row.ReasonCode,
			ReviewState:           row.ReviewStatus,
			SourceEvidence:        evidence,
		})
	}

	coverageRows, err := r.d.queries.ListEligibilityCoverage(ctx, releaseID)
	if err != nil {
		return biz.EligibilityRuleSet{}, err
	}
	coverage := make([]biz.RuleCoverage, 0, len(coverageRows))
	for _, row := range coverageRows {
		var scope biz.RuleScope
		if err := json.Unmarshal(row.Scope, &scope); err != nil {
			return biz.EligibilityRuleSet{}, err
		}
		var sources []string
		if err := json.Unmarshal(row.CoveredDataSources, &sources); err != nil {
			return biz.EligibilityRuleSet{}, err
		}
		coverage = append(coverage, biz.RuleCoverage{
			RuleType:           row.RuleType,
			Scope:              scope,
			Status:             row.Status,
			CoveredDataSources: sources,
			Notes:              row.Notes,
		})
	}
	return biz.EligibilityRuleSet{
		ReleaseStatus: status,
		Rules:         rules,
		Coverage:      coverage,
	}, nil
}
