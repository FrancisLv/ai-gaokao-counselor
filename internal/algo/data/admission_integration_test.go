//go:build integration
// +build integration

// 集成测试：真实连本地 PostgreSQL 跑 data 层三段 SQL（LoadCandidates/BatchGroupHistory/BatchGroupPlan），
// 并串起 biz.RecommendCandidates 验证概率分档端到端。默认不随 `go test ./...` 运行（需 -tags integration）。
//
// 运行：
//
//	ALGO_TEST_DSN="postgres://francis@localhost:5432/ai_gaokao_counselor?sslmode=disable" \
//	  go test ./internal/algo/data/ -tags integration -v
//
// 未设 ALGO_TEST_DSN 时自动 skip（CI 无库不失败）。
package data

import (
	"context"
	"errors"
	"fmt"
	"os"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/choucisan/ai-gaokao-counselor/conf"
	"github.com/choucisan/ai-gaokao-counselor/internal/algo/biz"
	errs "github.com/choucisan/ai-gaokao-counselor/internal/pkg/errors"
)

const (
	itProvince = "湖南"
	itCategory = "物理类"
	itBatch    = "本科批"
	itRelease  = "rel_legacy_v1"
	itYear     = 2025 // 报考年（演示）；概率窗口=[2022,2024]，绝不含 2025
	itWindow   = 3
)

func setupRepo(t testing.TB) *admissionRepo {
	t.Helper()
	dsn := os.Getenv("ALGO_TEST_DSN")
	if dsn == "" {
		t.Skip("ALGO_TEST_DSN 未设置，跳过集成测试")
	}
	t.Setenv("ALGO_DATA_DSN", dsn)
	bc := &conf.Bootstrap{Data: &conf.Data{Database: &conf.Data_Database{Dsn: dsn}}}
	d, cleanup, err := NewData(bc)
	if err != nil {
		t.Fatalf("NewData: %v", err)
	}
	t.Cleanup(cleanup)
	return &admissionRepo{d: d}
}

func BenchmarkIntegration_LoadCandidates(b *testing.B) {
	repo := setupRepo(b)
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if _, err := repo.LoadCandidates(context.Background(), itRelease, itProvince, itCategory, itBatch, itYear); err != nil {
			b.Fatal(err)
		}
	}
}

func BenchmarkIntegration_BatchGroupHistory(b *testing.B) {
	repo := setupRepo(b)
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if _, err := repo.BatchGroupHistory(context.Background(), itRelease, itProvince, itCategory, itBatch, itYear, itWindow); err != nil {
			b.Fatal(err)
		}
	}
}

func TestIntegration_MissingEnrollmentPlan(t *testing.T) {
	repo := setupRepo(t)
	_, err := repo.LoadCandidates(context.Background(), itRelease, itProvince, itCategory, itBatch, 9999)
	if !errors.Is(err, errs.ErrMissingEnrollmentPlan) {
		t.Fatalf("缺报考年计划必须报错，got %v", err)
	}
}

func TestIntegration_CurrentPlanMembershipUsesReportYearPlan(t *testing.T) {
	dsn := os.Getenv("ALGO_TEST_DSN")
	if dsn == "" {
		t.Skip("ALGO_TEST_DSN 未设置，跳过集成测试")
	}
	pool, err := pgxpool.New(context.Background(), dsn)
	if err != nil {
		t.Fatalf("pgxpool.New: %v", err)
	}
	t.Cleanup(pool.Close)

	suffix := fmt.Sprintf("%d", time.Now().UnixNano())
	releaseID := "rel_m4b_membership_" + suffix
	universityCode := "T" + suffix
	majorGroup := "G" + suffix
	if _, err := pool.Exec(context.Background(), `
		INSERT INTO release (release_id, status, manifest, manifest_sha256, published_at)
		VALUES ($1, 'STAGING', '{}'::jsonb, 'sha256:m4b-membership', NULL)`,
		releaseID,
	); err != nil {
		t.Fatalf("insert release: %v", err)
	}
	t.Cleanup(func() {
		_, _ = pool.Exec(context.Background(), `DELETE FROM major_admission WHERE release_id = $1`, releaseID)
		_, _ = pool.Exec(context.Background(), `DELETE FROM enrollment_plan WHERE release_id = $1`, releaseID)
		_, _ = pool.Exec(context.Background(), `DELETE FROM major WHERE release_id = $1`, releaseID)
		_, _ = pool.Exec(context.Background(), `DELETE FROM university WHERE release_id = $1`, releaseID)
		_, _ = pool.Exec(context.Background(), `DELETE FROM release WHERE release_id = $1`, releaseID)
	})
	var universityID int64
	if err := pool.QueryRow(context.Background(), `
		INSERT INTO university (
			school_id, enrollment_code, name, province, release_id
		) VALUES ($1, $2, $3, '湖南', $4)
		RETURNING id`,
		"s"+suffix, universityCode, "成员核验测试大学", releaseID,
	).Scan(&universityID); err != nil {
		t.Fatalf("insert university: %v", err)
	}
	insertMajor := func(code, name string) int64 {
		t.Helper()
		var majorID int64
		if err := pool.QueryRow(context.Background(), `
			INSERT INTO major (
				university_id, major_code, major_name, major_group, release_id
			) VALUES ($1, $2, $3, $4, $5)
			RETURNING id`,
			universityID, code, name, majorGroup, releaseID,
		).Scan(&majorID); err != nil {
			t.Fatalf("insert major %s: %v", code, err)
		}
		return majorID
	}
	currentMajorID := insertMajor("M01", "当年计划专业")
	historicalOnlyMajorID := insertMajor("M02", "仅历史专业")
	newPlanMajorID := insertMajor("M03", "新计划专业")
	for _, row := range []struct {
		majorID int64
		code    string
		name    string
	}{
		{majorID: currentMajorID, code: "M01", name: "当年计划专业"},
		{majorID: historicalOnlyMajorID, code: "M02", name: "仅历史专业"},
	} {
		if _, err := pool.Exec(context.Background(), `
			INSERT INTO major_admission (
				university_id, major_id, release_id, province, year, category, batch,
				university_code, university_name, major_group, major_code, major_name,
				min_score, min_rank, subject_req
			) VALUES (
				$1, $2, $3, '湖南', 2024, '物理类', '本科批',
				$4, '成员核验测试大学', $5, $6, $7,
				600, 10000, '首选物理，再选不限'
			)`,
			universityID, row.majorID, releaseID, universityCode,
			majorGroup, row.code, row.name,
		); err != nil {
			t.Fatalf("insert historical major %s: %v", row.code, err)
		}
	}
	if _, err := pool.Exec(context.Background(), `
		INSERT INTO enrollment_plan (
			university_id, major_id, release_id, province, year, category, batch,
			university_code, university_name, major_group, major_code, major_name,
			plan_count
		) VALUES (
			$1, $2, $3, '湖南', 2025, '物理类', '本科批',
			$4, '成员核验测试大学', $5, 'M01', '当年计划专业', 10
		), (
			$1, $6, $3, '湖南', 2025, '物理类', '本科批',
			$4, '成员核验测试大学', $5, 'M03', '新计划专业', 8
		)`,
		universityID,
		currentMajorID,
		releaseID,
		universityCode,
		majorGroup,
		newPlanMajorID,
	); err != nil {
		t.Fatalf("insert enrollment plan: %v", err)
	}
	if _, err := pool.Exec(context.Background(), `
		INSERT INTO admission_group_lineage (
			release_id, target_year, target_category, target_batch,
			target_university_code, target_major_group,
			history_year, history_university_code, history_major_group,
			mapping_method, confidence,
			target_primary_subject, history_primary_subject,
			target_major_names, history_major_names, matched_major_names,
			target_major_count, history_major_count, matched_major_count,
			mapping_version
		) VALUES (
			$1, 2025, '物理类', '本科批', $2, $3,
			2024, $2, $3, 'EXACT_CODE', 1,
			'首选物理', '首选物理',
			ARRAY['当年计划专业','新计划专业'],
			ARRAY['当年计划专业','仅历史专业'],
			ARRAY['当年计划专业'],
			2, 2, 1, 'test-lineage-v1'
		)
		`,
		releaseID,
		universityCode,
		majorGroup,
	); err != nil {
		t.Fatalf("insert lineage: %v", err)
	}

	repo := setupRepo(t)
	candidates, err := repo.LoadCandidates(
		context.Background(), releaseID, "湖南", "物理类", "本科批", 2025,
	)
	if err != nil {
		t.Fatalf("LoadCandidates: %v", err)
	}
	if len(candidates) != 1 || len(candidates[0].Majors) != 2 {
		t.Fatalf("candidates = %+v, want one group with two current-plan majors", candidates)
	}
	got := map[string]bool{}
	for _, major := range candidates[0].Majors {
		got[major.MajorCode] = major.InCurrentPlan
	}
	if !reflect.DeepEqual(got, map[string]bool{"M01": true, "M03": true}) {
		t.Fatalf("current-plan members = %v, want M01/M03 from 2025 plan", got)
	}
}

func TestIntegration_LoadCandidates(t *testing.T) {
	repo := setupRepo(t)
	cands, err := repo.LoadCandidates(context.Background(), itRelease, itProvince, itCategory, itBatch, itYear)
	if err != nil {
		t.Fatalf("LoadCandidates: %v", err)
	}
	t.Logf("候选组数 = %d（2024 统计源 ∩ 2025 招生计划）", len(cands))
	if len(cands) < 1000 {
		t.Errorf("候选组数 = %d，异常偏少（预期 ~3000+）", len(cands))
	}

	// 候选主键唯一。跨年来源由 lineage 固定，不能再由历史明细的多重院校身份任取。
	seen := make(map[biz.GroupKey]bool)
	for _, c := range cands {
		key := biz.GroupKey{UniversityCode: c.UniversityCode, MajorGroup: c.MajorGroup}
		if seen[key] || key.UniversityCode == "" || key.MajorGroup == "" {
			t.Fatalf("候选主键重复或缺失: %+v", key)
		}
		seen[key] = true
	}

	// 抽查：找一个有专业明细的组，验证组装完整
	var sampled bool
	for _, c := range cands {
		if len(c.Majors) > 0 && c.GroupMinRank > 0 {
			t.Logf("样本组 %s%s（%s）：tier=%s location=%s 专业数=%d subjectReq=%v groupMinRank=%d",
				c.UniversityCode, c.MajorGroup, c.UniversityName, c.SchoolTier, c.Location, len(c.Majors), c.SubjectReq, c.GroupMinRank)
			if c.SchoolTier == "" {
				t.Errorf("SchoolTier 未派生")
			}
			if c.UniversityName == "" {
				t.Errorf("UniversityName 为空")
			}
			// 专业明细字段抽查
			m := c.Majors[0]
			if m.MajorName == "" {
				t.Errorf("首个专业 MajorName 为空")
			}
			sampled = true
			break
		}
	}
	if !sampled {
		t.Errorf("未找到任何带专业明细的候选组")
	}

	// 保序校验：candidates 按 (UniversityCode, MajorGroup) 升序（DH1 确定性）
	for i := 1; i < len(cands); i++ {
		prev := biz.GroupKey{UniversityCode: cands[i-1].UniversityCode, MajorGroup: cands[i-1].MajorGroup}
		cur := biz.GroupKey{UniversityCode: cands[i].UniversityCode, MajorGroup: cands[i].MajorGroup}
		if prev.UniversityCode > cur.UniversityCode ||
			(prev.UniversityCode == cur.UniversityCode && prev.MajorGroup > cur.MajorGroup) {
			t.Fatalf("候选未保序：index %d (%+v) 在 %d (%+v) 之前", i-1, prev, i, cur)
		}
	}
}

// 当前计划专业排序回归：专业代码稳定有序，重复查询的完整候选与选科列表一致。
func TestIntegration_LoadCandidates_Reproducible(t *testing.T) {
	repo := setupRepo(t)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	first, err := repo.LoadCandidates(ctx, itRelease, itProvince, itCategory, itBatch, itYear)
	if err != nil {
		t.Fatalf("LoadCandidates: %v", err)
	}
	for _, candidate := range first {
		if len(candidate.Majors) == 0 {
			t.Fatalf("候选 %s/%s 缺少当前计划专业", candidate.UniversityCode, candidate.MajorGroup)
		}
		for index, major := range candidate.Majors {
			if strings.TrimSpace(major.MajorCode) == "" {
				t.Fatalf("候选 %s/%s 含空专业代码", candidate.UniversityCode, candidate.MajorGroup)
			}
			if index > 0 && candidate.Majors[index-1].MajorCode > major.MajorCode {
				t.Fatalf(
					"候选 %s/%s 当前计划专业未按代码稳定排序",
					candidate.UniversityCode,
					candidate.MajorGroup,
				)
			}
		}
	}
	for i := 0; i < 3; i++ {
		next, err := repo.LoadCandidates(ctx, itRelease, itProvince, itCategory, itBatch, itYear)
		if err != nil {
			t.Fatalf("LoadCandidates 重复查询: %v", err)
		}
		if !reflect.DeepEqual(next, first) {
			t.Fatalf("第 %d 次重复查询完整候选不一致（含 Majors/SubjectReq 顺序）", i+1)
		}
	}
	t.Logf("%d 个候选完整内容重复 3 次一致，当前计划专业代码非空且稳定排序", len(first))
}

func TestIntegration_BatchGroupHistory_NoHoldoutLeak(t *testing.T) {
	repo := setupRepo(t)
	hm, err := repo.BatchGroupHistory(context.Background(), itRelease, itProvince, itCategory, itBatch, itYear, itWindow)
	if err != nil {
		t.Fatalf("BatchGroupHistory: %v", err)
	}
	t.Logf("历史组数 = %d", len(hm))
	if len(hm) < 1000 {
		t.Errorf("历史组数 = %d，异常偏少", len(hm))
	}

	// ⚠️ 核心：窗口=[2022,2024]，绝不能含报考年 2025（验证 holdout，防泄漏）；且每组按年降序
	for key, records := range hm {
		if len(records) == 0 {
			t.Fatalf("key %+v 有空 history slice", key)
		}
		for i, r := range records {
			if r.Year >= itYear {
				t.Fatalf("泄漏！key %+v 含报考年 %d（窗口上界须 ≤ %d）", key, r.Year, itYear-1)
			}
			if r.Year < itYear-itWindow {
				t.Fatalf("key %+v 含窗口外年份 %d（下界 %d）", key, r.Year, itYear-itWindow)
			}
			if r.GroupMinScore <= 0 || r.GroupMinRank <= 0 || r.TotalCount <= 0 {
				t.Fatalf("key %+v 记录字段异常：minScore=%d minRank=%d totalCount=%d", key, r.GroupMinScore, r.GroupMinRank, r.TotalCount)
			}
			// 降序校验（index 越小年份越近）
			if i > 0 && records[i-1].Year < r.Year {
				t.Fatalf("key %+v history 未按年降序：%d 在 %d 前", key, records[i-1].Year, r.Year)
			}
		}
	}
	zhongnan := hm[biz.GroupKey{
		UniversityCode: "4302",
		MajorGroup:     "（116）",
	}]
	foundZhongnan2024 := false
	for _, record := range zhongnan {
		if record.Year == 2024 {
			foundZhongnan2024 = true
			if record.GroupMinScore != 624 || record.GroupMinRank != 6798 {
				t.Fatalf(
					"中南大学 2025（116）→2024（107）历史组线 = %d/%d，want 624/6798（最低分及其对应位次）",
					record.GroupMinScore,
					record.GroupMinRank,
				)
			}
			break
		}
	}
	if !foundZhongnan2024 {
		t.Fatal("缺少中南大学 2025（116）→2024（107）历史样本")
	}
	// total_count 口径抽查：2024 湖南物理类全省总人数应为 313685（跨批次 MAX cumulative）
	var checked2024 bool
	for _, records := range hm {
		for _, r := range records {
			if r.Year == 2024 {
				if r.TotalCount != 313685 {
					t.Errorf("2024 total_count = %d, want 313685（全省总人数口径）", r.TotalCount)
				}
				checked2024 = true
				break
			}
		}
		if checked2024 {
			break
		}
	}
}

func TestIntegration_BatchMajorHistory_UsesCurrentPlanMajorIdentity(t *testing.T) {
	repo := setupRepo(t)
	candidates, err := repo.LoadCandidates(
		context.Background(),
		itRelease,
		itProvince,
		itCategory,
		itBatch,
		itYear,
	)
	if err != nil {
		t.Fatalf("LoadCandidates: %v", err)
	}
	history, err := repo.BatchMajorHistory(
		context.Background(),
		itRelease,
		itProvince,
		itCategory,
		itBatch,
		itYear,
		itWindow,
	)
	if err != nil {
		t.Fatalf("BatchMajorHistory: %v", err)
	}
	if len(history) == 0 {
		t.Fatal("BatchMajorHistory returned no records")
	}
	currentMajors := make(map[biz.MajorKey]string)
	for _, candidate := range candidates {
		for _, major := range candidate.Majors {
			currentMajors[biz.MajorKey{
				UniversityCode: candidate.UniversityCode,
				MajorGroup:     candidate.MajorGroup,
				MajorCode:      major.MajorCode,
			}] = major.MajorName
		}
	}
	for key, records := range history {
		currentName, ok := currentMajors[key]
		if !ok {
			t.Fatalf("history key not present in current plan majors: %+v", key)
		}
		for index, record := range records {
			if record.Year < itYear-itWindow || record.Year >= itYear {
				t.Fatalf("history year outside window: %+v", record)
			}
			if index > 0 && records[index-1].Year <= record.Year {
				t.Fatalf("major history is not year-descending: %+v", records)
			}
			if record.MajorName != currentName {
				t.Fatalf(
					"major history name %q does not match current plan name %q: key=%+v",
					record.MajorName,
					currentName,
					key,
				)
			}
		}
	}
}

func TestIntegration_BatchMajorHistory_MatchesRenumberedMajorByExactName(t *testing.T) {
	repo := setupRepo(t)
	history, err := repo.BatchMajorHistory(
		context.Background(),
		itRelease,
		itProvince,
		itCategory,
		itBatch,
		itYear,
		itWindow,
	)
	if err != nil {
		t.Fatalf("BatchMajorHistory: %v", err)
	}
	key := biz.MajorKey{
		UniversityCode: "3702",
		MajorGroup:     "（212）",
		MajorCode:      "51",
	}
	records := history[key]
	if len(records) != 2 {
		t.Fatalf(
			"中国海洋大学（212）数学与应用数学历史 = %+v, want 2023/2024",
			records,
		)
	}
	wants := []struct {
		year     int
		minScore int
		minRank  int
	}{
		{year: 2024, minScore: 599, minRank: 14586},
		{year: 2023, minScore: 600, minRank: 15149},
	}
	for index, want := range wants {
		if records[index].Year != want.year ||
			records[index].MinScore != want.minScore ||
			records[index].MinRank != want.minRank {
			t.Fatalf(
				"history[%d] = %+v, want year=%d score=%d rank=%d",
				index,
				records[index],
				want.year,
				want.minScore,
				want.minRank,
			)
		}
		if records[index].MajorName != "数学与应用数学" {
			t.Fatalf("history[%d] name = %q", index, records[index].MajorName)
		}
	}
}

func TestIntegration_BatchMajorHistory_MatchesRenamedGroupAndRenumberedMajors(t *testing.T) {
	repo := setupRepo(t)
	history, err := repo.BatchMajorHistory(
		context.Background(),
		itRelease,
		itProvince,
		itCategory,
		itBatch,
		itYear,
		itWindow,
	)
	if err != nil {
		t.Fatalf("BatchMajorHistory: %v", err)
	}
	key := biz.MajorKey{
		UniversityCode: "4302",
		MajorGroup:     "（217）",
		MajorCode:      "85",
	}
	records := history[key]
	if len(records) != 3 {
		t.Fatalf(
			"中南大学（217）数学与应用数学历史 = %+v, want 2022/2023/2024",
			records,
		)
	}
	wants := []struct {
		year     int
		minScore int
		minRank  int
	}{
		{year: 2024, minScore: 610, minRank: 10702},
		{year: 2023, minScore: 612, minRank: 10771},
		{year: 2022, minScore: 586, minRank: 13272},
	}
	for index, want := range wants {
		if records[index].Year != want.year ||
			records[index].MinScore != want.minScore ||
			records[index].MinRank != want.minRank {
			t.Fatalf(
				"history[%d] = %+v, want year=%d score=%d rank=%d",
				index,
				records[index],
				want.year,
				want.minScore,
				want.minRank,
			)
		}
		if records[index].MajorName != "数学与应用数学" {
			t.Fatalf("history[%d] name = %q", index, records[index].MajorName)
		}
	}
}

func TestIntegration_BatchGroupPlan(t *testing.T) {
	repo := setupRepo(t)
	pm, err := repo.BatchGroupPlan(context.Background(), itRelease, itProvince, itCategory, itYear, itYear-1)
	if err != nil {
		t.Fatalf("BatchGroupPlan: %v", err)
	}
	t.Logf("计划组数 = %d", len(pm))
	if len(pm) < 1000 {
		t.Errorf("计划组数 = %d，异常偏少", len(pm))
	}
	// 抽查：应有组同时含报考年与报考年-1 计划
	var bothYears int
	for _, pc := range pm {
		if pc.PlanCount > 0 && pc.PrevPlanCount > 0 {
			bothYears++
		}
	}
	t.Logf("同时有 2025+2024 计划的组数 = %d", bothYears)
	if bothYears == 0 {
		t.Errorf("无任何组同时含两年计划，PlanChange 组装可能有误")
	}
}

// TestIntegration_EndToEnd_Recommend 串起 data 三段 SQL + biz.RecommendCandidates，验证真实数据下能分出冲/稳/保。
func TestIntegration_EndToEnd_Recommend(t *testing.T) {
	repo := setupRepo(t)
	ctx := context.Background()

	cands, err := repo.LoadCandidates(ctx, itRelease, itProvince, itCategory, itBatch, itYear)
	if err != nil {
		t.Fatalf("LoadCandidates: %v", err)
	}
	hm, err := repo.BatchGroupHistory(ctx, itRelease, itProvince, itCategory, itBatch, itYear, itWindow)
	if err != nil {
		t.Fatalf("BatchGroupHistory: %v", err)
	}
	pm, err := repo.BatchGroupPlan(ctx, itRelease, itProvince, itCategory, itYear, itYear-1)
	if err != nil {
		t.Fatalf("BatchGroupPlan: %v", err)
	}

	// 用户百分位取一个中间值（真实场景由 rank.Convert 算）；此处直接给 0.30 观察分档
	cfg := biz.RecommendConfig{
		WindowYears: itWindow, DecayFactor: 0.7, PlanAdjust: 0.5,
		RushThreshold: 0.2, StableThreshold: 0.5, SafeThreshold: 0.8,
	}
	res := biz.RecommendCandidates(cands, 0.30, hm, pm, cfg)
	t.Logf("分档结果：冲=%d 稳=%d 保=%d 淘汰=%d 总候选=%d",
		res.RushTotal, res.StableTotal, res.SafeTotal, res.FilteredCount, res.TotalCandidates)
	if res.TotalCandidates == 0 {
		t.Errorf("端到端无任何带概率候选，history 与 candidates 的 GroupKey 可能不匹配")
	}
	if res.RushTotal+res.StableTotal+res.SafeTotal == 0 {
		t.Errorf("三档全空，概率计算可能有误")
	}
}

// TestIntegration_CheckConstraints_RealSubjectReq 用真实候选的 subject_req 验证选科解析/匹配。
// 核心断言：真实库全部 distinct subject_req 格式都能被 parseSubjectReq 解析（零“无法解析”拒绝），
// 且一个真实选科组合能正常分出 kept/rejected（kept+rejected==total、拒绝均为 subject_req 规则）。
func TestIntegration_CheckConstraints_RealSubjectReq(t *testing.T) {
	repo := setupRepo(t)
	cands, err := repo.LoadCandidates(context.Background(), itRelease, itProvince, itCategory, itBatch, itYear)
	if err != nil {
		t.Fatalf("LoadCandidates: %v", err)
	}
	userSubjects := []string{"物理", "化学", "生物"}
	query := biz.UserQuery{Subjects: userSubjects, Category: itCategory}

	// 1) 逐个 distinct subject_req 验证可解析（无法解析会产生 Reason 含“无法解析”的 rejected）
	seen := map[string]bool{}
	var parseFail []string
	for _, c := range cands {
		for _, sr := range c.SubjectReq {
			if seen[sr] {
				continue
			}
			seen[sr] = true
			_, rejected, _ := biz.CheckConstraints(query, []biz.Candidate{
				{UniversityCode: c.UniversityCode, MajorGroup: c.MajorGroup, SubjectReq: []string{sr}},
			})
			for _, r := range rejected {
				if strings.Contains(r.Reason, "无法解析") {
					parseFail = append(parseFail, sr)
				}
			}
		}
	}
	t.Logf("真实 distinct subject_req 文本数 = %d", len(seen))
	if len(parseFail) > 0 {
		t.Errorf("以下真实 subject_req 无法解析（parser 需修正）：%v", parseFail)
	}

	// 2) 全量选科过滤：kept + rejected == total，kept 非空，拒绝均为 subject_req 规则
	kept, rejected, err := biz.CheckConstraints(query, cands)
	if err != nil {
		t.Fatalf("CheckConstraints: %v", err)
	}
	t.Logf("选科%v过滤：kept=%d rejected=%d total=%d", userSubjects, len(kept), len(rejected), len(cands))
	if len(kept)+len(rejected) != len(cands) {
		t.Errorf("kept(%d)+rejected(%d) != total(%d)", len(kept), len(rejected), len(cands))
	}
	if len(kept) == 0 {
		t.Errorf("kept 为 0，选科过滤过严或解析全失败")
	}
	for _, r := range rejected {
		if r.Rule != biz.RuleSubjectReq {
			t.Errorf("rejected.Rule = %q, want %q", r.Rule, biz.RuleSubjectReq)
		}
	}
}

// TestIntegration_EndToEnd_Optimize 串起 data 三段 SQL + biz.RecommendCandidates + biz.OptimizeUsecase.OptimizeWishlist，
// 验证真实数据下能产出成品志愿表（utility 批量打分 → 配比选择 → 全序排序 → 保底健全性检查）。
// 百分位取 0.30（与 TestIntegration_EndToEnd_Recommend 同口径，实测保档 ~1051 个，稳拿池充足）。
func TestIntegration_EndToEnd_Optimize(t *testing.T) {
	repo := setupRepo(t)
	ctx := context.Background()

	cands, err := repo.LoadCandidates(ctx, itRelease, itProvince, itCategory, itBatch, itYear)
	if err != nil {
		t.Fatalf("LoadCandidates: %v", err)
	}
	hm, err := repo.BatchGroupHistory(ctx, itRelease, itProvince, itCategory, itBatch, itYear, itWindow)
	if err != nil {
		t.Fatalf("BatchGroupHistory: %v", err)
	}
	pm, err := repo.BatchGroupPlan(ctx, itRelease, itProvince, itCategory, itYear, itYear-1)
	if err != nil {
		t.Fatalf("BatchGroupPlan: %v", err)
	}

	cfg := biz.RecommendConfig{
		WindowYears: itWindow, DecayFactor: 0.7, PlanAdjust: 0.5,
		RushThreshold: 0.2, StableThreshold: 0.5, SafeThreshold: 0.8,
	}
	res := biz.RecommendCandidates(cands, 0.30, hm, pm, cfg)
	t.Logf("分档：冲=%d 稳=%d 保=%d 淘汰=%d", res.RushTotal, res.StableTotal, res.SafeTotal, res.FilteredCount)

	// 合并三档为单数组（模拟 orchestrator 合并 EstimateProb 结果后回传 OptimizeWishlist）
	pool := make([]biz.CandidateWithProb, 0, res.RushTotal+res.StableTotal+res.SafeTotal)
	pool = append(pool, res.RushCandidates...)
	pool = append(pool, res.StableCandidates...)
	pool = append(pool, res.SafeCandidates...)
	if len(pool) == 0 {
		t.Fatal("候选池为空，无法优化")
	}

	uc := biz.NewOptimizeUsecase(&conf.Bootstrap{})
	profile := biz.UserProfile{
		RiskAppetite:   "balanced",
		PriorityDims:   []string{"tier"},
		PreferredTiers: []string{"985", "211"},
	}
	wl, err := uc.OptimizeWishlist(ctx, pool, profile)
	if err != nil {
		t.Fatalf("OptimizeWishlist: %v", err)
	}
	t.Logf("志愿表：len=%d 冲=%d 稳=%d 稳拿保底=%d SafeGuardOK=%v 滑档率=%.3e 期望满意度=%.4f",
		len(wl.Candidates), wl.Report.RushCount, wl.Report.StableCount,
		wl.Report.SafeCount, wl.Report.SafeGuardOK, wl.Report.SlipRate, wl.Report.ExpectedSatisfaction)

	if len(wl.Candidates) == 0 {
		t.Fatal("志愿表为空")
	}
	// FinalRank 连续 1..N
	for i, c := range wl.Candidates {
		if c.FinalRank != i+1 {
			t.Errorf("Candidates[%d].FinalRank = %d, want %d", i, c.FinalRank, i+1)
		}
	}
	// 效用分已注入（非全零）
	if wl.Candidates[0].Score <= 0 {
		t.Errorf("首位候选效用分 = %v, want >0", wl.Candidates[0].Score)
	}
	// 保底达标时滑档率应极小（Π(1−pᵢ) 数量级 <1e-3）
	if wl.Report.SafeGuardOK && wl.Report.SlipRate > 1e-3 {
		t.Errorf("保底达标但滑档率偏高 = %v", wl.Report.SlipRate)
	}
	// 真实高分段保档充足，minSafe=9 应达标
	if !wl.Report.SafeGuardOK {
		t.Errorf("真实数据下保底未达标：SafeCount=%d", wl.Report.SafeCount)
	}
}
