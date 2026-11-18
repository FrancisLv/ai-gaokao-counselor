package biz

import (
	"reflect"
	"strings"
	"testing"
)

func BenchmarkCheckConstraints(b *testing.B) {
	candidates, _ := benchmarkCandidates()
	query := UserQuery{Category: "物理类", Subjects: []string{"物理", "化学", "生物"}}
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		kept, rejected, err := CheckConstraints(query, candidates)
		if err != nil || len(kept)+len(rejected) != len(candidates) {
			b.Fatal("候选丢失", err)
		}
	}
}

func TestCheckConstraints_RequestIsolation(t *testing.T) {
	candidates, _ := benchmarkCandidates()
	queries := []UserQuery{
		{Subjects: []string{"物理", "化学", "生物"}},
		{Subjects: []string{"历史", "政治", "地理"}},
		{Subjects: []string{"物理", "政治", "地理"}},
	}
	for _, q := range queries {
		var wantKept []Candidate
		var wantRejected []RejectedCandidate
		for _, cand := range candidates {
			matched, reason, err := matchCandidateSubjects(q.Subjects, cand.SubjectReq)
			if err != nil {
				t.Fatal(err)
			}
			if matched {
				wantKept = append(wantKept, cand)
			} else {
				wantRejected = append(wantRejected, RejectedCandidate{CandidateWithProb: CandidateWithProb{Candidate: cand}, Rule: RuleSubjectReq, Reason: reason})
			}
		}
		kept, rejected, err := CheckConstraints(q, candidates)
		if err != nil {
			t.Fatal(err)
		}
		if len(kept) != len(wantKept) || len(rejected) != len(wantRejected) {
			t.Fatal("数量与逐项对照不一致")
		}
		for i := range kept {
			if !reflect.DeepEqual(kept[i], wantKept[i]) {
				t.Fatal("通过项或顺序变化")
			}
		}
		for i := range rejected {
			if !reflect.DeepEqual(rejected[i], wantRejected[i]) {
				t.Fatal("拒绝项、原因或顺序变化")
			}
		}
	}
}

// mustParseReq 测试辅助：解析选科要求，失败即 fatal。
func mustParseReq(t *testing.T, text string) SubjectReq {
	t.Helper()
	r, err := parseSubjectReq(text)
	if err != nil {
		t.Fatalf("parseSubjectReq(%q) unexpected err: %v", text, err)
	}
	return r
}

func TestNormalizeSubject(t *testing.T) {
	cases := map[string]string{
		"政治":   "思想政治",
		"思想政治": "思想政治",
		"化学":   "化学",
		" 生物 ": "生物", // TrimSpace
		"":     "",
	}
	for in, want := range cases {
		if got := normalizeSubject(in); got != want {
			t.Errorf("normalizeSubject(%q) = %q, want %q", in, got, want)
		}
	}
}

// TestParseSubjectReq 覆盖真实 27 种 distinct 的代表格式 + 边界（空串、全角括号防御）。
func TestParseSubjectReq(t *testing.T) {
	cases := []struct {
		name string
		text string
		want SubjectReq
	}{
		{"物理+单科化学", "首选物理，再选化学", SubjectReq{firstPhysics, reselectAll, []string{"化学"}}},
		{"物理+不限", "首选物理，再选不限", SubjectReq{firstPhysics, reselectNone, nil}},
		{"历史+不限", "首选历史，再选不限", SubjectReq{firstHistory, reselectNone, nil}},
		{"物理+2选1", "首选物理，再选化学/生物(2选1)", SubjectReq{firstPhysics, reselectAtLeastOne, []string{"化学", "生物"}}},
		{"历史+思想政治单科", "首选历史，再选思想政治", SubjectReq{firstHistory, reselectAll, []string{"思想政治"}}},
		{"物理+2科必选", "首选物理，再选化学、生物(2科必选)", SubjectReq{firstPhysics, reselectAll, []string{"化学", "生物"}}},
		{"不限+不限", "首选不限，再选不限", SubjectReq{firstAny, reselectNone, nil}},
		{"历史+思想政治地理2科必选", "首选历史，再选思想政治、地理(2科必选)", SubjectReq{firstHistory, reselectAll, []string{"思想政治", "地理"}}},
		{"物理+思想政治地理2选1", "首选物理，再选思想政治/地理(2选1)", SubjectReq{firstPhysics, reselectAtLeastOne, []string{"思想政治", "地理"}}},
		{"物理+生物地理2科必选", "首选物理，再选生物、地理(2科必选)", SubjectReq{firstPhysics, reselectAll, []string{"生物", "地理"}}},
		{"空串→不限", "", SubjectReq{firstAny, reselectNone, nil}},
		{"全角括号防御归一", "首选物理，再选化学/生物（2选1）", SubjectReq{firstPhysics, reselectAtLeastOne, []string{"化学", "生物"}}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := mustParseReq(t, c.text)
			if got.FirstChoice != c.want.FirstChoice || got.ReselectMode != c.want.ReselectMode {
				t.Errorf("parseSubjectReq(%q) = {%q,%q}, want {%q,%q}",
					c.text, got.FirstChoice, got.ReselectMode, c.want.FirstChoice, c.want.ReselectMode)
			}
			if !reflect.DeepEqual(got.Reselect, c.want.Reselect) {
				t.Errorf("parseSubjectReq(%q).Reselect = %v, want %v", c.text, got.Reselect, c.want.Reselect)
			}
		})
	}
}

// TestParseSubjectReq_Errors 未知格式必须返回 error（硬约束保守剔除）。
func TestParseSubjectReq_Errors(t *testing.T) {
	bad := []string{
		"物理化学", // 无全角逗号
		"首选物理", // 只有一段
		"首选物理，再选化学，再选生物",    // 3 段
		"首选生物，再选化学",         // 未知首选（生物不是首选科目）
		"首选物理，再选化学/生物",      // 有分隔符无模式标记
		"首选物理，再选化学/生物(3选2)", // 未知模式标记
		"首选物理，再选化学、生物",      // 顿号无标记
	}
	for _, text := range bad {
		if _, err := parseSubjectReq(text); err == nil {
			t.Errorf("parseSubjectReq(%q) 应返回 error，got nil", text)
		}
	}
}

func TestMatchSubjects(t *testing.T) {
	cases := []struct {
		name string
		user []string
		req  string
		want bool
	}{
		{"不限放过任意用户", []string{"物理", "化学", "生物"}, "首选物理，再选不限", true},
		{"首选不符→false", []string{"历史", "化学", "生物"}, "首选物理，再选不限", false},
		{"首选不限→忽略用户首选", []string{"历史", "化学", "生物"}, "首选不限，再选不限", true},
		{"单科必选命中", []string{"物理", "化学", "生物"}, "首选物理，再选化学", true},
		{"单科必选未命中", []string{"物理", "政治", "地理"}, "首选物理，再选化学", false},
		{"2选1命中其一", []string{"物理", "化学", "政治"}, "首选物理，再选化学/生物(2选1)", true},
		{"2选1命中另一", []string{"物理", "生物", "地理"}, "首选物理，再选化学/生物(2选1)", true},
		{"2选1全不命中", []string{"物理", "政治", "地理"}, "首选物理，再选化学/生物(2选1)", false},
		{"2科必选都有", []string{"物理", "化学", "生物"}, "首选物理，再选化学、生物(2科必选)", true},
		{"2科必选缺一", []string{"物理", "化学", "政治"}, "首选物理，再选化学、生物(2科必选)", false},
		{"政治归一化为思想政治(单科)", []string{"物理", "政治", "地理"}, "首选物理，再选思想政治", true},
		{"政治归一化(2选1)", []string{"历史", "政治", "地理"}, "首选历史，再选思想政治/地理(2选1)", true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := matchSubjects(c.user, mustParseReq(t, c.req)); got != c.want {
				t.Errorf("matchSubjects(%v, %q) = %v, want %v", c.user, c.req, got, c.want)
			}
		})
	}
}

// candCodes 提取候选的 UniversityCode 列表（断言保序用）。
func candCodes(cs []Candidate) []string {
	out := make([]string, len(cs))
	for i, c := range cs {
		out[i] = c.UniversityCode
	}
	return out
}

// TestCheckConstraints_MatchAny 验证组级 match-any + 空列表放过 + 无法解析剔除 + 保序。
func TestCheckConstraints_MatchAny(t *testing.T) {
	cands := []Candidate{
		{UniversityCode: "A", MajorGroup: "(1)", SubjectReq: []string{"首选物理，再选化学"}},                      // 命中
		{UniversityCode: "B", MajorGroup: "(1)", SubjectReq: []string{"首选物理，再选地理", "首选物理，再选化学/生物(2选1)"}}, // match-any：命中第二条
		{UniversityCode: "C", MajorGroup: "(1)", SubjectReq: []string{"首选物理，再选思想政治"}},                    // 不命中
		{UniversityCode: "D", MajorGroup: "(1)"}, // 空 SubjectReq → 不限放过
		{UniversityCode: "E", MajorGroup: "(1)", SubjectReq: []string{"首选物理，再选化学/生物"}}, // 无法解析 → 剔除
	}
	kept, rejected, err := CheckConstraints(UserQuery{Subjects: []string{"物理", "化学", "地理"}, Category: "物理类"}, cands)
	if err != nil {
		t.Fatalf("unexpected err: %v", err)
	}
	// kept 保序：A, B, D
	if got := candCodes(kept); !reflect.DeepEqual(got, []string{"A", "B", "D"}) {
		t.Errorf("kept = %v, want [A B D]", got)
	}
	// rejected 保序：C(不匹配), E(无法解析)
	if len(rejected) != 2 {
		t.Fatalf("rejected len = %d, want 2", len(rejected))
	}
	if rejected[0].UniversityCode != "C" || rejected[0].Rule != RuleSubjectReq {
		t.Errorf("rejected[0] = %s/%s, want C/%s", rejected[0].UniversityCode, rejected[0].Rule, RuleSubjectReq)
	}
	if !strings.Contains(rejected[0].Reason, "选科不匹配") {
		t.Errorf("rejected[0].Reason = %q, want 含'选科不匹配'", rejected[0].Reason)
	}
	if rejected[1].UniversityCode != "E" || !strings.Contains(rejected[1].Reason, "无法解析") {
		t.Errorf("rejected[1] = %s/%q, want E/含'无法解析'", rejected[1].UniversityCode, rejected[1].Reason)
	}
	// 纯函数处于概率前，rejected 的 ProbEstimate 应为零值
	if rejected[0].Probability != 0 || rejected[0].Category != "" {
		t.Errorf("rejected ProbEstimate 应零值, got %+v", rejected[0].ProbEstimate)
	}
}

// TestCheckConstraints_Empty 空候选 → 空 kept/rejected、无 error。
func TestCheckConstraints_Empty(t *testing.T) {
	kept, rejected, err := CheckConstraints(UserQuery{Subjects: []string{"物理", "化学", "生物"}}, nil)
	if err != nil {
		t.Fatalf("unexpected err: %v", err)
	}
	if len(kept) != 0 || len(rejected) != 0 {
		t.Errorf("空候选应空结果, got kept=%d rejected=%d", len(kept), len(rejected))
	}
}

// TestCheckConstraints_AllPass 全部不限 → 全 kept、无 rejected。
func TestCheckConstraints_AllPass(t *testing.T) {
	cands := []Candidate{
		{UniversityCode: "A", SubjectReq: []string{"首选物理，再选不限"}},
		{UniversityCode: "B", SubjectReq: []string{"首选不限，再选不限"}},
	}
	kept, rejected, err := CheckConstraints(UserQuery{Subjects: []string{"物理", "化学", "生物"}}, cands)
	if err != nil {
		t.Fatalf("unexpected err: %v", err)
	}
	if len(kept) != 2 || len(rejected) != 0 {
		t.Errorf("应全过, got kept=%d rejected=%d", len(kept), len(rejected))
	}
}
