// Package biz 是 svc-algo 的业务层：领域算法（纯函数、无 IO）+ Repo 接口 + 领域类型。
//
// 设计见 docs/task2-implementation-plan.md §2/§3。本包不碰 SQL、不 import pb、不 import Kratos errors；
// 历史数据 / 一分一段表由 data 层查好后注入，便于单测与复现。
package biz

// ======== §2.1 核心输入 ========

// UserQuery 用户查询（全局唯一定义，rank/prob 共用）。
type UserQuery struct {
	ReleaseID string   // 不可变数据发布清单；所有公开数据查询必须按此过滤
	Province  string   // 省份：湖南
	Year      int      // 报考年份（请求传入，如演示 2025）；窗口/计划/候选池均由此推导，见 §3.0
	Category  string   // 科类：物理类/历史类（对应 DB category 字段，非 track）
	Batch     string   // 批次：本科批
	Score     float64  // 高考分数
	Subjects  []string // 选科组合，如 ["物理", "化学", "生物"]
	// 以下由 rank 模块计算后回填
	Rank       int     // 位次
	Percentile float64 // 百分位
}

// RankQuery 位次转换入参（RankConvert RPC / RankUsecase.Convert 专用；H2/Q32）。
// 只带换算必需的四项，比复用带 Rank/Percentile 回填字段的 UserQuery 更干净。
type RankQuery struct {
	ReleaseID string  // 不可变数据发布清单；决定读取哪一组一分一段数据
	Province  string  // 省份：湖南
	Category  string  // 科类：物理类/历史类
	Year      int     // 报考年（如 2025）：决定载入哪年 score_rank_map
	Score     float64 // 高考分数
}

// UserProfile 用户画像（用于效用打分）。
type UserProfile struct {
	RiskAppetite    string   // 风险偏好：conservative/balanced/aggressive
	PriorityDims    []string // 优先维度：["tier", "major", "location", "strength"]
	Interests       []string // 兴趣专业方向（utility 专业匹配用，由 LLM 从对话提取）
	PreferredCities []string // 偏好城市
	PreferredTiers  []string // 偏好学校层次：["985", "211", "双一流"]
	// 注：CareerGoal 已删除（死参数，YAGNI）。
}

// ======== §2.2 核心输出 ========

// RankResult 位次转换结果（G1：四字段统一）。
type RankResult struct {
	Rank         int     // 位次（全省第几名）
	Percentile   float64 // 百分位（超过了百分之几的人）
	TotalCount   int     // 全省总人数（= 完整一分一段表 MAX(cumulative_count)，百分位分母）
	ControlScore int     // 报考年本科线（score_rank_map.control_score，ConvertByScore 内部已用于下界校验）
	// 注：proto RankConvertReply 只回传 {rank, percentile}（薄契约，TotalCount/ControlScore 为内部派生量不外泄）。
}

// Candidate 候选（院校专业组）。
type Candidate struct {
	UniversityID   int
	UniversityCode string
	UniversityName string
	MajorGroup     string        // 专业组代号，如"（109）"
	GroupMinRank   int           // 组投档线位次（组内 MIN(min_rank)）
	Majors         []MajorDetail // 组内专业详情（展示用）
	SchoolTier     string        // 985/211/双一流/普通（由 deriveSchoolTier 派生，见 §3.2 Step1）
	Location       string        // 城市
	Province       string        // 省份（utility 地域"同省"判断用）
	SubjectReq     []string      // 选科要求
	// 学校实力/升学资源（来自 university 表，utility 打分用）
	NumMaster      int  // 硕士点数量
	NumDoctor      int  // 博士点数量
	RuankeRank     int  // 软科排名
	PlanCount      int  // 报考年该院校专业组计划数
	PlanCountKnown bool // false 表示计划数 UNKNOWN，不得按 0 处理
	// 历史录取数据（由 service 层填充）
	HistoryData []AdmissionRecord
}

// AdmissionRecord 某年录取记录（专业组维度）。
type AdmissionRecord struct {
	Year          int     // 所属年份（落在窗口 [year-W, year-1] 内，不含报考年）
	GroupMinRank  int     // 组投档线位次（组内最低位次）
	TotalCount    int     // 该省该年总人数（用于算百分位）
	MinPercentile float64 // 组投档线百分位 = GroupMinRank / TotalCount
	EnrollCount   int     // 招生人数
}

// MajorDetail 组内专业详情（major_admission JOIN major）。
type MajorDetail struct {
	MajorCode     string  // 专业代码
	MajorName     string  // 专业名称
	SubjectReq    string  // 选科要求，如"首选物理，再选不限"
	Duration      string  // 学制，如"四年"（来自 major 表）
	Tuition       float64 // 学费（元/年，来自 major 表）
	MajorNote     string  // 备注，如"（卓越班）"、中外合作等
	MinScore      int     // 该专业历史最低分（专业级参考）
	MinRank       int     // 该专业历史最低位次（仅展示，不用于概率计算）
	AdmitCount    int     // 录取人数
	InCurrentPlan bool    // 该专业代码是否存在于报考年招生计划
}

// Category 分档标签（ProbEstimate.Category 取值；单一真相源，prob/optimize 共用，§3.2/§3.5）。
const (
	CategoryRush   = "冲" // 概率 ∈ [RushThreshold, StableThreshold)
	CategoryStable = "稳" // 概率 ∈ [StableThreshold, SafeThreshold)
	CategorySafe   = "保" // 概率 ≥ SafeThreshold
)

// ProbEstimate 概率估计结果。
type ProbEstimate struct {
	Probability float64 // 录取概率，范围 [0.01, 0.99]
	Method      string  // 计算方法：weighted_hit（加权命中率法）
	DataYears   int     // 有效数据年数
	Category    string  // 冲/稳/保（取值见 CategoryRush/CategoryStable/CategorySafe）
}

// CandidateWithProb 带概率的候选 = 候选 + 概率（prob 模块输出）。
type CandidateWithProb struct {
	Candidate
	ProbEstimate
}

// UtilityScore 效用打分。
type UtilityScore struct {
	Score     float64            // 总分 0~1
	Breakdown map[string]float64 // 各维度得分
	Weights   map[string]float64 // 各维度权重
}

// WishlistResult 志愿表优化结果。
type WishlistResult struct {
	Candidates []CandidateWithScore // 排序后的志愿表
	Report     OptimizationReport   // 优化报告
}

// CandidateWithScore 带分数的候选 = 候选+概率+效用（optimize 模块输入）。
type CandidateWithScore struct {
	CandidateWithProb
	UtilityScore
	FinalRank int // 在志愿表中的位置
}

// OptimizationReport 优化报告。
type OptimizationReport struct {
	RushCount            int     // 冲的数量
	StableCount          int     // 稳的数量
	SafeCount            int     // 稳拿保底数（p≥SafeGuardThreshold=0.9，非分档"保"数）
	SafeGuardOK          bool    // 保底是否达标（SafeCount ≥ params.MinSafeCount）
	SlipRate             float64 // 解析式估算滑档率 = Π(1−pᵢ)（有保底时≈0）
	ExpectedSatisfaction float64 // 期望满意度 = 志愿表效用分均值
}

// AuditBasis 全局审计依据（对应 pb.AuditBasis，EstimateProbReply.basis 返回）。
type AuditBasis struct {
	UserRank       int     // 用户位次
	UserPercentile float64 // 用户百分位
	SampleYears    []int   // 概率窗口样本年份 = [报考年-W, 报考年-1]（不含报考年）
	Method         string  // weighted_hit
	Province       string
	Category       string
}

// RejectedCandidate 被硬规则剔除的候选 + 原因（对应 pb.RejectedCandidate）。
type RejectedCandidate struct {
	CandidateWithProb        // 复用统一候选形状
	Rule              string // 触发规则 key：subject_req/single_score/physical/gender/batch
	Reason            string // 人话原因（供前端/LLM 解释）
}

// ======== §3.2 prob 相关 ========

// RecommendResult 分档候选池（EstimateProb / RecommendCandidates 的输出）。
type RecommendResult struct {
	RushCandidates   []CandidateWithProb // 冲档候选（默认全量；PerCategoryLimit>0 时才截断）
	StableCandidates []CandidateWithProb // 稳档候选
	SafeCandidates   []CandidateWithProb // 保档候选
	TotalCandidates  int                 // 候选总数
	FilteredCount    int                 // 筛选后数量
	// 分档总数（截断前，用于前端显示「还有 N 个」/分页）
	RushTotal   int // 冲档候选总数
	StableTotal int // 稳档候选总数
	SafeTotal   int // 保档候选总数
	// Basis 全局审计依据（EstimateProbReply.basis 来源）。纯函数 RecommendCandidates 不知道用户 rank/percentile，
	// 故本字段由 RecommendUsecase.EstimateProb 编排末尾回填（§4.2②：RecommendResult = 三档 + AuditBasis）。
	Basis AuditBasis
}

// RecommendConfig 概率估计 + 分档配置（默认值由 conf.Bootstrap.Algo 构造，见 NewRecommendUsecase）。
type RecommendConfig struct {
	// 筛选条件
	WindowYears  int      // 历史窗口年数（默认 3）；概率窗口=[报考年-W, 报考年-1]，不含报考年（§3.0）
	PreferCities []string // 偏好城市（可选）
	// 注（G9/YAGNI）：原 PreferTiers/ExcludeTiers 已删除（死参）；层次偏好统一走 UserProfile.PreferredTiers（utility 调权）。

	// 分档参数
	DecayFactor     float64 // 时效衰减（默认 0.7）
	PlanAdjust      float64 // 计划修正因子（默认 0.5）
	RushThreshold   float64 // 冲阈值（默认 0.2）
	StableThreshold float64 // 稳阈值（默认 0.5）
	SafeThreshold   float64 // 保阈值（默认 0.8）

	// 候选池输出控制（用于分档浏览，见 §3.6）
	PerCategoryLimit int // 每档最多返回数量（默认 0=全量；>0 时截断，一般不用）
}

// GroupKey 院校专业组唯一标识（批量查询/回填的 key）。
type GroupKey struct {
	UniversityCode string
	MajorGroup     string
}

// PlanChange 招生计划变化（报考年 vs 报考年-1）。
type PlanChange struct {
	PlanCount     int // 报考年(year)计划数，如 2025
	PrevPlanCount int // 报考年-1(year-1)计划数，如 2024
}

// ======== §3.4 utility 相关 ========

// UtilityConfig 效用打分配置（默认权重由 conf.Bootstrap.Algo.utility_weights 构造，OptimizeUsecase 持有）。
type UtilityConfig struct {
	// 四维默认权重，key：tier/major/location/strength，默认各 0.25。
	// 不强制 Σ=1：基准值经 PriorityDims/PreferredTiers ×倍率后统一归一化（见 §3.4）。
	DefaultWeights map[string]float64
}

// NormStats 归一化统计（当批全体候选的 min/max，CalculateUtilityBatch 内部第一步算出）。
type NormStats struct {
	MasterMin, MasterMax int // 硕士点数 min/max
	DoctorMin, DoctorMax int // 博士点数 min/max
	RuankeMax            int // 软科排名最大值（排名越小越好，反向用）
}

// ======== §3.5 optimize 相关 ========

// OptimizeParams 志愿表优化参数（默认值由 conf.Bootstrap.Algo 构造，见 NewOptimizeUsecase）。
type OptimizeParams struct {
	MaxWishlistSize    int         // 最多填几个志愿（L）
	MinSafeCount       int         // 最少「稳拿保底」数（p≥SafeGuardThreshold 的硬下限，显式可审计；默认 conf.Algo.min_safe_count=9）
	SafeGuardThreshold float64     // 保底滑档率上限（默认 0.9）
	TargetRatio        RatioConfig // 目标冲稳保比例（配比单一真相源）
	// 注（Q20/DR1）：RiskAppetite 字段已删除——它是用户偏好、已在 UserProfile；OptimizeWishlist 只认 TargetRatio。
}

// RatioConfig 冲稳保比例配置。
type RatioConfig struct {
	Rush   float64 // 默认 0.3
	Stable float64 // 默认 0.4
	Safe   float64 // 默认 0.3
}
