package data

import (
	"context"
	"encoding/json"
	"errors"
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

	var currentState, ruleType string
	var scope, evidenceJSON []byte
	err = tx.QueryRow(ctx, `
		SELECT review_status, rule_type, scope, source_evidence
		FROM rule_version
		WHERE rule_id = $1 AND version = $2
		FOR UPDATE`,
		change.RuleID, change.Version,
	).Scan(&currentState, &ruleType, &scope, &evidenceJSON)
	if errors.Is(err, pgx.ErrNoRows) || currentState != change.ExpectedState {
		return errs.ErrRuleStateConflict
	}
	if err != nil {
		return err
	}
	if !validRuleTransition(currentState, change.TargetState) {
		return errs.ErrRuleStateConflict
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
		var evidence []biz.EvidenceRef
		if err := json.Unmarshal(evidenceJSON, &evidence); err != nil {
			return errs.ErrRulePublishIncomplete
		}
		for _, ref := range evidence {
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
