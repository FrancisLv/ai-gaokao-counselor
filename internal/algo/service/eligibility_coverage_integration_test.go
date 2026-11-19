//go:build integration

package service

import (
	"context"
	"fmt"
	"os"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"google.golang.org/protobuf/proto"

	algov1 "github.com/choucisan/ai-gaokao-counselor/api/algo/v1"
	"github.com/choucisan/ai-gaokao-counselor/conf"
	"github.com/choucisan/ai-gaokao-counselor/internal/algo/biz"
	"github.com/choucisan/ai-gaokao-counselor/internal/algo/data"
)

func TestIntegration_EvaluateEligibility_RuleCoverage(t *testing.T) {
	dsn := os.Getenv("ALGO_TEST_DSN")
	if dsn == "" {
		t.Skip("ALGO_TEST_DSN 未设置，跳过集成测试")
	}
	pool, err := pgxpool.New(context.Background(), dsn)
	if err != nil {
		t.Fatalf("pgxpool.New: %v", err)
	}
	t.Cleanup(pool.Close)
	applyEligibilityCoverageTestMigrations(t, pool)

	t.Setenv("ALGO_DATA_DSN", dsn)
	bc := &conf.Bootstrap{Data: &conf.Data{Database: &conf.Data_Database{Dsn: dsn}}}
	d, cleanup, err := data.NewData(bc)
	if err != nil {
		t.Fatalf("NewData: %v", err)
	}
	t.Cleanup(cleanup)
	client := setupBufconnClient(t, NewAlgo(
		nil,
		nil,
		nil,
		biz.NewEligibilityUsecase(data.NewEligibilityRepo(d)),
	))

	t.Run("specific PARTIAL overrides broad FULL and remains frozen", func(t *testing.T) {
		releaseID := seedCoverageRelease(t, pool)
		insertEligibilityCoverage(t, pool, releaseID, `{}`, "FULL", `["enrollment_plan"]`, "broad full")
		insertEligibilityCoverage(
			t, pool, releaseID,
			`{"university_id":1}`, "PARTIAL", `[]`, "current-plan gap for school",
		)
		ruleID := "rule-coverage-partial-" + releaseID
		publishEligibilityRuleFixture(
			t, pool, releaseID, ruleID, "subject-requirement",
			"SUBJECT_REQUIREMENT", `["subjects","category"]`,
			`{"op":"NOT","child":{"op":"SUBJECT_MATCH","subject_req":"首选物理，再选化学"}}`, 1,
		)

		first, err := client.EvaluateEligibility(context.Background(), eligibilityCoverageRequest(releaseID))
		if err != nil {
			t.Fatalf("EvaluateEligibility: %v", err)
		}
		if first.GetStatus() != algov1.EligibilityResultStatus_ELIGIBILITY_RESULT_STATUS_PARTIAL {
			t.Fatalf("status = %v, want PARTIAL", first.GetStatus())
		}
		major := first.GetGroups()[0].GetMajors()[0]
		if major.GetStatus() != algov1.MajorEligibilityStatus_MAJOR_ELIGIBILITY_STATUS_UNKNOWN {
			t.Fatalf("major status = %v, want UNKNOWN", major.GetStatus())
		}
		if reasons := major.GetUnknownReasons(); len(reasons) != 1 || reasons[0] != biz.ReasonRuleCoverageMissing {
			t.Fatalf("unknown reasons = %v", reasons)
		}
		if _, err := pool.Exec(context.Background(), `
			UPDATE rule_coverage
			SET status = 'FULL', notes = 'mutated after publish'
			WHERE release_id = $1
			  AND scope = '{"university_id":1}'::jsonb`, releaseID,
		); err == nil {
			t.Fatal("published RuleCoverage update unexpectedly succeeded")
		}
		second, err := client.EvaluateEligibility(context.Background(), eligibilityCoverageRequest(releaseID))
		if err != nil {
			t.Fatalf("repeat EvaluateEligibility: %v", err)
		}
		if !proto.Equal(first, second) {
			t.Fatal("published RuleCoverage mutation changed eligibility result")
		}
	})

	t.Run("specific FULL can override broad NONE", func(t *testing.T) {
		releaseID := seedCoverageRelease(t, pool)
		insertEligibilityCoverage(t, pool, releaseID, `{}`, "NONE", `[]`, "broad gap")
		insertEligibilityCoverage(
			t, pool, releaseID,
			`{"university_id":1,"major_group":"01","major_code":"080901"}`,
			"FULL", `["enrollment_plan"]`, "candidate complete",
		)
		if _, err := pool.Exec(context.Background(), `
			UPDATE release SET status = 'PUBLISHED', published_at = clock_timestamp()
			WHERE release_id = $1`, releaseID,
		); err != nil {
			t.Fatalf("publish release: %v", err)
		}

		reply, err := client.EvaluateEligibility(context.Background(), eligibilityCoverageRequest(releaseID))
		if err != nil {
			t.Fatalf("EvaluateEligibility: %v", err)
		}
		if reply.GetStatus() != algov1.EligibilityResultStatus_ELIGIBILITY_RESULT_STATUS_COMPLETE {
			t.Fatalf("status = %v, want COMPLETE", reply.GetStatus())
		}
		if got := reply.GetGroups()[0].GetMajors()[0].GetStatus(); got != algov1.MajorEligibilityStatus_MAJOR_ELIGIBILITY_STATUS_PASS {
			t.Fatalf("major status = %v, want PASS", got)
		}
	})

	t.Run("equally specific conflict blocks", func(t *testing.T) {
		releaseID := seedCoverageRelease(t, pool)
		insertEligibilityCoverage(
			t, pool, releaseID,
			`{"university_id":1}`, "FULL", `["enrollment_plan"]`, "school full",
		)
		insertEligibilityCoverage(
			t, pool, releaseID,
			`{"major_group":"01"}`, "NONE", `[]`, "group gap",
		)
		if _, err := pool.Exec(context.Background(), `
			UPDATE release SET status = 'PUBLISHED', published_at = clock_timestamp()
			WHERE release_id = $1`, releaseID,
		); err != nil {
			t.Fatalf("publish release: %v", err)
		}

		reply, err := client.EvaluateEligibility(context.Background(), eligibilityCoverageRequest(releaseID))
		if err != nil {
			t.Fatalf("EvaluateEligibility: %v", err)
		}
		if reply.GetStatus() != algov1.EligibilityResultStatus_ELIGIBILITY_RESULT_STATUS_BLOCKED {
			t.Fatalf("status = %v, want BLOCKED", reply.GetStatus())
		}
		if got := reply.GetDiagnostics().GetConflictCount(); got != 1 {
			t.Fatalf("conflict count = %d, want 1", got)
		}
		reasons := reply.GetGroups()[0].GetMajors()[0].GetUnknownReasons()
		if len(reasons) != 1 || reasons[0] != biz.ReasonRuleConflict {
			t.Fatalf("unknown reasons = %v", reasons)
		}
	})

	for _, tc := range []struct {
		name   string
		status string
		notes  string
	}{
		{name: "NONE", status: "NONE", notes: "no rule coverage"},
		{name: "missing", status: "", notes: ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			releaseID := seedCoverageRelease(t, pool)
			if tc.status != "" {
				insertEligibilityCoverage(t, pool, releaseID, `{}`, tc.status, `[]`, tc.notes)
			}
			if _, err := pool.Exec(context.Background(), `
				UPDATE release SET status = 'PUBLISHED', published_at = clock_timestamp()
				WHERE release_id = $1`, releaseID,
			); err != nil {
				t.Fatalf("publish release: %v", err)
			}
			reply, err := client.EvaluateEligibility(context.Background(), eligibilityCoverageRequest(releaseID))
			if err != nil {
				t.Fatalf("EvaluateEligibility: %v", err)
			}
			if reply.GetStatus() != algov1.EligibilityResultStatus_ELIGIBILITY_RESULT_STATUS_PARTIAL {
				t.Fatalf("status = %v, want PARTIAL", reply.GetStatus())
			}
			reasons := reply.GetGroups()[0].GetMajors()[0].GetUnknownReasons()
			if len(reasons) != 1 || reasons[0] != biz.ReasonRuleCoverageMissing {
				t.Fatalf("unknown reasons = %v", reasons)
			}
		})
	}
}

func seedCoverageRelease(t *testing.T, pool *pgxpool.Pool) string {
	t.Helper()
	releaseID := fmt.Sprintf("rel_m4b_coverage_seam_%d", time.Now().UnixNano())
	if _, err := pool.Exec(context.Background(), `
		INSERT INTO release (release_id, status, manifest, manifest_sha256, published_at)
		VALUES ($1, 'STAGING', '{}'::jsonb, 'sha256:m4b-coverage-seam', NULL)`,
		releaseID,
	); err != nil {
		t.Fatalf("insert staging release: %v", err)
	}
	if _, err := pool.Exec(context.Background(), `
		INSERT INTO release_scope (
			release_id, province, application_year, category, batch
		) VALUES ($1, '湖南', 2025, '物理类', '本科批')`,
		releaseID,
	); err != nil {
		t.Fatalf("insert staging release scope: %v", err)
	}
	t.Cleanup(func() {
		_, _ = pool.Exec(context.Background(), `DELETE FROM release WHERE release_id = $1`, releaseID)
	})
	return releaseID
}

func applyEligibilityCoverageTestMigrations(t *testing.T, pool *pgxpool.Pool) {
	t.Helper()
	var tableExists bool
	if err := pool.QueryRow(context.Background(),
		`SELECT to_regclass('rule_coverage') IS NOT NULL`,
	).Scan(&tableExists); err != nil {
		t.Fatalf("inspect eligibility tables: %v", err)
	}
	if !tableExists {
		applyCoverageSQLFile(t, pool, "21_eligibility_rules.sql")
	}

	for _, migration := range []struct {
		file   string
		table  string
		column string
	}{
		{file: "22_rule_lifecycle.sql", table: "rule_version", column: "created_by"},
		{file: "23_rule_expression_contract.sql", table: "rule_version", column: "expression_version"},
	} {
		var present bool
		if err := pool.QueryRow(context.Background(), `
			SELECT EXISTS (
				SELECT 1
				FROM information_schema.columns
				WHERE table_schema = current_schema()
				  AND table_name = $1
				  AND column_name = $2
			)`, migration.table, migration.column,
		).Scan(&present); err != nil {
			t.Fatalf("inspect %s.%s: %v", migration.table, migration.column, err)
		}
		if !present {
			applyCoverageSQLFile(t, pool, migration.file)
		}
	}
	var profileFieldTableExists bool
	if err := pool.QueryRow(context.Background(),
		`SELECT to_regclass('eligibility_profile_field') IS NOT NULL`,
	).Scan(&profileFieldTableExists); err != nil {
		t.Fatalf("inspect eligibility profile field table: %v", err)
	}
	if !profileFieldTableExists {
		applyCoverageSQLFile(t, pool, "24_eligibility_profile_fields.sql")
	}
	applyCoverageSQLFile(t, pool, "25_rule_coverage_contract.sql")
	var profileFieldReleaseBound bool
	if err := pool.QueryRow(context.Background(), `
		SELECT EXISTS (
			SELECT 1
			FROM information_schema.columns
			WHERE table_schema = current_schema()
			  AND table_name = 'eligibility_profile_field'
			  AND column_name = 'release_id'
		)`,
	).Scan(&profileFieldReleaseBound); err != nil {
		t.Fatalf("inspect release-bound profile field catalog: %v", err)
	}
	if !profileFieldReleaseBound {
		applyCoverageSQLFile(t, pool, "26_release_bound_profile_fields.sql")
	}
}

func applyCoverageSQLFile(t *testing.T, pool *pgxpool.Pool, name string) {
	t.Helper()
	body, err := os.ReadFile("../../../migrations/" + name)
	if err != nil {
		t.Fatalf("read migration %s: %v", name, err)
	}
	if _, err := pool.Exec(context.Background(), string(body)); err != nil {
		t.Fatalf("apply migration %s: %v", name, err)
	}
}

func insertEligibilityCoverage(
	t *testing.T,
	pool *pgxpool.Pool,
	releaseID string,
	scope string,
	status string,
	sources string,
	notes string,
) {
	t.Helper()
	if _, err := pool.Exec(context.Background(), `
		INSERT INTO rule_coverage (
			release_id, rule_type, scope, status, covered_data_sources, notes
		) VALUES ($1, 'SUBJECT_REQUIREMENT', $2::jsonb, $3, $4::jsonb, $5)`,
		releaseID, scope, status, sources, notes,
	); err != nil {
		t.Fatalf("insert RuleCoverage %s %s: %v", scope, status, err)
	}
}

func eligibilityCoverageRequest(releaseID string) *algov1.EvaluateEligibilityRequest {
	request := &algov1.EvaluateEligibilityRequest{
		ReleaseId: releaseID, ConfirmedProfileVersion: "profile-coverage-v1",
		ProfileLifecycle: algov1.EligibilityProfileLifecycle_ELIGIBILITY_PROFILE_LIFECYCLE_CONFIRMED,
		Scope:            &algov1.EligibilityScope{Province: "湖南", ApplicationYear: 2025, Category: "物理类", Batch: "本科批"},
		ProfileFacts: []*algov1.EligibilityProfileFact{
			{
				FieldKey: "subjects", State: algov1.EligibilityProfileFieldState_ELIGIBILITY_PROFILE_FIELD_STATE_PROVIDED,
				StringValues: []string{"物理", "化学", "生物"},
			},
			{
				FieldKey: "category", State: algov1.EligibilityProfileFieldState_ELIGIBILITY_PROFILE_FIELD_STATE_PROVIDED,
				StringValue: "物理类",
			},
		},
		CandidateGroups: []*algov1.EligibilityCandidateGroup{{
			UniversityId: 1, UniversityCode: "1001", UniversityName: "测试大学", MajorGroup: "01",
			Majors: []*algov1.EligibilityCandidateMajor{{
				MajorCode: "080901", MajorName: "计算机科学与技术",
				SubjectRequirement: "首选物理，再选化学", InCurrentPlan: true,
			}},
		}},
	}
	setEligibilityProfileDigest(request)
	return request
}
