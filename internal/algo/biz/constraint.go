package biz

import (
	"fmt"
	"strings"
)

// constraint.go —— 硬规则裁决（§3.3）。纯函数，不碰 SQL/不 import pb。
// MVP 只实现「选科匹配」硬约束；单科成绩/体检/性别/批次为 M1 TODO（见文末）。

// 触发规则 key 常量（RejectedCandidate.Rule 取值，与 proto RejectedCandidate.rule 枚举一致）。
const (
	RuleSubjectReq  = "subject_req"  // 选科不匹配 / 选科要求无法解析
	RuleSingleScore = "single_score" // 单科成绩要求（M1 TODO）
	RulePhysical    = "physical"     // 体检限报（M1 TODO）
	RuleGender      = "gender"       // 性别限制（M1 TODO）
	RuleBatch       = "batch"        // 批次不符（M1 TODO，service 层预过滤）
)

// 再选模式（SubjectReq.ReselectMode 取值）。
const (
	reselectNone       = "none"       // 再选不限
	reselectAtLeastOne = "atLeastOne" // X/Y(2选1)：命中任一即可
	reselectAll        = "all"        // 单科 或 X、Y(2科必选)：须全部包含
)

// 首选科目枚举。
const (
	firstPhysics  = "物理"
	firstHistory  = "历史"
	firstAny      = "不限"
	reselectText  = "不限"
	markerChoose1 = "(2选1)"
	markerAll2    = "(2科必选)"
)

// SubjectReq 解析后的选科要求（湖南 3+1+2：首选 + 再选）。
type SubjectReq struct {
	FirstChoice  string   // 首选：物理/历史/不限
	ReselectMode string   // 再选模式：none(不限)/atLeastOne(2选1)/all(单科或2科必选)
	Reselect     []string // 再选科目（归一化后）；none 时为空
}

// normalizeSubject 科目名归一化（政治→思想政治，与库中口径对齐；库中再选科目已用全称"思想政治"）。
func normalizeSubject(s string) string {
	switch strings.TrimSpace(s) {
	case "政治":
		return "思想政治"
	default:
		return strings.TrimSpace(s)
	}
}

// splitSubjects 按分隔符拆分科目串并逐个归一化，丢弃空项（保序）。
func splitSubjects(s, sep string) []string {
	raw := strings.Split(s, sep)
	out := make([]string, 0, len(raw))
	for _, r := range raw {
		if n := normalizeSubject(r); n != "" {
			out = append(out, n)
		}
	}
	return out
}

// parseSubjectReq 解析文本选科要求（纯函数）。
// 数据源 major_admission.subject_req，实测全库仅 27 种（湖南本科批），格式统一 "首选X，再选Y"：
//   - 分隔符：全角逗号 "，"；首选前缀 "首选"、再选前缀 "再选"
//   - 再选四类：`不限`(none) / 单科如`化学`(all,1门) / `X/Y(2选1)`(atLeastOne,按"/"拆) / `X、Y(2科必选)`(all,按全角顿号"、"拆)
//   - 括号为半角 `( )`；防御性把全角 `（ ）` 归一为半角
//
// 空串/NULL → 视为不限（FirstChoice=不限, ReselectMode=none），不因脏数据误杀。
// 无法解析的未知格式 → 返回 error，调用方据此剔除该候选（硬约束保守）。
func parseSubjectReq(text string) (SubjectReq, error) {
	text = strings.TrimSpace(text)
	if text == "" {
		return SubjectReq{FirstChoice: firstAny, ReselectMode: reselectNone}, nil
	}

	// 全角括号防御性归一（实测数据为半角，此步仅兜底）
	text = strings.ReplaceAll(text, "（", "(")
	text = strings.ReplaceAll(text, "）", ")")

	parts := strings.Split(text, "，")
	if len(parts) != 2 {
		return SubjectReq{}, fmt.Errorf("缺全角逗号分隔首选/再选: %q", text)
	}

	if !strings.HasPrefix(strings.TrimSpace(parts[0]), "首选") || !strings.HasPrefix(strings.TrimSpace(parts[1]), "再选") {
		return SubjectReq{}, fmt.Errorf("缺少首选/再选前缀: %q", text)
	}
	first := strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(parts[0]), "首选"))
	first = normalizeSubject(first)
	if first != firstPhysics && first != firstHistory && first != firstAny {
		return SubjectReq{}, fmt.Errorf("未知首选科目 %q（原文 %q）", first, text)
	}

	reselect := strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(parts[1]), "再选"))
	req := SubjectReq{FirstChoice: first}
	switch {
	case reselect == reselectText:
		req.ReselectMode = reselectNone
	case strings.HasSuffix(reselect, markerChoose1):
		req.ReselectMode = reselectAtLeastOne
		req.Reselect = splitSubjects(strings.TrimSuffix(reselect, markerChoose1), "/")
	case strings.HasSuffix(reselect, markerAll2):
		req.ReselectMode = reselectAll
		req.Reselect = splitSubjects(strings.TrimSuffix(reselect, markerAll2), "、")
	default:
		// 无已知模式标记：单科放过；含分隔符或残留括号 → 未知格式报错（保守剔除）
		if strings.Contains(reselect, "(") || strings.ContainsAny(reselect, "/、") {
			return SubjectReq{}, fmt.Errorf("再选格式无法解析 %q（原文 %q）", reselect, text)
		}
		req.ReselectMode = reselectAll
		req.Reselect = []string{normalizeSubject(reselect)}
	}

	// 模式与科目数一致性兜底：非 none 模式必须解析出至少 1 门再选科目
	if req.ReselectMode != reselectNone && len(req.Reselect) == 0 {
		return SubjectReq{}, fmt.Errorf("再选科目为空但模式为 %q（原文 %q）", req.ReselectMode, text)
	}
	seen := make(map[string]bool, len(req.Reselect))
	for _, s := range req.Reselect {
		if !isReselect(s) || seen[s] {
			return SubjectReq{}, fmt.Errorf("非法或重复再选科目: %q", text)
		}
		seen[s] = true
	}
	if strings.HasSuffix(reselect, markerChoose1) || strings.HasSuffix(reselect, markerAll2) {
		if len(req.Reselect) != 2 {
			return SubjectReq{}, fmt.Errorf("再选科目数不符: %q", text)
		}
	}
	return req, nil
}

// matchSubjects 用户选科是否满足一条选科要求（纯函数）。
// 首选：req=不限→过，否则须 == 用户首选（候选 SQL 已按 category 过滤，此处冗余保险）。
// 再选：none→过、atLeastOne→用户再选 ∩ req ≠ ∅、all→req ⊆ 用户再选。
// 用户再选 = userSubjects 去掉首选（物理/历史）。
func matchSubjects(userSubjects []string, req SubjectReq) bool {
	return newSubjectMatcher(userSubjects).matches(req)
}

type parsedSubject struct {
	req SubjectReq
	err error
}

// subjectMatcher 仅活在单次调用中，复用科目集合和文本解析，避免跨请求污染。
type subjectMatcher struct {
	first    string
	reselect map[string]bool
	parsed   map[string]parsedSubject
}

func newSubjectMatcher(userSubjects []string) *subjectMatcher {
	m := &subjectMatcher{reselect: make(map[string]bool, len(userSubjects)), parsed: make(map[string]parsedSubject)}
	for _, s := range userSubjects {
		n := normalizeSubject(s)
		if n == firstPhysics || n == firstHistory {
			m.first = n // 首选（若同时含物理+历史属非法输入，取后者，交由上游校验）
			continue
		}
		if n != "" {
			m.reselect[n] = true
		}
	}
	return m
}

func (m *subjectMatcher) matches(req SubjectReq) bool {
	// 首选匹配
	if req.FirstChoice != firstAny && req.FirstChoice != m.first {
		return false
	}
	// 再选匹配
	switch req.ReselectMode {
	case reselectNone:
		return true
	case reselectAtLeastOne:
		for _, r := range req.Reselect {
			if m.reselect[r] {
				return true
			}
		}
		return false
	case reselectAll:
		for _, r := range req.Reselect {
			if !m.reselect[r] {
				return false
			}
		}
		return true
	default:
		return false // 未知模式保守拒绝
	}
}

// matchCandidateSubjects 组级 match-any：Candidate.SubjectReq 是组内 distinct 列表
// （实测 99.76% 长度=1，仅 32 组为 2），满足任一条即通过。
//   - 空列表（组内无 subject_req）→ 当"不限"放过（matched=true）
//   - 任一条 parseSubjectReq 失败 → 返回 parseErr（调用方保守剔除该候选）
//   - 全部不匹配 → matched=false + 人话 reason
func matchCandidateSubjects(userSubjects []string, reqs []string) (matched bool, reason string, parseErr error) {
	return newSubjectMatcher(userSubjects).matchCandidate(userSubjects, reqs)
}

func (m *subjectMatcher) matchCandidate(userSubjects []string, reqs []string) (matched bool, reason string, parseErr error) {
	if len(reqs) == 0 {
		return true, "", nil
	}
	for _, text := range reqs {
		parsed, ok := m.parsed[text]
		if !ok {
			parsed.req, parsed.err = parseSubjectReq(text)
			m.parsed[text] = parsed
		}
		if parsed.err != nil {
			return false, "", parsed.err
		}
		if m.matches(parsed.req) {
			matched = true
		}
	}
	if matched {
		return true, "", nil
	}
	return false, fmt.Sprintf("选科不匹配：用户选科 [%s] 不满足该专业组任一选科要求 [%s]",
		strings.Join(userSubjects, ","), strings.Join(reqs, " | ")), nil
}

// CheckConstraints 检查硬约束（纯函数，作用于 []Candidate——约束过滤先于概率，见 §3.3 编排）。
// 返回：通过的候选 + 被拒的候选（带规则 key 与原因）。
//
// 注（G5/G6）：全链统一用 RejectedCandidate（§2.2，嵌入 CandidateWithProb）。本纯函数处于概率计算之前，
// 故其 rejected 项的 CandidateWithProb.ProbEstimate 为零值（仅填 Candidate+Rule+Reason）；
// 独立 CheckConstraints RPC 由 usecase 层按 GroupKey 从入参池回填 prob（见 §4.2③）。
//
// 可复现纪律（DH1/DH3）：单线程、按 candidates 输入序 append，不迭代 map 产序。
// MVP 只裁决选科；err 恒为 nil（保留返回位以对齐文档签名，未来系统性错误用）。
func CheckConstraints(query UserQuery, candidates []Candidate) (kept []Candidate, rejected []RejectedCandidate, err error) {
	kept = make([]Candidate, 0, len(candidates))
	rejected = make([]RejectedCandidate, 0)
	matcher := newSubjectMatcher(query.Subjects)
	for _, cand := range candidates {
		matched, reason, parseErr := matcher.matchCandidate(query.Subjects, cand.SubjectReq)
		switch {
		case parseErr != nil:
			// 无法解析的未知格式 → 保守剔除并记原因（§3.3 边界决策）
			rejected = append(rejected, RejectedCandidate{
				CandidateWithProb: CandidateWithProb{Candidate: cand},
				Rule:              RuleSubjectReq,
				Reason:            fmt.Sprintf("选科要求无法解析：%v", parseErr),
			})
		case matched:
			kept = append(kept, cand)
		default:
			rejected = append(rejected, RejectedCandidate{
				CandidateWithProb: CandidateWithProb{Candidate: cand},
				Rule:              RuleSubjectReq,
				Reason:            reason,
			})
		}
	}
	return kept, rejected, nil
}

// M1 阶段暂不实现（§3.3 检查项 2/3/4，标记 TODO）：
//   - 批次/分数线：用户分数 ≥ 批次线，由 service 层预先过滤（rank.Convert 已校验本科线下界）
//   - 体检限报：需 RAG 检索章程体检要求（RulePhysical）
//   - 单科要求：如英语 ≥ 120（RuleSingleScore）
