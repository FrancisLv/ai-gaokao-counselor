//go:build integration

package service

import (
	"context"
	"os"
	"testing"

	"github.com/jackc/pgx/v5/pgxpool"
	"google.golang.org/protobuf/proto"

	algov1 "github.com/choucisan/ai-gaokao-counselor/api/algo/v1"
	"github.com/choucisan/ai-gaokao-counselor/conf"
	"github.com/choucisan/ai-gaokao-counselor/internal/algo/biz"
	"github.com/choucisan/ai-gaokao-counselor/internal/algo/data"
)

func TestIntegration_EvaluateEligibility_GroupAggregation(t *testing.T) {
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

	releaseID := seedCoverageRelease(t, pool)
	ruleID := "rule-group-aggregation-" + releaseID
	publishEligibilityRuleFixture(
		t, pool, releaseID, ruleID, "subject-requirement",
		"SUBJECT_REQUIREMENT", `["subjects","category"]`,
		`{"op":"NOT","child":{"op":"SUBJECT_MATCH"}}`, 1,
		"SUBJECT_REQUIREMENT",
	)

	t.Run("PASS_PARTIAL preserves restricted majors", func(t *testing.T) {
		request := eligibilityCoverageRequest(releaseID)
		request.CandidateGroups[0].Majors = []*algov1.EligibilityCandidateMajor{
			{
				MajorCode: "080903", MajorName: "计划外专业",
				SubjectRequirement: "首选物理，再选化学", InCurrentPlan: false,
			},
			{
				MajorCode: "080902", MajorName: "受限专业",
				SubjectRequirement: "首选物理，再选思想政治", InCurrentPlan: true,
			},
			{
				MajorCode: "080901", MajorName: "可报专业",
				SubjectRequirement: "首选物理，再选化学", InCurrentPlan: true,
			},
		}

		first, err := client.EvaluateEligibility(context.Background(), request)
		if err != nil {
			t.Fatalf("EvaluateEligibility: %v", err)
		}
		second, err := client.EvaluateEligibility(context.Background(), request)
		if err != nil {
			t.Fatalf("repeat EvaluateEligibility: %v", err)
		}
		if !proto.Equal(first, second) {
			t.Fatal("same group input changed eligibility result")
		}
		if got := first.GetStatus(); got != algov1.EligibilityResultStatus_ELIGIBILITY_RESULT_STATUS_COMPLETE {
			t.Fatalf("status = %v, want COMPLETE", got)
		}
		group := first.GetGroups()[0]
		if got := group.GetStatus(); got != algov1.GroupEligibilityStatus_GROUP_ELIGIBILITY_STATUS_PASS_PARTIAL {
			t.Fatalf("group status = %v, want PASS_PARTIAL", got)
		}
		want := []struct {
			code   string
			status algov1.MajorEligibilityStatus
		}{
			{code: "080901", status: algov1.MajorEligibilityStatus_MAJOR_ELIGIBILITY_STATUS_PASS},
			{code: "080902", status: algov1.MajorEligibilityStatus_MAJOR_ELIGIBILITY_STATUS_FAIL},
			{code: "080903", status: algov1.MajorEligibilityStatus_MAJOR_ELIGIBILITY_STATUS_NOT_APPLICABLE},
		}
		if len(group.GetMajors()) != len(want) {
			t.Fatalf("majors = %d, want %d", len(group.GetMajors()), len(want))
		}
		for index, expected := range want {
			actual := group.GetMajors()[index]
			if actual.GetMajorCode() != expected.code || actual.GetStatus() != expected.status {
				t.Fatalf("major %d = code %q status %v, want %q %v",
					index, actual.GetMajorCode(), actual.GetStatus(), expected.code, expected.status)
			}
		}
		restricted := group.GetMajors()[1]
		if len(restricted.GetFailureRuleKeys()) != 1 ||
			restricted.GetFailureRuleKeys()[0] != "subject-requirement" {
			t.Fatalf("restricted failure keys = %v", restricted.GetFailureRuleKeys())
		}
	})

	t.Run("invalid candidate identities are blocked", func(t *testing.T) {
		cases := []struct {
			name            string
			mutate          func(*algov1.EvaluateEligibilityRequest)
			wantMajorReason string
		}{
			{
				name: "empty group",
				mutate: func(request *algov1.EvaluateEligibilityRequest) {
					request.CandidateGroups[0].Majors = nil
				},
			},
			{
				name: "duplicate major",
				mutate: func(request *algov1.EvaluateEligibilityRequest) {
					request.CandidateGroups[0].Majors = append(
						request.CandidateGroups[0].Majors,
						request.CandidateGroups[0].Majors[0],
					)
				},
				wantMajorReason: biz.ReasonCandidateMajorDuplicate,
			},
			{
				name: "incomplete scope",
				mutate: func(request *algov1.EvaluateEligibilityRequest) {
					request.Scope.Batch = ""
				},
				wantMajorReason: biz.ReasonEligibilityScopeInvalid,
			},
		}
		for _, tc := range cases {
			t.Run(tc.name, func(t *testing.T) {
				request := eligibilityCoverageRequest(releaseID)
				tc.mutate(request)
				reply, err := client.EvaluateEligibility(context.Background(), request)
				if err != nil {
					t.Fatalf("EvaluateEligibility: %v", err)
				}
				if got := reply.GetStatus(); got != algov1.EligibilityResultStatus_ELIGIBILITY_RESULT_STATUS_BLOCKED {
					t.Fatalf("status = %v, want BLOCKED", got)
				}
				groups := reply.GetGroups()
				if len(groups) != 1 {
					t.Fatalf("groups = %v", groups)
				}
				if groups[0].GetStatus() != algov1.GroupEligibilityStatus_GROUP_ELIGIBILITY_STATUS_UNKNOWN {
					t.Fatalf("group status = %v, want UNKNOWN", groups[0].GetStatus())
				}
				if tc.wantMajorReason == "" {
					if reply.GetDiagnostics().GetUnknownCount() == 0 {
						t.Fatal("empty group did not increment unknown diagnostics")
					}
					return
				}
				reasons := groups[0].GetMajors()[0].GetUnknownReasons()
				if len(reasons) != 1 || reasons[0] != tc.wantMajorReason {
					t.Fatalf("unknown reasons = %v, want [%s]", reasons, tc.wantMajorReason)
				}
			})
		}
	})

	t.Run("missing required fact makes the whole group unknown", func(t *testing.T) {
		missingReleaseID := seedCoverageRelease(t, pool)
		missingRuleID := "rule-group-missing-" + missingReleaseID
		publishEligibilityRuleFixture(
			t, pool, missingReleaseID, missingRuleID, "gender-restriction",
			"GENDER_RESTRICTION", `["gender"]`,
			`{"op":"EQ","field":"gender","value":"FEMALE"}`, 1,
			"SUBJECT_REQUIREMENT", "GENDER_RESTRICTION",
		)
		request := eligibilityCoverageRequest(missingReleaseID)
		request.CandidateGroups[0].Majors = []*algov1.EligibilityCandidateMajor{
			{
				MajorCode: "080901", MajorName: "可报专业",
				SubjectRequirement: "首选物理，再选化学", InCurrentPlan: true,
			},
			{
				MajorCode: "080902", MajorName: "待补事实专业",
				SubjectRequirement: "首选物理，再选化学", InCurrentPlan: true,
			},
		}

		reply, err := client.EvaluateEligibility(context.Background(), request)
		if err != nil {
			t.Fatalf("EvaluateEligibility: %v", err)
		}
		if got := reply.GetStatus(); got != algov1.EligibilityResultStatus_ELIGIBILITY_RESULT_STATUS_PARTIAL {
			t.Fatalf("status = %v, want PARTIAL", got)
		}
		group := reply.GetGroups()[0]
		if got := group.GetStatus(); got != algov1.GroupEligibilityStatus_GROUP_ELIGIBILITY_STATUS_UNKNOWN {
			t.Fatalf("group status = %v, want UNKNOWN", got)
		}
		for _, major := range group.GetMajors() {
			if got := major.GetStatus(); got != algov1.MajorEligibilityStatus_MAJOR_ELIGIBILITY_STATUS_MISSING_REQUIRED_FACT {
				t.Fatalf("major %s status = %v, want MISSING_REQUIRED_FACT", major.GetMajorCode(), got)
			}
			if got := major.GetMissingFields(); len(got) != 1 || got[0] != biz.FieldGender {
				t.Fatalf("major %s missing fields = %v, want [%s]",
					major.GetMajorCode(), got, biz.FieldGender)
			}
		}
	})
}
