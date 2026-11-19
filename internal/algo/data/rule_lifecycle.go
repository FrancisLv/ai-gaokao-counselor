package data

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	"github.com/choucisan/ai-gaokao-counselor/internal/algo/biz"
	errs "github.com/choucisan/ai-gaokao-counselor/internal/pkg/errors"
)

// RuleDraft 是创建 RuleVersion 内容版本的内部写入命令。内容落库后不可原地修改。
type RuleDraft struct {
	RuleID                string
	Version               int
	RuleKey               string
	RuleType              string
	Scope                 biz.RuleScope
	RequiredProfileFields []string
	Expression            biz.RuleExpression
	OutcomeWhenTrue       string
	ReasonCode            string
	SourceEvidence        []biz.EvidenceRef
	CreatedBy             string
}

// RuleTransition 是规则生命周期的内部状态变更命令；不暴露为在线业务 RPC。
type RuleTransition struct {
	RuleID        string
	Version       int
	ExpectedState string
	TargetState   string
	Actor         string
	ReleaseID     string
	Reason        string
}

type RuleLifecycleRepo struct {
	d *Data
}

func NewRuleLifecycleRepo(d *Data) *RuleLifecycleRepo {
	return &RuleLifecycleRepo{d: d}
}

func (r *RuleLifecycleRepo) CreateDraft(ctx context.Context, draft RuleDraft) error {
	if err := validateRuleDraft(draft); err != nil {
		return err
	}
	scope, err := json.Marshal(draft.Scope)
	if err != nil {
		return errs.ErrInvalidQuery
	}
	required, err := json.Marshal(draft.RequiredProfileFields)
	if err != nil {
		return errs.ErrInvalidQuery
	}
	expression, err := json.Marshal(draft.Expression)
	if err != nil {
		return errs.ErrInvalidQuery
	}
	evidence, err := json.Marshal(draft.SourceEvidence)
	if err != nil {
		return errs.ErrInvalidQuery
	}

	tx, err := r.d.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	if _, err := tx.Exec(ctx, `
		INSERT INTO rule_version (
			rule_id, version, rule_key, rule_type, scope, required_profile_fields,
			expression, outcome_when_true, reason_code, review_status,
			source_evidence, created_by
		) VALUES (
			$1, $2, $3, $4, $5::jsonb, $6::jsonb,
			$7::jsonb, $8, $9, 'DRAFT', $10::jsonb, $11
		)`,
		draft.RuleID, draft.Version, draft.RuleKey, draft.RuleType,
		string(scope), string(required), string(expression), draft.OutcomeWhenTrue,
		draft.ReasonCode, string(evidence), draft.CreatedBy,
	); err != nil {
		return mapRuleDraftDBError(err)
	}
	return tx.Commit(ctx)
}

func (r *RuleLifecycleRepo) Transition(ctx context.Context, change RuleTransition) error {
	if strings.TrimSpace(change.RuleID) == "" ||
		change.Version <= 0 ||
		strings.TrimSpace(change.Actor) == "" {
		return errs.ErrInvalidQuery
	}
	if change.TargetState != biz.ReviewStateReviewed &&
		change.TargetState != biz.ReviewStatePublished &&
		change.TargetState != biz.ReviewStateRevoked {
		return errs.ErrRuleStateConflict
	}

	tx, err := r.d.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback(ctx) }()

	var currentState, ruleKey, ruleType, outcomeWhenTrue, reasonCode string
	var scope, requiredFieldsJSON, expressionJSON, evidenceJSON []byte
	var expressionVersion int
	var validInterval bool
	err = tx.QueryRow(ctx, `
		SELECT
			review_status,
			rule_key,
			rule_type,
			scope,
			required_profile_fields,
			expression,
			expression_version,
			outcome_when_true,
			reason_code,
			source_evidence,
			valid_to IS NULL OR valid_to > valid_from
		FROM rule_version
		WHERE rule_id = $1 AND version = $2
		FOR UPDATE`,
		change.RuleID, change.Version,
	).Scan(
		&currentState,
		&ruleKey,
		&ruleType,
		&scope,
		&requiredFieldsJSON,
		&expressionJSON,
		&expressionVersion,
		&outcomeWhenTrue,
		&reasonCode,
		&evidenceJSON,
		&validInterval,
	)
	if errors.Is(err, pgx.ErrNoRows) || currentState != change.ExpectedState {
		return errs.ErrRuleStateConflict
	}
	if err != nil {
		return err
	}
	if !validRuleTransition(currentState, change.TargetState) {
		return errs.ErrRuleStateConflict
	}
	if !validInterval {
		return errs.ErrRulePublishIncomplete
	}

	rule, err := decodeLifecycleRule(
		change.RuleID,
		change.Version,
		currentState,
		ruleKey,
		ruleType,
		scope,
		requiredFieldsJSON,
		expressionJSON,
		expressionVersion,
		outcomeWhenTrue,
		reasonCode,
		evidenceJSON,
	)
	if err != nil {
		return errs.ErrRulePublishIncomplete
	}
	definitionReleaseID := ""
	if change.TargetState == biz.ReviewStatePublished {
		definitionReleaseID = change.ReleaseID
	}
	definitions, err := loadLifecycleProfileFields(ctx, tx, definitionReleaseID)
	if err != nil {
		return err
	}
	if reason := biz.ValidateRuleVersionForLifecycle(rule, definitions); reason != "" {
		return fmt.Errorf("%w: %s", errs.ErrRulePublishIncomplete, reason)
	}

	switch change.TargetState {
	case biz.ReviewStateReviewed:
		_, err = tx.Exec(ctx, `
			UPDATE rule_version
			SET review_status = 'REVIEWED',
			    reviewed_by = $3,
			    reviewed_at = clock_timestamp()
			WHERE rule_id = $1 AND version = $2`,
			change.RuleID, change.Version, change.Actor,
		)
	case biz.ReviewStatePublished:
		if strings.TrimSpace(change.ReleaseID) == "" {
			return errs.ErrRulePublishIncomplete
		}
		for _, ref := range rule.SourceEvidence {
			if strings.TrimSpace(ref.EvidenceID) == "" || ref.ReleaseID != change.ReleaseID {
				return errs.ErrRulePublishIncomplete
			}
		}
		var coverageExists, conflictExists bool
		if err := tx.QueryRow(ctx, `
			SELECT EXISTS (
				SELECT 1
				FROM rule_coverage coverage
				WHERE coverage.release_id = $1
				  AND coverage.rule_type = $2
				  AND coverage.status IN ('FULL', 'PARTIAL')
				  AND eligibility_scope_covers(coverage.scope, $3::jsonb)
			)`, change.ReleaseID, ruleType, string(scope),
		).Scan(&coverageExists); err != nil {
			return err
		}
		if !coverageExists {
			return errs.ErrRulePublishIncomplete
		}
		if err := tx.QueryRow(ctx, `
			SELECT EXISTS (
				SELECT 1
				FROM rule_version existing
				WHERE existing.release_id = $1
				  AND existing.rule_key = (
				      SELECT rule_key FROM rule_version WHERE rule_id = $2 AND version = $3
				  )
				  AND existing.review_status = 'PUBLISHED'
				  AND (existing.rule_id <> $2 OR existing.version <> $3)
				  AND eligibility_scope_overlaps(existing.scope, $4::jsonb)
			)`, change.ReleaseID, change.RuleID, change.Version, string(scope),
		).Scan(&conflictExists); err != nil {
			return err
		}
		if conflictExists {
			return errs.ErrRulePublishConflict
		}
		_, err = tx.Exec(ctx, `
			UPDATE rule_version
			SET review_status = 'PUBLISHED',
			    release_id = $3,
			    published_by = $4,
			    published_at = clock_timestamp()
			WHERE rule_id = $1 AND version = $2`,
			change.RuleID, change.Version, change.ReleaseID, change.Actor,
		)
	case biz.ReviewStateRevoked:
		if strings.TrimSpace(change.Reason) == "" {
			return errs.ErrRuleStateConflict
		}
		_, err = tx.Exec(ctx, `
			UPDATE rule_version
			SET review_status = 'REVOKED',
			    revoked_by = $3,
			    revoked_at = clock_timestamp(),
			    revoke_reason = $4
			WHERE rule_id = $1 AND version = $2`,
			change.RuleID, change.Version, change.Actor, change.Reason,
		)
	}
	if err != nil {
		return mapRuleLifecycleDBError(err)
	}
	return tx.Commit(ctx)
}

func decodeLifecycleRule(
	ruleID string,
	version int,
	state string,
	ruleKey string,
	ruleType string,
	scopeJSON []byte,
	requiredFieldsJSON []byte,
	expressionJSON []byte,
	expressionVersion int,
	outcomeWhenTrue string,
	reasonCode string,
	evidenceJSON []byte,
) (biz.RuleVersion, error) {
	var scope biz.RuleScope
	if err := json.Unmarshal(scopeJSON, &scope); err != nil {
		return biz.RuleVersion{}, err
	}
	var required []string
	if err := json.Unmarshal(requiredFieldsJSON, &required); err != nil {
		return biz.RuleVersion{}, err
	}
	var expression biz.RuleExpression
	if err := json.Unmarshal(expressionJSON, &expression); err != nil {
		return biz.RuleVersion{}, err
	}
	var evidence []biz.EvidenceRef
	if err := json.Unmarshal(evidenceJSON, &evidence); err != nil {
		return biz.RuleVersion{}, err
	}
	return biz.RuleVersion{
		RuleID:                ruleID,
		Version:               version,
		RuleKey:               ruleKey,
		RuleType:              ruleType,
		Scope:                 scope,
		RequiredProfileFields: required,
		When:                  expression,
		OutcomeWhenTrue:       outcomeWhenTrue,
		ReasonCode:            reasonCode,
		ReviewState:           state,
		ExpressionVersion:     expressionVersion,
		SourceEvidence:        evidence,
	}, nil
}

func loadLifecycleProfileFields(
	ctx context.Context,
	tx pgx.Tx,
	releaseID string,
) (map[string]biz.ProfileFieldDefinition, error) {
	query := `
		SELECT field_key, value_type, allowed_values
		FROM eligibility_profile_field
		WHERE ($1::text = '' OR release_id = $1::text)
		ORDER BY field_key, release_id`
	rows, err := tx.Query(ctx, query, releaseID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	definitions := make(map[string]biz.ProfileFieldDefinition)
	for rows.Next() {
		var definition biz.ProfileFieldDefinition
		if err := rows.Scan(
			&definition.FieldKey,
			&definition.ValueType,
			&definition.AllowedValues,
		); err != nil {
			return nil, err
		}
		if existing, ok := definitions[definition.FieldKey]; ok {
			if existing.ValueType != definition.ValueType ||
				!sameStringSlice(existing.AllowedValues, definition.AllowedValues) {
				return nil, fmt.Errorf("%w: %s", errs.ErrRulePublishIncomplete, biz.ReasonRuleUnknownField)
			}
			continue
		}
		definitions[definition.FieldKey] = definition
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return definitions, nil
}

func sameStringSlice(left, right []string) bool {
	if len(left) != len(right) {
		return false
	}
	for i := range left {
		if left[i] != right[i] {
			return false
		}
	}
	return true
}

func validateRuleDraft(draft RuleDraft) error {
	if strings.TrimSpace(draft.RuleID) == "" ||
		draft.Version <= 0 ||
		strings.TrimSpace(draft.RuleKey) == "" ||
		strings.TrimSpace(draft.RuleType) == "" ||
		strings.TrimSpace(draft.ReasonCode) == "" ||
		strings.TrimSpace(draft.CreatedBy) == "" ||
		len(draft.SourceEvidence) == 0 {
		return errs.ErrInvalidQuery
	}
	return nil
}

func validRuleTransition(from, to string) bool {
	return (from == biz.ReviewStateDraft && to == biz.ReviewStateReviewed) ||
		(from == biz.ReviewStateReviewed && to == biz.ReviewStatePublished) ||
		(from == biz.ReviewStatePublished && to == biz.ReviewStateRevoked)
}

func mapRuleLifecycleDBError(err error) error {
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) {
		switch pgErr.Code {
		case "23505":
			return errs.ErrRulePublishConflict
		case "23514":
			return errs.ErrRuleStateConflict
		}
	}
	return err
}

func mapRuleDraftDBError(err error) error {
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) && (pgErr.Code == "23505" || pgErr.Code == "23514") {
		return errs.ErrInvalidQuery
	}
	return err
}
