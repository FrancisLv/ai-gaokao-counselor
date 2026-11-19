//go:build integration
// +build integration

package data

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/choucisan/ai-gaokao-counselor/conf"
	"github.com/choucisan/ai-gaokao-counselor/internal/algo/biz"
	errs "github.com/choucisan/ai-gaokao-counselor/internal/pkg/errors"
)

func TestIntegration_RuleLifecycleAuditAndPublishGuards(t *testing.T) {
	pool, repo, d := setupRuleLifecycleIntegration(t)
	releaseID := fmt.Sprintf("rel_m4b_lifecycle_%d", time.Now().UnixNano())
	insertLifecycleRelease(t, pool, releaseID)
	insertLifecycleCoverage(t, pool, releaseID)

	draft := lifecycleRuleDraft(releaseID, "rule-subject-lifecycle-"+releaseID, "subject-requirement")
	cleanupLifecycleRule(t, pool, draft.RuleID)
	if err := repo.CreateDraft(context.Background(), draft); err != nil {
		t.Fatalf("CreateDraft: %v", err)
	}
	var draftReleaseID *string
	if err := pool.QueryRow(context.Background(),
		`SELECT release_id FROM rule_version WHERE rule_id = $1 AND version = $2`,
		draft.RuleID, draft.Version,
	).Scan(&draftReleaseID); err != nil {
		t.Fatalf("read draft release binding: %v", err)
	}
	if draftReleaseID != nil {
		t.Fatalf("DRAFT release_id = %q, want unbound", *draftReleaseID)
	}
	assertLifecycleAudit(t, pool, draft.RuleID, draft.Version, []string{"DRAFT"})

	if err := repo.Transition(context.Background(), RuleTransition{
		RuleID: draft.RuleID, Version: draft.Version,
		ExpectedState: "DRAFT", TargetState: "PUBLISHED", Actor: "reviewer",
	}); !errors.Is(err, errs.ErrRuleStateConflict) {
		t.Fatalf("DRAFT -> PUBLISHED err = %v, want ErrRuleStateConflict", err)
	}

	if err := repo.Transition(context.Background(), RuleTransition{
		RuleID: draft.RuleID, Version: draft.Version,
		ExpectedState: "DRAFT", TargetState: "REVIEWED", Actor: "reviewer",
	}); err != nil {
		t.Fatalf("DRAFT -> REVIEWED: %v", err)
	}
	assertLifecycleAudit(t, pool, draft.RuleID, draft.Version, []string{"DRAFT", "REVIEWED"})

	if _, err := pool.Exec(context.Background(),
		`UPDATE rule_version SET expression = '{"op":"EQ"}'::jsonb
		 WHERE rule_id = $1 AND version = $2`, draft.RuleID, draft.Version); err == nil {
		t.Fatal("REVIEWED rule content mutation unexpectedly succeeded")
	}

	if err := repo.Transition(context.Background(), RuleTransition{
		RuleID: draft.RuleID, Version: draft.Version,
		ExpectedState: "REVIEWED", TargetState: "PUBLISHED", Actor: "publisher",
		ReleaseID: releaseID,
	}); err != nil {
		t.Fatalf("REVIEWED -> PUBLISHED: %v", err)
	}
	assertLifecycleAudit(t, pool, draft.RuleID, draft.Version, []string{"DRAFT", "REVIEWED", "PUBLISHED"})

	conflicting := lifecycleRuleDraft(releaseID, "rule-subject-conflict-"+releaseID, "subject-requirement")
	cleanupLifecycleRule(t, pool, conflicting.RuleID)
	if err := repo.CreateDraft(context.Background(), conflicting); err != nil {
		t.Fatalf("Create conflicting draft: %v", err)
	}
	if err := repo.Transition(context.Background(), RuleTransition{
		RuleID: conflicting.RuleID, Version: conflicting.Version,
		ExpectedState: "DRAFT", TargetState: "REVIEWED", Actor: "reviewer",
	}); err != nil {
		t.Fatalf("conflicting DRAFT -> REVIEWED: %v", err)
	}
	if err := repo.Transition(context.Background(), RuleTransition{
		RuleID: conflicting.RuleID, Version: conflicting.Version,
		ExpectedState: "REVIEWED", TargetState: "PUBLISHED", Actor: "publisher",
		ReleaseID: releaseID,
	}); !errors.Is(err, errs.ErrRulePublishConflict) {
		t.Fatalf("publication conflict err = %v, want ErrRulePublishConflict", err)
	}
	if _, err := pool.Exec(context.Background(), `
		UPDATE release SET status = 'PUBLISHED', published_at = clock_timestamp()
		WHERE release_id = $1`, releaseID,
	); err != nil {
		t.Fatalf("publish lifecycle release: %v", err)
	}

	if err := repo.Transition(context.Background(), RuleTransition{
		RuleID: draft.RuleID, Version: draft.Version,
		ExpectedState: "PUBLISHED", TargetState: "REVOKED", Actor: "domain-owner",
		Reason: "source clause withdrawn",
	}); err != nil {
		t.Fatalf("PUBLISHED -> REVOKED: %v", err)
	}
	assertLifecycleAudit(t, pool, draft.RuleID, draft.Version,
		[]string{"DRAFT", "REVIEWED", "PUBLISHED", "REVOKED"})
	assertLifecycleRevocationAudit(t, pool, draft.RuleID, draft.Version,
		"domain-owner", "source clause withdrawn")

	replacement := draft
	replacement.Version = 2
	replacement.ReasonCode = "SUBJECT_NOT_MATCHED_V2"
	if err := repo.CreateDraft(context.Background(), replacement); err != nil {
		t.Fatalf("create replacement version: %v", err)
	}
	var oldReason, newReason string
	if err := pool.QueryRow(context.Background(), `
		SELECT
			(SELECT reason_code FROM rule_version WHERE rule_id = $1 AND version = 1),
			(SELECT reason_code FROM rule_version WHERE rule_id = $1 AND version = 2)`,
		draft.RuleID,
	).Scan(&oldReason, &newReason); err != nil {
		t.Fatalf("read replacement versions: %v", err)
	}
	if oldReason != draft.ReasonCode || newReason != replacement.ReasonCode {
		t.Fatalf("version content changed: v1=%q v2=%q", oldReason, newReason)
	}

	request := biz.EligibilityRequest{
		ReleaseID: releaseID, ConfirmedProfileVersion: "profile-v1",
		ProfileLifecycle: biz.ProfileLifecycleConfirmed,
		Scope: biz.EligibilityScope{
			Province: "湖南", ApplicationYear: 2025, Category: "物理类", Batch: "本科批",
		},
		ProfileFacts: []biz.ProfileFact{
			{FieldKey: "subjects", State: biz.ProfileProvided,
				StringValues: []string{"物理", "化学", "生物"}},
			{FieldKey: "category", State: biz.ProfileProvided, StringValue: "物理类"},
		},
		CandidateGroups: []biz.CandidateGroup{{
			UniversityID: 1, UniversityCode: "1001", UniversityName: "测试大学",
			MajorGroup: "01",
			Majors: []biz.CandidateMajor{{
				MajorCode: "080901", MajorName: "计算机科学与技术",
				SubjectRequirement: "首选物理，再选化学", InCurrentPlan: true,
			}},
		}},
	}
	uc := biz.NewEligibilityUsecase(NewEligibilityRepo(d))
	result, err := uc.EvaluateEligibility(context.Background(), request)
	if err != nil {
		t.Fatalf("EvaluateEligibility after revoke: %v", err)
	}
	if result.Status != biz.EligibilityPartial {
		t.Fatalf("status after revoke = %q, want PARTIAL", result.Status)
	}
	if got := result.Groups[0].Majors[0].Status; got != biz.MajorUnknown {
		t.Fatalf("major after revoke = %q, want UNKNOWN", got)
	}

	if err := repo.Transition(context.Background(), RuleTransition{
		RuleID: replacement.RuleID, Version: replacement.Version,
		ExpectedState: "DRAFT", TargetState: "REVIEWED", Actor: "reviewer",
	}); err != nil {
		t.Fatalf("replacement DRAFT -> REVIEWED: %v", err)
	}
	if err := repo.Transition(context.Background(), RuleTransition{
		RuleID: replacement.RuleID, Version: replacement.Version,
		ExpectedState: "REVIEWED", TargetState: "PUBLISHED", Actor: "publisher",
		ReleaseID: releaseID,
	}); err != nil {
		t.Fatalf("replacement REVIEWED -> PUBLISHED: %v", err)
	}
	recovered, err := uc.EvaluateEligibility(context.Background(), request)
	if err != nil {
		t.Fatalf("EvaluateEligibility after replacement publish: %v", err)
	}
	if recovered.Status != biz.EligibilityComplete {
		t.Fatalf("status after replacement = %q, want COMPLETE", recovered.Status)
	}
	if got := recovered.Groups[0].Majors[0].Status; got != biz.MajorPass {
		t.Fatalf("major after replacement = %q, want PASS", got)
	}
}

func TestIntegration_RuleLifecycleRejectsDirectPublishAndMissingCoverage(t *testing.T) {
	pool, repo, _ := setupRuleLifecycleIntegration(t)
	releaseID := fmt.Sprintf("rel_m4b_lifecycle_guards_%d", time.Now().UnixNano())
	insertLifecycleRelease(t, pool, releaseID)

	if _, err := pool.Exec(context.Background(), `
		INSERT INTO rule_version (
			rule_id, version, rule_key, rule_type, scope, required_profile_fields,
			expression, outcome_when_true, reason_code, review_status, release_id,
			source_evidence, created_by, reviewed_by, reviewed_at, published_by, published_at
		) VALUES (
			'rule-direct-publish', 1, 'subject-requirement', 'SUBJECT_REQUIREMENT',
			'{}'::jsonb, '["subjects","category"]'::jsonb,
			'{"op":"NOT","child":{"op":"SUBJECT_MATCH"}}'::jsonb, 'FAIL',
			'SUBJECT_NOT_MATCHED', 'PUBLISHED', $1,
			'[{"evidence_id":"ev-direct"}]'::jsonb, 'editor',
			'reviewer', NOW(), 'publisher', NOW()
		)`, releaseID,
	); err == nil {
		t.Fatal("direct PUBLISHED insert unexpectedly succeeded")
	}

	draft := lifecycleRuleDraft(releaseID, "rule-missing-coverage-"+releaseID, "subject-requirement")
	cleanupLifecycleRule(t, pool, draft.RuleID)
	if err := repo.CreateDraft(context.Background(), draft); err != nil {
		t.Fatalf("CreateDraft: %v", err)
	}
	if err := repo.Transition(context.Background(), RuleTransition{
		RuleID: draft.RuleID, Version: draft.Version,
		ExpectedState: "DRAFT", TargetState: "REVIEWED", Actor: "reviewer",
	}); err != nil {
		t.Fatalf("DRAFT -> REVIEWED: %v", err)
	}
	if err := repo.Transition(context.Background(), RuleTransition{
		RuleID: draft.RuleID, Version: draft.Version,
		ExpectedState: "REVIEWED", TargetState: "PUBLISHED", Actor: "publisher",
		ReleaseID: releaseID,
	}); !errors.Is(err, errs.ErrRulePublishIncomplete) {
		t.Fatalf("missing coverage err = %v, want ErrRulePublishIncomplete", err)
	}

	insertLifecycleCoverage(t, pool, releaseID)
	mismatched := lifecycleRuleDraft(releaseID, "rule-mismatched-evidence-"+releaseID, "subject-requirement")
	cleanupLifecycleRule(t, pool, mismatched.RuleID)
	mismatched.SourceEvidence[0].ReleaseID = "rel-other"
	if err := repo.CreateDraft(context.Background(), mismatched); err != nil {
		t.Fatalf("create mismatched-evidence draft: %v", err)
	}
	if err := repo.Transition(context.Background(), RuleTransition{
		RuleID: mismatched.RuleID, Version: mismatched.Version,
		ExpectedState: "DRAFT", TargetState: "REVIEWED", Actor: "reviewer",
	}); err != nil {
		t.Fatalf("mismatched-evidence DRAFT -> REVIEWED: %v", err)
	}
	if err := repo.Transition(context.Background(), RuleTransition{
		RuleID: mismatched.RuleID, Version: mismatched.Version,
		ExpectedState: "REVIEWED", TargetState: "PUBLISHED", Actor: "publisher",
		ReleaseID: releaseID,
	}); !errors.Is(err, errs.ErrRulePublishIncomplete) {
		t.Fatalf("mismatched evidence err = %v, want ErrRulePublishIncomplete", err)
	}
}

func setupRuleLifecycleIntegration(t *testing.T) (*pgxpool.Pool, *RuleLifecycleRepo, *Data) {
	t.Helper()
	dsn := os.Getenv("ALGO_TEST_DSN")
	if dsn == "" {
		t.Skip("ALGO_TEST_DSN 未设置，跳过集成测试")
	}
	pool, err := pgxpool.New(context.Background(), dsn)
	if err != nil {
		t.Fatalf("pgxpool.New: %v", err)
	}
	t.Cleanup(pool.Close)
	applyRuleLifecycleMigration(t, pool)

	t.Setenv("ALGO_DATA_DSN", dsn)
	d, cleanup, err := NewData(&conf.Bootstrap{
		Data: &conf.Data{Database: &conf.Data_Database{Dsn: dsn}},
	})
	if err != nil {
		t.Fatalf("NewData: %v", err)
	}
	t.Cleanup(cleanup)
	return pool, NewRuleLifecycleRepo(d), d
}

func applyRuleLifecycleMigration(t *testing.T, pool *pgxpool.Pool) {
	t.Helper()
	path := filepath.Join("..", "..", "..", "migrations", "22_rule_lifecycle.sql")
	body, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read migration: %v", err)
	}
	if _, err := pool.Exec(context.Background(), string(body)); err != nil {
		t.Fatalf("apply migration: %v", err)
	}
}

func insertLifecycleRelease(t *testing.T, pool *pgxpool.Pool, releaseID string) {
	t.Helper()
	if _, err := pool.Exec(context.Background(),
		`INSERT INTO release (release_id, status, manifest, manifest_sha256, published_at)
		 VALUES ($1, 'STAGING', '{}'::jsonb, 'sha256:m4b-lifecycle', NULL)`,
		releaseID,
	); err != nil {
		t.Fatalf("insert release: %v", err)
	}
	t.Cleanup(func() {
		_, _ = pool.Exec(context.Background(), `DELETE FROM release WHERE release_id = $1`, releaseID)
	})
}

func insertLifecycleCoverage(t *testing.T, pool *pgxpool.Pool, releaseID string) {
	t.Helper()
	if _, err := pool.Exec(context.Background(),
		`INSERT INTO rule_coverage (
			release_id, rule_type, scope, status, covered_data_sources, notes
		 ) VALUES (
			$1, 'SUBJECT_REQUIREMENT', '{}'::jsonb, 'FULL',
			'["major_admission"]'::jsonb, 'lifecycle fixture'
		 )`, releaseID,
	); err != nil {
		t.Fatalf("insert lifecycle coverage: %v", err)
	}
}

func lifecycleRuleDraft(releaseID, ruleID, ruleKey string) RuleDraft {
	return RuleDraft{
		RuleID: ruleID, Version: 1, RuleKey: ruleKey,
		RuleType:              biz.RuleTypeSubjectRequirement,
		RequiredProfileFields: []string{"subjects", "category"},
		Expression: biz.RuleExpression{
			Operator: biz.OperatorNot,
			Child:    &biz.RuleExpression{Operator: biz.OperatorSubjectMatch},
		},
		OutcomeWhenTrue: biz.OutcomeFail,
		ReasonCode:      "SUBJECT_NOT_MATCHED",
		SourceEvidence: []biz.EvidenceRef{{
			EvidenceID: "ev-" + ruleID, EvidenceType: "DOCUMENT",
			ReleaseID: releaseID, ContentSHA: "sha256:" + ruleID,
		}},
		CreatedBy: "rule-editor",
	}
}

func cleanupLifecycleRule(t *testing.T, pool *pgxpool.Pool, ruleID string) {
	t.Helper()
	t.Cleanup(func() {
		_, _ = pool.Exec(context.Background(), `DELETE FROM rule_version WHERE rule_id = $1`, ruleID)
	})
}

func assertLifecycleAudit(
	t *testing.T,
	pool *pgxpool.Pool,
	ruleID string,
	version int,
	want []string,
) {
	t.Helper()
	rows, err := pool.Query(context.Background(),
		`SELECT to_state
		 FROM rule_lifecycle_event
		 WHERE rule_id = $1 AND version = $2
		 ORDER BY event_id`, ruleID, version)
	if err != nil {
		t.Fatalf("query lifecycle audit: %v", err)
	}
	defer rows.Close()
	var got []string
	for rows.Next() {
		var state string
		if err := rows.Scan(&state); err != nil {
			t.Fatalf("scan audit state: %v", err)
		}
		got = append(got, state)
	}
	if err := rows.Err(); err != nil {
		t.Fatalf("iterate audit: %v", err)
	}
	if len(got) != len(want) {
		t.Fatalf("audit states = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("audit states = %v, want %v", got, want)
		}
	}
}

func assertLifecycleRevocationAudit(
	t *testing.T,
	pool *pgxpool.Pool,
	ruleID string,
	version int,
	wantActor string,
	wantReason string,
) {
	t.Helper()
	var actor, reason string
	if err := pool.QueryRow(context.Background(), `
		SELECT actor, reason
		FROM rule_lifecycle_event
		WHERE rule_id = $1 AND version = $2 AND to_state = 'REVOKED'
		ORDER BY event_id DESC
		LIMIT 1`, ruleID, version,
	).Scan(&actor, &reason); err != nil {
		t.Fatalf("query revoke audit: %v", err)
	}
	if actor != wantActor || reason != wantReason {
		t.Fatalf("revoke audit = (%q, %q), want (%q, %q)",
			actor, reason, wantActor, wantReason)
	}
}
