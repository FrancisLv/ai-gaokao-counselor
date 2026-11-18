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
	"os"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/choucisan/ai-gaokao-counselor/conf"
	"github.com/choucisan/ai-gaokao-counselor/internal/algo/biz"
	errs "github.com/choucisan/ai-gaokao-counselor/internal/pkg/errors"
)

const (
	itProvince = "湖南"
	itCategory = "物理类"
	itBatch    = "本科批"
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
		if _, err := repo.LoadCandidates(context.Background(), itProvince, itCategory, itBatch, itYear); err != nil {
			b.Fatal(err)
		}
	}
}

func BenchmarkIntegration_BatchGroupHistory(b *testing.B) {
	repo := setupRepo(b)
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if _, err := repo.BatchGroupHistory(context.Background(), itProvince, itCategory, itBatch, itYear, itWindow); err != nil {
			b.Fatal(err)
		}
	}
}

func TestIntegration_MissingEnrollmentPlan(t *testing.T) {
	repo := setupRepo(t)
	_, err := repo.LoadCandidates(context.Background(), itProvince, itCategory, itBatch, 9999)
	if !errors.Is(err, errs.ErrMissingEnrollmentPlan) {
		t.Fatalf("缺报考年计划必须报错，got %v", err)
	}
}

func TestIntegration_LoadCandidates(t *testing.T) {
	repo := setupRepo(t)
	cands, err := repo.LoadCandidates(context.Background(), itProvince, itCategory, itBatch, itYear)
	if err != nil {
		t.Fatalf("LoadCandidates: %v", err)
	}
	t.Logf("候选组数 = %d（2024 统计源 ∩ 2025 招生计划）", len(cands))
	if len(cands) < 1000 {
		t.Errorf("候选组数 = %d，异常偏少（预期 ~3000+）", len(cands))
	}

	// 候选主键唯一；历史数据中 1412/（105）关联不同院校身份，须剔除而非任取其一。
	seen := make(map[biz.GroupKey]bool)
	for _, c := range cands {
		key := biz.GroupKey{UniversityCode: c.UniversityCode, MajorGroup: c.MajorGroup}
		if seen[key] || key.UniversityCode == "" || key.MajorGroup == "" {
			t.Fatalf("候选主键重复或缺失: %+v", key)
		}
		seen[key] = true
		if key.UniversityCode == "1412" && key.MajorGroup == "（105）" {
			t.Fatal("院校身份冲突的组不应被推荐")
		}
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

// 并列专业排序回归：既固定已知同位次样本的次序，也比较重复查询的完整候选与选科列表。
func TestIntegration_LoadCandidates_Reproducible(t *testing.T) {
	repo := setupRepo(t)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	first, err := repo.LoadCandidates(ctx, itProvince, itCategory, itBatch, itYear)
	if err != nil {
		t.Fatalf("LoadCandidates: %v", err)
	}
	var tiedCodes []string
	for _, c := range first {
		if c.UniversityCode == "1120" && c.MajorGroup == "（104）" {
			for _, m := range c.Majors {
				if m.MinRank == 11037 {
					tiedCodes = append(tiedCodes, m.MajorCode)
				}
			}
			break
		}
	}
	if !reflect.DeepEqual(tiedCodes, []string{"21", "24"}) {
		t.Fatalf("1120/（104）同位次专业次序 = %v, want [21 24]", tiedCodes)
	}
	for i := 0; i < 3; i++ {
		next, err := repo.LoadCandidates(ctx, itProvince, itCategory, itBatch, itYear)
		if err != nil {
			t.Fatalf("LoadCandidates 重复查询: %v", err)
		}
		if !reflect.DeepEqual(next, first) {
			t.Fatalf("第 %d 次重复查询完整候选不一致（含 Majors/SubjectReq 顺序）", i+1)
		}
	}
	t.Logf("%d 个候选完整内容重复 3 次一致，并列专业次序固定为 %v", len(first), tiedCodes)
}

func TestIntegration_BatchGroupHistory_NoHoldoutLeak(t *testing.T) {
	repo := setupRepo(t)
	hm, err := repo.BatchGroupHistory(context.Background(), itProvince, itCategory, itBatch, itYear, itWindow)
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
			if r.GroupMinRank <= 0 || r.TotalCount <= 0 {
				t.Fatalf("key %+v 记录字段异常：minRank=%d totalCount=%d", key, r.GroupMinRank, r.TotalCount)
			}
			// 降序校验（index 越小年份越近）
			if i > 0 && records[i-1].Year < r.Year {
				t.Fatalf("key %+v history 未按年降序：%d 在 %d 前", key, records[i-1].Year, r.Year)
			}
		}
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

func TestIntegration_BatchGroupPlan(t *testing.T) {
	repo := setupRepo(t)
	pm, err := repo.BatchGroupPlan(context.Background(), itProvince, itCategory, itYear, itYear-1)
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

	cands, err := repo.LoadCandidates(ctx, itProvince, itCategory, itBatch, itYear)
	if err != nil {
		t.Fatalf("LoadCandidates: %v", err)
	}
	hm, err := repo.BatchGroupHistory(ctx, itProvince, itCategory, itBatch, itYear, itWindow)
	if err != nil {
		t.Fatalf("BatchGroupHistory: %v", err)
	}
	pm, err := repo.BatchGroupPlan(ctx, itProvince, itCategory, itYear, itYear-1)
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
	cands, err := repo.LoadCandidates(context.Background(), itProvince, itCategory, itBatch, itYear)
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

	cands, err := repo.LoadCandidates(ctx, itProvince, itCategory, itBatch, itYear)
	if err != nil {
		t.Fatalf("LoadCandidates: %v", err)
	}
	hm, err := repo.BatchGroupHistory(ctx, itProvince, itCategory, itBatch, itYear, itWindow)
	if err != nil {
		t.Fatalf("BatchGroupHistory: %v", err)
	}
	pm, err := repo.BatchGroupPlan(ctx, itProvince, itCategory, itYear, itYear-1)
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
