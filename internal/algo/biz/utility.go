package biz

import "strings"

// utility.go —— 效用打分（§3.4）。纯函数，不碰 SQL/不 import pb。
// 职责：算用户对每个候选的"心动分"，拉开同档内区分度（概率二元粗粒度的档内排序全靠它兜底，§6.1）。

// 打分维度 key（全局统一，PriorityDims/Breakdown/Weights/公式四处同口径；与 config.go DefaultWeights 键一致）。
const (
	DimTier     = "tier"
	DimMajor    = "major"
	DimLocation = "location"
	DimStrength = "strength"
)

// utilityDims 固定维度序（DH3：所有对维度的遍历走此 slice，不 range map 产序，保证可复现）。
var utilityDims = []string{DimTier, DimMajor, DimLocation, DimStrength}

// userProvinceMVP "同省"判定的用户省份。系统仅覆盖湖南考生（项目边界），故候选院校 province==湖南 即"同省"。
const userProvinceMVP = "湖南"

// defaultDimWeight 单维兜底权重（yaml 缺该维时用）。
const defaultDimWeight = 0.25

// priorityBoostFactor PriorityDims/PreferredTiers 命中维度的权重倍率（封顶 ×2，不叠加）。
const priorityBoostFactor = 2.0

// CalculateUtilityBatch 批量计算效用分（纯函数）。
// 流程：① 扫一遍 candidates 算归一化统计 NormStats → ② 逐个算四维得分 → ③ 按权重合成。
// 批量入口的原因：升学资源(strength)维需当批全体候选的 min-max 统计，单候选无法独立打分。
// 可复现（DH1/DH3）：按 candidates 输入序 append，维度遍历走固定 utilityDims slice。
func CalculateUtilityBatch(profile UserProfile, candidates []Candidate, config UtilityConfig) ([]UtilityScore, error) {
	stats := computeNormStats(candidates)
	weights := deriveWeights(profile, config)

	scores := make([]UtilityScore, 0, len(candidates))
	for _, c := range candidates {
		breakdown := map[string]float64{
			DimTier:     tierScore(c.SchoolTier),
			DimMajor:    majorScore(profile.Interests, c.Majors),
			DimLocation: locationScore(profile.PreferredCities, c.Location, c.Province),
			DimStrength: strengthScore(c, stats),
		}
		total := 0.0
		for _, dim := range utilityDims { // Σweights=1 → total ∈ [0,1]
			total += breakdown[dim] * weights[dim]
		}
		scores = append(scores, UtilityScore{
			Score:     total,
			Breakdown: breakdown,
			Weights:   weights, // 同批权重一致（只读共享，不逐份拷贝）
		})
	}
	return scores, nil
}

// computeNormStats 扫一遍候选算 min-max 归一化统计（strength 维用）。
// RuankeMax 取全体最大软科排名（排名越小越好，反向用）；缺失值(≤0)在 strengthScore 内单独按 0 处理。
func computeNormStats(candidates []Candidate) NormStats {
	if len(candidates) == 0 {
		return NormStats{}
	}
	s := NormStats{
		MasterMin: candidates[0].NumMaster, MasterMax: candidates[0].NumMaster,
		DoctorMin: candidates[0].NumDoctor, DoctorMax: candidates[0].NumDoctor,
		RuankeMax: candidates[0].RuankeRank,
	}
	for _, c := range candidates[1:] {
		if c.NumMaster < s.MasterMin {
			s.MasterMin = c.NumMaster
		}
		if c.NumMaster > s.MasterMax {
			s.MasterMax = c.NumMaster
		}
		if c.NumDoctor < s.DoctorMin {
			s.DoctorMin = c.NumDoctor
		}
		if c.NumDoctor > s.DoctorMax {
			s.DoctorMax = c.NumDoctor
		}
		if c.RuankeRank > s.RuankeMax {
			s.RuankeMax = c.RuankeRank
		}
	}
	return s
}

// minmax 归一化 (x−min)/(max−min)；max==min 时取 0.5（全体同值，无区分度）。
func minmax(x, min, max int) float64 {
	if max == min {
		return 0.5
	}
	return float64(x-min) / float64(max-min)
}

// tierScore 学校层次得分（固定映射，与 SchoolTier 派生优先级一致）。
func tierScore(tier string) float64 {
	switch tier {
	case "985":
		return 1.0
	case "211":
		return 0.8
	case "双一流":
		return 0.6
	default: // 普通
		return 0.4
	}
}

// majorScore 专业匹配（关键词包含）：任一 interest 作为子串命中任一组内 MajorName → 1.0，否则 0.0。
// 无 interests → 0.0（该维对本批全体一致，不影响相对排序）。后续优化：LLM 语义匹配。
func majorScore(interests []string, majors []MajorDetail) float64 {
	for _, m := range majors {
		for _, kw := range interests {
			if kw != "" && strings.Contains(m.MajorName, kw) {
				return 1.0
			}
		}
	}
	return 0.0
}

// locationScore 地域匹配三档：偏好城市命中=1.0 / 同省(湖南)=0.8 / 其他=0.5。
// 城市命中用双向子串（"长沙" vs "长沙市" 皆命中）；"相邻省"档已砍（库中无省份邻接数据，YAGNI）。
func locationScore(preferredCities []string, location, province string) float64 {
	for _, city := range preferredCities {
		if city == "" {
			continue
		}
		if city == location || strings.Contains(location, city) || strings.Contains(city, location) {
			return 1.0
		}
	}
	if province == userProvinceMVP {
		return 0.8
	}
	return 0.5
}

// strengthScore 升学资源：0.4×minmax(NumMaster) + 0.3×minmax(NumDoctor) + 0.3×(1 − RuankeRank/RuankeMax)。
// 缺失值（RuankeRank≤0 或 RuankeMax≤0）→ 软科项按 0 计（不误给缺失数据最高分）。
func strengthScore(c Candidate, s NormStats) float64 {
	master := minmax(c.NumMaster, s.MasterMin, s.MasterMax)
	doctor := minmax(c.NumDoctor, s.DoctorMin, s.DoctorMax)
	ruanke := 0.0
	if c.RuankeRank > 0 && s.RuankeMax > 0 {
		ruanke = 1 - float64(c.RuankeRank)/float64(s.RuankeMax)
		if ruanke < 0 {
			ruanke = 0 // 防御：rank > max 不应发生
		}
	}
	return 0.4*master + 0.3*doctor + 0.3*ruanke
}

// deriveWeights 权重提取：基准 DefaultWeights（缺维兜底 0.25）→ PriorityDims 命中维 ×2 →
// PreferredTiers 非空则 tier ×2（与 PriorityDims 含 tier 同一信号，封顶 ×2 不叠加）→ 归一化 Σ=1。
// 例：PriorityDims=["major"] + PreferredTiers=["985"] → tier×2、major×2 → 归一化 0.33/0.33/0.17/0.17。
func deriveWeights(profile UserProfile, config UtilityConfig) map[string]float64 {
	w := make(map[string]float64, len(utilityDims))
	for _, dim := range utilityDims {
		w[dim] = weightOr(config.DefaultWeights, dim)
	}

	boost := make(map[string]bool, len(utilityDims))
	for _, d := range profile.PriorityDims {
		if _, ok := w[d]; ok { // 只认四维内的合法 key，忽略未知维度
			boost[d] = true
		}
	}
	if len(profile.PreferredTiers) > 0 {
		boost[DimTier] = true // 强调学校层次 → tier ×2（封顶，不与 PriorityDims 叠加）
	}
	for _, dim := range utilityDims {
		if boost[dim] {
			w[dim] *= priorityBoostFactor
		}
	}

	var sum float64
	for _, dim := range utilityDims {
		sum += w[dim]
	}
	if sum > 0 {
		for _, dim := range utilityDims {
			w[dim] /= sum
		}
	}
	return w
}

// weightOr 取基准权重；map 为 nil / 缺 key / 值为 0 → 兜底 defaultDimWeight（与 config.go f64Or 语义一致）。
func weightOr(m map[string]float64, key string) float64 {
	if v, ok := m[key]; ok && v != 0 {
		return v
	}
	return defaultDimWeight
}
