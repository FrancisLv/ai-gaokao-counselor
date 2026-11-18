# Task 2: 算法层详细实现计划

> 基于 `docs/algorithms.md` 设计，实现 svc-algo 的核心算法层。
>
> ⚠️ **旧代码骨架已作废**：现有 `internal/algo/{rank,prob,constraint,optimize}` 与 `service.go` 是几个版本前的骨架（正态/KDE/置信区间等旧设计），**忽略、实现时以本文档为准重写**。
> 🏛️ **架构：kratos 三层 + wire**——`service`（proto 对接）→ `biz`（领域算法 + Repo 接口）→ `data`（实现 Repo、跑 SQL）。DB 操作只在 data 层，算法层纯函数无 IO。

---

## 一、整体架构

### 1.1 代码组织

```
internal/algo/
├── biz/                    # 【业务层】领域算法（纯函数、无 IO）+ Repo 接口 + 领域类型
│   ├── repo.go             #   AdmissionRepo / RankRepo 接口（data 层实现）
│   ├── types.go            #   领域对象：UserQuery / Candidate / RecommendResult ...
│   ├── rank.go             #   分数→位次→百分位（纯函数）+ RankUsecase（持 RankRepo → 承接 RankConvert RPC）
│   ├── prob.go             #   录取概率·加权命中率法（纯函数，历史数据由 data 注入）
│   ├── constraint.go       #   硬规则裁决（选科/体检/批次，纯函数）
│   ├── utility.go          #   效用打分（心动分，纯函数）
│   ├── optimize.go         #   志愿表优化（冲稳保配比+排序+保底检查，纯函数）+ OptimizeUsecase（持 conf → 承接 OptimizeWishlist RPC）
│   └── recommend.go        #   RecommendUsecase（持 AdmissionRepo + *RankUsecase + conf）：编排「取数→硬约束过滤→算概率→分档」→ 承接 EstimateProb + CheckConstraints RPC
├── data/                   # 【数据层】实现 biz.Repo，封装所有 SQL（唯一碰 DB 处，用 pgx/v5）
│   ├── data.go             #   Data{pg *pgxpool.Pool}
│   ├── admission.go        #   候选生成 / BatchGroupHistory / BatchGroupPlan
│   └── rank.go             #   一分一段表查询
└── service/                # 【服务层】proto 对接（薄），wire 注入 biz usecase
    ├── algo.go             #   Service：4 个 RPC → 调 biz usecase → 映射 proto
    └── dto.go              #   biz↔pb 映射（toPb*/fromPb*，每类型一对；H5/Q35 归 service 层，biz 不 import pb）

cmd/algo/
├── main.go                 # kratos 启动：加载 conf.Bootstrap（config.New+Scan）→ wireApp(&bc)
├── wire.go                 # //go:build wireinject —— 依赖注入声明（入参 *conf.Bootstrap）
└── wire_gen.go             # wire 自动生成（勿手改）

conf/                       # 【配置】Kratos conf.Bootstrap（Server/Data/Algo 三段）
├── conf.proto              #   Bootstrap 定义（含 Algo 算法参数段，A1 外部化）
└── conf.pb.go              #   protoc 生成（勿手改）
```

### 1.2 设计原则

- **kratos 三层分层**：`service`（proto 对接，薄）→ `biz`（领域算法 + Repo 接口）→ `data`（实现 Repo、跑 SQL）。依赖倒置：biz 定义接口、data 实现，biz 不碰 SQL。
- **纯函数优先**：biz 层算法不做 IO；历史数据、一分一段表由 data 层查好后注入，便于单测与复现。
- **wire 依赖注入**：`data.New{Admission,Rank}Repo → biz.New{Rank,Recommend,Optimize}Usecase → service.NewAlgo` 由 wire 串联（RankUsecase 持 RankRepo、RecommendUsecase 复用 *RankUsecase），禁止包内直接 new 下层。详见 §4.1③。
- **配置驱动**：算法常量（衰减/阈值/配比/窗口）来自 `configs/algo.yaml` → `conf.Bootstrap.Algo`，wire 注入 usecase 构造默认 config；用户偏好（风险/城市/兴趣）来自每请求 `profile`（UserProfile message），service 层合并覆盖。详见 §4.1。
- **类型化契约**：跨服务载荷一律用 `algo.proto` 的类型化 message（非 opaque `*_json` 字符串）。pb message 是 wire DTO，§2 的 biz 领域 struct 与之分离，由 service 层做 biz↔pb 映射（详见 §4.3）；改 proto 后跑 `make api` 重生成 Go+Python，勿手改 `*.pb.go`（已 gitignore）。
- **可复现（Q24）**：相同输入必须产生相同输出，三条纪律——①**排序全序**：所有排序用全序比较器（如 utility 降序 + GroupKey 升序 tie-break），禁 `sort.Slice` 仅比单键致并列乱序；②**禁 map 迭代产序**：任何有序输出不得直接 `range map`（Go map 序随机），需有序则收集 key 排序后遍历（`Breakdown/Weights` 等 map 仅按 key 取值/序列化，pb map 序不影响语义）；③**并行须保序**：若引入 goroutine 并行（M2+ 按需），必须用预分配 indexed slice（`results[i]` 按输入下标写）或事后稳定排序，保证输出序与单线程一致。MVP 概率计算单线程即够（瓶颈是 DB、已批量 SQL 解决）。
- **可审计**：每个结果都带依据（用了哪些数据、哪种方法）。
- **防御性编程**：数据不足时明确报错，不编造结果。

---

## 二、数据结构定义（biz/types.go）

> **§2 是 biz 层领域模型**（Go 嵌入扁平化、算法内部用，`cwp.Probability` 直接可取）；**wire 契约是 `algo.proto` 的类型化 message**（DTO，snake_case、嵌套组合）。两者字段一一对应但形状不同，由 **service 层做 biz↔pb 映射**（见 §4.3）。改契约请改 `.proto` 并跑 `make api`，勿手改生成的 `*.pb.go`（已 gitignore）。

### 2.1 核心输入

```go
// UserQuery 用户查询（全局唯一定义，rank/prob 共用）
type UserQuery struct {
    Province string   // 省份：湖南
    Year     int      // 报考年份（请求传入，如演示 2025）；窗口/计划/候选池均由此推导，见 §3.0
    Category string   // 科类：物理类/历史类（对应 DB `category` 字段，非 track）
    Batch    string   // 批次：本科批
    Score    float64  // 高考分数
    Subjects []string // 选科组合，如 ["物理", "化学", "生物"]
    // 以下由 rank 模块计算后回填
    Rank       int     // 位次
    Percentile float64 // 百分位
}

// RankQuery 位次转换入参（RankConvert RPC / RankUsecase.Convert 专用；H2/Q32 补齐定义）
// 只带换算必需的四项，比复用带 Rank/Percentile 回填字段的 UserQuery 更干净
type RankQuery struct {
    Province string // 省份：湖南
    Category string // 科类：物理类/历史类
    Year     int    // 报考年（如 2025）：决定载入哪年 score_rank_map
    Score    float64 // 高考分数
}

// UserProfile 用户画像（用于效用打分）
type UserProfile struct {
    RiskAppetite    string   // 风险偏好：conservative/balanced/aggressive
    PriorityDims    []string // 优先维度，可选值与 utility 维度 key 统一：["tier", "major", "location", "strength"]
    Interests       []string // 兴趣专业方向（utility 专业匹配用，由 LLM 从对话提取）
    PreferredCities []string // 偏好城市
    PreferredTiers  []string // 偏好学校层次：["985", "211", "双一流"]
    // 注：CareerGoal（职业目标）已删除——定义了但打分从未使用（死参数），MVP 按 YAGNI 移除；
    //     后续若需按职业目标调权重（postgraduate→strength↑ 等）再加回。
}
```

### 2.2 核心输出

```go
// RankResult 位次转换结果（G1：四字段统一——TotalCount 算百分位的分母、ControlScore 判“分数<本科线”均内部用）
type RankResult struct {
    Rank         int     // 位次（全省第几名）
    Percentile   float64 // 百分位（超过了百分之几的人）
    TotalCount   int     // 该省该年全省总人数（= 完整一分一段表最低分 cumulative，百分位分母）
    ControlScore int     // 报考年本科线（score_rank_map.control_score，ConvertByScore 内部已用于下界校验）
    // 注：proto RankConvertReply 只回传 {rank, percentile}（薄契约，TotalCount/ControlScore 为内部派生量不外泄）
}

// Candidate 候选（院校专业组）
type Candidate struct {
    UniversityID   int
    UniversityCode string
    UniversityName string
    MajorGroup     string        // 专业组代号，如"（109）"
    GroupMinRank   int           // 组投档线位次（组内 MIN(min_rank)）
    Majors         []MajorDetail // 组内专业详情（展示用）
    SchoolTier     string        // 985/211/双一流/普通（由 biz 纯函数 deriveSchoolTier(is_985,is_211,is_dual_class) 派生，优先级 985>211>双一流>普通，见 §3.2 Step1）
    Location       string        // 城市
    Province       string        // 省份（utility 地域"同省"判断用，SQL 从 university.province 取）
    SubjectReq     []string      // 选科要求
    // 学校实力/升学资源（来自 university 表真实字段，utility 打分用）
    NumMaster  int // 硕士点数量
    NumDoctor  int // 博士点数量
    RuankeRank int // 软科排名
    
    // 历史录取数据（由 service 层填充）
    HistoryData []AdmissionRecord
}

// AdmissionRecord 某年录取记录（专业组维度）
type AdmissionRecord struct {
    Year          int     // 所属年份（落在窗口 [year-W, year-1] 内，不含报考年）
    GroupMinRank  int     // 组投档线位次（组内最低位次）
    TotalCount    int     // 该省该年总人数（用于算百分位）
    MinPercentile float64 // 组投档线百分位 = GroupMinRank / TotalCount
    EnrollCount   int     // 招生人数
}

// MajorDetail 组内专业详情（major_admission JOIN major）
type MajorDetail struct {
    MajorCode  string  // 专业代码
    MajorName  string  // 专业名称
    SubjectReq string  // 选科要求，如"首选物理，再选不限"
    Duration   string  // 学制，如"四年"（来自 major 表）
    Tuition    float64 // 学费（元/年，来自 major 表）
    MajorNote  string  // 备注，如"（卓越班）"、中外合作等
    MinScore   int     // 该专业历史最低分（专业级参考）
    MinRank    int     // 该专业历史最低位次（专业级，仅展示，不用于概率计算）
    AdmitCount int     // 录取人数
}

// ProbEstimate 概率估计结果
type ProbEstimate struct {
    Probability float64 // 录取概率，范围 [0.01, 0.99]
    Method      string  // 计算方法：weighted_hit（加权命中率法）
    DataYears   int     // 有效数据年数
    Category    string  // 冲/稳/保
}

// CandidateWithProb 带概率的候选 = 候选 + 概率（prob 模块输出）
type CandidateWithProb struct {
    Candidate
    ProbEstimate
}

// UtilityScore 效用打分
type UtilityScore struct {
    Score       float64            // 总分 0~1
    Breakdown   map[string]float64 // 各维度得分
    Weights     map[string]float64 // 各维度权重
}

// WishlistResult 志愿表优化结果
type WishlistResult struct {
    Candidates []CandidateWithScore // 排序后的志愿表
    Report     OptimizationReport   // 优化报告
}

// CandidateWithScore 带分数的候选 = 候选+概率+效用（optimize 模块输入）
// 由 CandidateWithProb 叠加 UtilityScore 组装而成（见 4.2 service 编排）
type CandidateWithScore struct {
    CandidateWithProb
    UtilityScore
    FinalRank int // 在志愿表中的位置
}

// OptimizationReport 优化报告
type OptimizationReport struct {
    RushCount    int     // 冲的数量
    StableCount  int     // 稳的数量
    SafeCount    int     // 稳拿保底数（p≥SafeGuardThreshold=0.9，非分档"保"数）
    SafeGuardOK  bool    // 保底是否达标（SafeCount ≥ params.MinSafeCount）
    SlipRate     float64 // 解析式估算滑档率 = Π(1−pᵢ)（有保底时≈0）
    ExpectedSatisfaction float64 // 期望满意度 = 志愿表效用分均值
}

// AuditBasis 全局审计依据（对应 pb.AuditBasis，EstimateProbReply.basis 返回）
type AuditBasis struct {
    UserRank       int     // 用户位次
    UserPercentile float64 // 用户百分位
    SampleYears    []int   // 概率窗口样本年份 = [报考年-W, 报考年-1]，如报考年2025/W3 → [2022,2023,2024]（不含报考年）
    Method         string  // weighted_hit
    Province       string
    Category       string
}

// RejectedCandidate 被硬规则剔除的候选 + 原因（对应 pb.RejectedCandidate，CheckConstraintsReply.rejected 返回）
type RejectedCandidate struct {
    CandidateWithProb        // 复用统一候选形状
    Rule   string            // 触发规则 key：subject_req/single_score/physical/gender/batch
    Reason string            // 人话原因（供前端/LLM 解释）
}

// 注（G4）：RecommendResult（EstimateProb / biz.RecommendCandidates 的输出）定义随算法就近放在 §3.2，本 §2 不重复；
//          其余核心输入/输出 struct（UserQuery/Candidate/CandidateWithProb/WishlistResult 等）均在本 §2。
```

---

## 三、模块实现细节

### 3.0 年份口径（Q16：报考年驱动，禁用日历年 / 库内 MAX(year)）

**`year` = 报考当年**（用户/orchestrator 每请求传入；演示传 `2025`）。所有年份口径由 `year` 推导，**绝不读服务器日历年、不取库内 `MAX(year)`**（库内同时存在 2022-2025，取 MAX 会把验证年 2025 当训练年）：

| 用途 | 取哪年 | 说明 |
|------|--------|------|
| 分数→位次（score_rank_map） | `year`（报考年，2025） | 把报考年考生分数转位次；controlScore = 报考年本科线（2025 物理 405） |
| **概率历史窗口**（major_admission） | **`[year-WindowYears, year-1]`（[2022,2024]）** | **含 WindowYears 年、不含报考年**——报考年录取结果正是预测目标 |
| 计划修正（enrollment_plan） | 今年=`year`（2025）、去年=`year-1`（2024） | `PlanChange{PlanCount:year, PrevPlanCount:year-1}` |
| 候选池基准（enrollment_plan） | `year`（报考年，2025） | 报考年实际招生的院校专业组（DY4，见 §3.2 Step1） |

> ⚠️ **验证完整性（关键）**：2025 的 major_admission 已入库，但它是**验证 holdout（ground truth）**，**绝不能进入概率窗口**——否则用答案预测答案、回测失效。故窗口过滤必须带**上界 `ma.year <= year-1`**（见 §3.x BatchGroupHistory），不能只写下界。
>
> **防御（DY5）**：请求 `year` 对应的 score_rank_map / enrollment_plan 缺失 → **明确报错**（如「报考年 2025 的一分一段表未入库」），**不静默回退**到其他年份（回退会用错口径的位次/计划，结果不可信）。

### 3.1 biz/rank.go — 位次转换

**职责**：分数 → 位次 → 百分位

**核心函数**：

```go
// ConvertByScore 分数转位次（纯函数）
// table + controlScore 均由 data 层查 score_rank_map 后注入（MVP 固定 batch='本科批'）
// controlScore = score_rank_map.control_score（本科线），来源见下方说明
func ConvertByScore(score float64, table []ScoreRankEntry, controlScore int) (RankResult, error)

// RankToPercentile 位次转百分位（抽成独立函数，供 prob 模块复用）
// 百分位 = 位次 / 全省总人数
// 保证「用户百分位」与「历史录取百分位」口径完全一致
func RankToPercentile(rank int, totalCount int) float64 {
    if totalCount <= 0 {
        return 0
    }
    return float64(rank) / float64(totalCount)
}

// ScoreRankEntry 一分一段表条目（对应 score_rank_map 表）
type ScoreRankEntry struct {
    Score           int     // 分数
    ScoreRangeLow   int     // 分数区间下限（如 693-750 中的 693）
    ScoreRangeHigh  int     // 分数区间上限（如 693-750 中的 750）
    SegmentCount    int     // 本分数段人数
    CumulativeCount int     // 累计人数（即位次）
    RankRangeLow    int     // 位次区间下限
    RankRangeHigh   int     // 位次区间上限
}
```

> **controlScore（批次线）来源**：`score_rank_map.control_score` 列（按 province/year/category/batch 存储、完全填充，= 官方批次线；按 year 载入→取【报考年】本科线，实测 2025 物理 405、2024 物理 422/历史 438）。**MVP 固定 batch='本科批'** 指：controlScore 取本科线、candidate SQL 固定本科批、score<本科线报错；但 `RankRepo.LoadScoreRankMap` 载入的是**完整一分一段表（不按 batch 过滤）**——因为位次是全省总排位、与批次无关，且百分位分母须为全省总人数（表内最低分 cumulative=313685，与 BatchGroupHistory 同口径；只载本科批会误得 190233）。controlScore（本科线）作为标量单独返回，service 注入 ConvertByScore；ConvertByScore 签名不变、仍是纯函数。
>
> **为何 RankConvertRequest 不需 batch 字段**：① MVP 内部固定本科批；② 一分一段表跨批次按分数完全不重叠（实测所有 year/category overlap=0，本科批从本科线起、专科批到本科线−1 止），score 本身即可唯一定位行。
>
> **MVP 局限**：仅支持本科批考生；score < 本科线（专科批考生）直接报错"低于本科线，暂不支持"，后续按 score 派生 batch 再扩展专科批。

**算法**（基于实际表结构）：

> score_rank_map 表特点：
> - 大部分分数是单独一行（score_range = "450"）
> - 最高分段合并为一个区间（如 "693-750"，表示 693 分以上共 51 人）
> - cumulative_count 就是位次（全省排名第几名）

**步骤**：

0. **float→int 归整（Q23/DG2-A）**：入口 `s := int(math.Round(score))` 一次性四舍五入——高考分本就是整数，float64 只是类型载体；round 消除浮点毛刺、保证确定性（与 Q24 可复现衔接）。后续匹配与插值统一用 `s`。

1. **检查批次线**：`s < controlScore`（本科线）→ 返回错误 `SCORE_BELOW_CONTROL_LINE`（低于本科线，MVP 仅支持本科批）

2. **精确匹配**：在表中找到 `ScoreRangeLow ≤ s ≤ ScoreRangeHigh` 的行（⚠️ 必须用 score_range 解析出的区间，不能只比 score 列——顶段 694-750 无独立行、被 score=693 行的 '693-750' 覆盖）
   - 如果 score_range 是单个分数（如 "450"，ScoreRangeLow=ScoreRangeHigh=450），直接用 cumulative_count
   - 例：450 分 → cumulative_count = 153585 → 位次 153585

3. **区间插值**：如果分数在合并区间内（如 700 分在 693-750 区间）
   - 区间信息：693-750 分，共 51 人，位次 1-51
   - 假设人数在区间内均匀分布
   - 700 分的位置 = (750 - 700) / (750 - 693) = 50/57 ≈ 87.7%
   - 位次 ≈ 1 + 87.7% × (51 - 1) ≈ **45 名**

4. **算百分位**（调用 `RankToPercentile`）：
   - 全省总人数 = 完整一分一段表最低分的 cumulative_count（位次是全省总排位、与批次无关，故 LoadScoreRankMap 载入**完整表、不按 batch 过滤**；按 year 载入：用户百分位用报考年 total_count、历史百分位用各年自己的，2024 湖南物理类实测 313685）
   - ⚠️ 不可只载本科批表：本科批最低分=本科线 422 的 cumulative 仅 190233（=本科线上人数），当全省总人数会把用户百分位算大、与 prob 历史侧（BatchGroupHistory 取 MAX(cumulative) 跨批次=313685）口径不一致
   - 百分位 = RankToPercentile(位次, 总人数) = 位次 / 总人数
   - 例：位次 45，总人数 200000 → 百分位 = 45/200000 = **0.000225**（超过了 99.98% 的人）
   - 注：prob 模块算历史录取百分位也复用此函数、同一分母口径（全省总人数），确保可直接比较

**边界处理（Q23 收口）**：
- `s < controlScore`（本科线）→ 错误 `SCORE_BELOW_CONTROL_LINE`（FailedPrecondition/400，用户可见；MVP 仅支持本科批，专科批考生暂不支持）
- `s > 表内最高 ScoreRangeHigh`（=750，高考总分上限）→ 错误 `SCORE_ABOVE_MAX`（InvalidArgument/400，用户可见；DG1-A：>750 必是脏输入/抽取错误，静默 clamp 到 rank1 会给“能上任何学校”的垃圾推荐，故报错 surface；与下界对称）。**严禁外推**插值公式（s>750 会让 `(750−s)/(750−693)` 变负 → rank<1 荒谬）
- 表为空 → 错误（Internal/500）
- 错误码统一走 §4.4 错误模型（biz 返回框架无关 sentinel，service 层 toKratosErr 映射）

---

### 3.2 biz/prob.go — 录取概率估计

**职责**：根据用户信息，从所有「院校专业组」中筛选候选，批量计算进档概率，按冲稳保分档输出候选池（成品志愿表由 3.5 OptimizeWishlist 生成）

**志愿单位说明**：湖南是新高考「院校专业组」志愿模式，投档的基本单位是**专业组**而非单个专业。考生进档后，才在组内按分数/服从调剂分配专业。因此**录取概率 = 进入专业组的概率**，由组投档线（组内最低位次）决定，组内专业仅作展示明细。

**位次/百分位复用 3.1 rank 模块**：本模块不重复实现位次换算。
- 用户百分位：调用 `rank.ConvertByScore(score, table, controlScore)` 得到 `RankResult{Rank, Percentile}`
- 历史录取百分位：调用 `rank.RankToPercentile(min_rank, totalCount)`（组投档线已是 min_rank 位次，无需再转位次）
- 各年 `totalCount`：取自对应年份 `score_rank_map`
- **关键**：用户侧与候选侧必须用同一个百分位函数，否则「够得着」判断（用户百分位 ≤ 录取百分位）会因口径不一致而出错

**核心场景**：

```
用户输入：
  - 湖南省、物理类、本科批
  - 分数 650，位次 8000
  
系统输出：
  - 冲：XX 个专业组（概率 20%~50%）
  - 稳：XX 个专业组（概率 50%~80%）
  - 保：XX 个专业组（概率 80%+）
  - 按目标比例推荐志愿表，每个志愿含组内专业明细
```

**数据来源**（基于实际表结构）：

| 表名 | 用途 | 关键字段 |
|------|------|----------|
| `score_rank_map` | 一分一段表，计算用户百分位 + 批次线 | `cumulative_count`, `control_score` |
| `major_admission` | 专业录取数据（候选池，按专业组聚合） | `major_group`, `min_rank` |
| `enrollment_plan` | 报考年(year)招生计划：候选池基准 + 计划修正(今年year/去年year-1) | `plan_count` |
| `major` | 专业信息（选科要求） | `subject_req` |
| `university` | 学校信息 | `is_985`, `is_211`, `city`, `province` |

**专业组聚合口径**：

```
组投档线位次 = 组内所有专业的 MIN(min_rank)
  → 这是「能否进档」的真实门槛
组内专业明细 = 该组下每个专业的详情（代码/名称/选科/学制/学费/备注/历史分数位次）
  → 供展示，帮助考生了解进档后可能就读的专业及其录取参考
```

**⚠️ 两个「位次」概念的区别**（实现时勿混淆）：

| 概念 | 字段 | 含义 | 用途 |
|------|------|------|------|
| 组投档线位次 | `Candidate.GroupMinRank` | 组内所有专业的 `MIN(min_rank)`，即进档门槛 | **概率计算**（决定能否进这个组） |
| 专业级位次 | `MajorDetail.MinRank` | 组内某个专业自己的历史最低位次 | **仅展示**（进档后具体专业录取参考） |

> 录取概率只由「组投档线」决定；专业级位次不影响进档概率，仅供考生了解进档后各专业的热门/冷门程度。

**核心函数**：

```go
// RecommendCandidates 概率计算 + 分档（prob 层主入口，纯函数：数据由 usecase 注入，本函数不碰 SQL）
// = batchEstimateProb（逐组算概率）+ categorize（按阈值分冲/稳/保）的组合封装。
// 输入：已过硬约束的候选 + 用户百分位 + data 层查好的 historyMap/planMap + 配置
// 输出：按冲稳保分档的候选池 RecommendResult（到分档为止；成品志愿表由 3.5 OptimizeWishlist 生成）
// 注（H1/Q31）：编排（rank→LoadCandidates→CheckConstraints→调 Repo 取 historyMap/planMap）在
//   RecommendUsecase.EstimateProb 内部完成（§4.2②）；本函数只吃注入数据，保持 biz 纯函数、无 IO 不变量。
func RecommendCandidates(candidates []Candidate, userPercentile float64,
    historyMap map[GroupKey][]AdmissionRecord, planMap map[GroupKey]PlanChange,
    config RecommendConfig) RecommendResult

// UserQuery 见 2.1（全局唯一定义，含 Category/Batch/Rank/Percentile 等字段）

// RecommendConfig 推荐配置（默认值来自 conf.Bootstrap.Algo / algo.yaml；per-request 用户偏好覆盖见 §4.1②）
type RecommendConfig struct {
    // 筛选条件
    WindowYears     int     // 历史窗口年数（默认 3）；概率窗口=[报考年-W, 报考年-1]，不含报考年（§3.0）
    PreferCities    []string // 偏好城市（可选）
    // 注（G9/YAGNI）：原 PreferTiers/ExcludeTiers 已删除——定义了但无任何过滤逻辑消费（死参，同 CareerGoal 处理）；
    //   学校层次偏好统一走 UserProfile.PreferredTiers（在 utility 层调 tier 权重、非硬过滤，见 §3.4）。后续若真需层次硬过滤再加回。
    
    // 分档参数
    DecayFactor     float64 // 时效衰减（默认 0.7）
    PlanAdjust      float64 // 计划修正因子（默认 0.5）
    RushThreshold   float64 // 冲阈值（默认 0.2）
    StableThreshold float64 // 稳阈值（默认 0.5）
    SafeThreshold   float64 // 保阈值（默认 0.8）
    
    // 注：冲稳保配比参数已移至 3.5 OptimizeWishlist（见 RatioConfig），prob 只负责分档
    
    // 候选池输出控制（用于分档浏览，见 3.6）
    // 后端默认返回全量分档候选，分页/筛选/排序交由前端；此字段仅作可选安全阀
    PerCategoryLimit int    // 每档最多返回数量（默认 0=全量；>0 时截断，一般不用）
    SortBy           string // 排序：utility（默认）/ probability
}
```

**参数说明**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `WindowYears` | 3 | 概率历史窗口年数：[报考年-3, 报考年-1]（报考年2025→[2022,2024]，不含报考年）|
| `DecayFactor` | 0.7 | 时效衰减因子，越近的年份权重越高 |
| `PlanAdjust` | 0.5 | 招生计划变化的修正系数 |
| `RushThreshold` | 0.2 | 概率 < 20% 不推荐，20%~50% 为「冲」 |
| `StableThreshold` | 0.5 | 概率 50%~80% 为「稳」 |
| `SafeThreshold` | 0.8 | 概率 ≥ 80% 为「保」 |

**DecayFactor = 0.7 的选择理由**：

> 这是一个经验值，基于以下考虑：

```
3 年窗口下的权重分布（报考年2025、窗口[2022,2024]，最近年2024权重最高）：
  2024 年：1.0   （100% 可信）
  2023 年：0.7   （70% 可信）
  2022 年：0.49  （49% 可信）

各年占总权重的比例：
  2024: 1.0 / 2.19 = 46%
  2023: 0.7 / 2.19 = 32%
  2022: 0.49 / 2.19 = 22%

效果：近年数据权重高，但远年数据也有参考价值
```

对比其他值：
- **0.5**：太激进，2022 年权重只有 14%，几乎被忽略
- **0.7**：折中，各年权重比约 2:1.4:1，合理
- **0.9**：太保守，三年权重几乎一样（37%/33%/30%），没有体现「近年更重要」

```go
// RecommendResult 推荐结果
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
    // Basis 全局审计依据（EstimateProbReply.basis 来源，§4.2②）。纯函数 RecommendCandidates 不知道用户 rank/percentile，
    //   故由 RecommendUsecase.EstimateProb 编排末尾回填（Phase 5 落地：解决 basis 管道 seam）。
    Basis AuditBasis
}

// CandidateWithProb 见 2.2（= Candidate + ProbEstimate 组合，prob 模块输出）
// 字段访问（Go 内嵌提升）：
//   c.UniversityName / c.MajorGroup / c.Majors / c.GroupMinRank ← 来自 Candidate
//   c.Probability / c.Category / c.Method / c.DataYears ← 来自 ProbEstimate
```

**算法流程**：

```
┌─────────────────────────────────────────────────────────────────┐
│ Step 1: 候选生成（按院校专业组聚合）                              │
│  - 从 major_admission 查询该省份、科类、批次的所有专业组           │
│  - 按 (university_code, major_group) 聚合，取组内 MIN(min_rank) 为投档线 │
│  - 收集组内专业明细 + 组内 DISTINCT subject_req（组装，不过滤）    │
│  - 只取数+组装：LoadCandidates 无 userSubjects，选科过滤不在此    │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│ Step 1.5: 硬约束过滤（constraint.CheckConstraints，见 §3.3）      │
│  - 选科匹配：用户选科满足组内 subject_req（match-any 任一条即过）  │
│  - 输出 kept（通过）+ rejected（带原因）；后续只对 kept 算概率     │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│ Step 2: 批量概率计算（加权命中率法）                                 │
│  - 批量拉取所有专业组过去 3 年的组投档线位次（组内 MIN(min_rank)）  │
│  - 计算各年份百分位：percentile = 组投档线位次 / total_count        │
│  - 计划修正（如果今年招生人数变了）                                   │
│  - 时效加权：weight = decay^(历史记录降序下标)                             │
│  - 加权命中率：统计用户「够得着」的年份比例                            │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│ Step 3: 分档                                                      │
│  - 概率 < 20%：不推荐（淘汰）                                       │
│  - 20% ≤ 概率 < 50%：冲                                             │
│  - 50% ≤ 概率 < 80%：稳                                             │
│  - 概率 ≥ 80%：保                                                   │
└─────────────────────────────────────────────────────────────────┘
                              ↓
              输出 RecommendResult（冲/稳/保 分档候选池，每档 Top N）
                              ↓
        → 成品志愿表交由 3.5 OptimizeWishlist 生成（配比+排序+保底检查）
```

**概率计算方法：加权命中率法**

> **核心思想**：统计历史上用户「够得着」的年份比例

```
判断标准：用户百分位 ≤ 录取百分位 → 「够得着」

计算公式：概率 = Σ(命中年份的权重) / Σ(所有年份的权重)

示例（报考年2025、窗口[2022,2024]）：
  2024 年录取百分位 0.048，权重 1.0
  2023 年录取百分位 0.059，权重 0.7
  2022 年录取百分位 0.068，权重 0.49
  
  用户百分位 0.042：
    2024: 0.042 ≤ 0.048 → ✅ 命中
    2023: 0.042 ≤ 0.059 → ✅ 命中
    2022: 0.042 ≤ 0.068 → ✅ 命中
  
  概率 = (1.0 + 0.7 + 0.49) / (1.0 + 0.7 + 0.49) = 1.0 → 限制到 0.99
```

**概率范围限制**：
- 最低 1%：避免 0% 让用户觉得完全没希望
- 最高 99%：避免 100% 让用户觉得过度承诺

**为什么不用正态分布法**：
- 数据量少（通常只有 3 年），无法准确估计均值和标准差
- 正态分布假设不一定成立
- 加权命中率法简单透明，有统计学依据（频率学派）

**Step 1: 候选生成（详细）**

```sql
-- 候选池基准 = 报考年(year,$5)实际招生的院校专业组（DY4）：以 major_admission(报考年-1,$4，最近历史年) 为统计源，
--   用 EXISTS enrollment_plan(报考年) 把候选池限定在报考年真正招生的组（剔除停招组；报考年新增无历史的组概率层本就跳过）。
-- 参数：$1 province, $2 category, $3 batch, $4 = 报考年-1（如2024）, $5 = 报考年（如2025）
-- ⚠️ 绝不用报考年(2025)的 major_admission 当候选/历史基准——那是验证 holdout（§3.0）。
-- 性能：WHERE 命中复合索引 idx_major_admission_query(province,year,category,batch)，实测 ~16ms（2024物理本科批 3543 组，EXISTS 报考年计划后略减）
SELECT
    ma.university_id,
    ma.university_code,
    ma.university_name,
    ma.major_group,
    MIN(ma.min_rank) AS group_min_rank,   -- 组投档线位次（取自最近历史年 报考年-1）
    u.is_985, u.is_211, u.is_dual_class,  -- → SchoolTier 由 Go 纯函数 deriveSchoolTier 派生（不在 SQL 用 CASE，便于单测）
    u.city AS location,                   -- university 表无 location 字段，用 city 映射到 Candidate.Location
    u.province,                           -- utility 地域"同省"判断用
    u.num_master, u.num_doctor, u.ruanke_rank  -- utility strength（升学资源）维度用
FROM major_admission ma
JOIN university u ON ma.university_id = u.id
WHERE ma.province = $1
  AND ma.category = $2
  AND ma.batch = $3
  AND ma.year = $4                        -- 报考年-1（最近历史年，如2024）：组投档线位次基准
  AND ma.min_rank IS NOT NULL
  AND EXISTS (SELECT 1 FROM enrollment_plan ep          -- DY4：候选池限报考年($5)实际招生的组
              WHERE ep.university_code = ma.university_code AND ep.major_group = ma.major_group
                AND ep.province = $1 AND ep.category = $2 AND ep.batch = $3 AND ep.year = $5)
GROUP BY ma.university_id, ma.university_code, ma.university_name,
         ma.major_group, u.is_985, u.is_211, u.is_dual_class,
         u.city, u.province, u.num_master, u.num_doctor, u.ruanke_rank;
-- 新增的 u.* 列均函数依赖于 ma.university_id（university 主键），加入 GROUP BY 不改变分组粒度

-- 组内专业明细（含代码/选科/分数/位次/人数/备注，JOIN major 拿学制学费）；同 SQL1 口径：最近历史年($4) + EXISTS 报考年($5)计划
SELECT
    ma.university_code, ma.major_group,
    ma.major_code, ma.major_name, ma.subject_req, ma.major_note,
    ma.min_score, ma.min_rank, ma.admit_count,
    m.duration, m.tuition
FROM major_admission ma
LEFT JOIN major m ON ma.major_id = m.id
WHERE ma.province=$1 AND ma.category=$2 AND ma.batch=$3 AND ma.year=$4   -- 报考年-1
  AND EXISTS (SELECT 1 FROM enrollment_plan ep
              WHERE ep.university_code = ma.university_code AND ep.major_group = ma.major_group
                AND ep.province = $1 AND ep.category = $2 AND ep.batch = $3 AND ep.year = $5)  -- 报考年
ORDER BY ma.university_code, ma.major_group, ma.min_rank, ma.major_code, ma.id;
-- 位次并列时按专业代码排序，最后以 major_admission 主键 id 兜底，避免同位次专业交换顺序。

-- 选科过滤不在 SQL/Step1 做（LoadCandidates 无 userSubjects）；由 §3.3 constraint.CheckConstraints 用组装好的 Candidate.SubjectReq 判断（match-any）。category 已在 WHERE 过滤，覆盖首选。
```

**SchoolTier 派生（方案 B：biz 层纯函数，可单测）**：

```go
// deriveSchoolTier 从 university 三个布尔派生学校层次标签（纯函数）
// 优先级 985 > 211 > 双一流 > 普通，与 utility tier 打分（1.0/0.8/0.6/0.4）排序一致
// 现实中 985⊂211 且多在双一流名单内，布尔有重叠，优先级 CASE 保证唯一确定标签
func deriveSchoolTier(is985, is211, isDualClass bool) string {
    switch {
    case is985:       return "985"
    case is211:       return "211"
    case isDualClass: return "双一流"
    default:          return "普通"
    }
}
```

> data 层将 SQL 行映射为 Candidate 时调用此函数填 SchoolTier（SQL 只选三个布尔，不用 CASE 派生，使优先级规则显式在代码中、可单测）。

**候选组装（data 层 `LoadCandidates` 内完成，符合"DB+行映射归 data"）**：

两条 SQL 的结果按 `GroupKey{UniversityCode, MajorGroup}` 在内存合并（与 batchEstimateProb 同一 key 模式，方案 A；不用 array_agg 单 SQL 嵌套）。两 SQL 均以 major_admission(报考年-1) 为统计源、EXISTS enrollment_plan(报考年) 限定候选池（DY4，见 Step1 SQL）：

1. 跑聚合 SQL1 → 建 `[]Candidate` 骨架（SchoolTier=deriveSchoolTier(...)、Location、Province、NumMaster/NumDoctor/RuankeRank、GroupMinRank）+ `map[GroupKey]int` 下标索引。
2. 跑明细 SQL2（按上方完整 ORDER BY 排序，含专业代码和主键并列键）→ 逐行构造 `MajorDetail`，按 GroupKey append 到对应 `Candidate.Majors`（按专业级位次排序，仅展示；同位次亦保持确定顺序）；同时用 slice+seen 按首现顺序收集组内 DISTINCT `subject_req` → 回填 `Candidate.SubjectReq []string`（供 §3.3 选科 match-any）。
3. NULL 容错（LEFT JOIN 保留无 major 的专业，报考年-1（如2024）物理本科批实测 23 行 major_id 为 NULL、167 行 tuition 为 NULL）：Duration NULL→""、Tuition NULL→0、MinScore/MinRank NULL→0。
4. 无明细行的组（理论上不出现，两 SQL 同源同过滤）→ Majors 留空、不报错。

**Step 2: 批量概率计算（详细）**

```go
// 候选复用 2.2 定义的公共 Candidate（院校专业组），不再重复定义

// GroupKey 院校专业组唯一标识（批量查询结果按此分组）
type GroupKey struct {
    UniversityCode string
    MajorGroup     string
}

// PlanChange 招生计划变化（报考年 vs 报考年-1）
type PlanChange struct {
    PlanCount     int // 报考年(year)计划数，如2025
    PrevPlanCount int // 报考年-1(year-1)计划数，如2024
}

// batchEstimateProb 是 biz 层【纯函数】：historyMap / planMap 由 data 层查好后注入，本函数不碰 SQL。
// 性能关键：data 层用两条批量 SQL 一次性拉全量历史与招生计划（各 ~1 万行 / ~20ms / <1MB），
// 再在内存里按 GroupKey 查表，避免在候选循环里逐组查库（N+1：3543 组 × 2 = 7086 次往返）。
// 编排：RecommendUsecase 先 LoadCandidates 取候选 → CheckConstraints 硬约束过滤（选科）→ 调 AdmissionRepo 取 historyMap/planMap → 本函数对 kept 计算。
func batchEstimateProb(candidates []Candidate, userPercentile float64,
    historyMap map[GroupKey][]AdmissionRecord, planMap map[GroupKey]PlanChange,
    config RecommendConfig) []CandidateWithProb {

    results := make([]CandidateWithProb, 0, len(candidates))
    for _, cand := range candidates {
        key := GroupKey{cand.UniversityCode, cand.MajorGroup}
        hist, ok := historyMap[key]
        if !ok || len(hist) == 0 {
            continue // 无历史数据，跳过
        }
        // 拷贝一份，避免下面的计划修正污染 historyMap 里的共享 slice
        history := append([]AdmissionRecord(nil), hist...)

        // 计算百分位（复用 rank.RankToPercentile，与用户百分位口径一致）
        for i := range history {
            history[i].MinPercentile = rank.RankToPercentile(history[i].GroupMinRank, history[i].TotalCount)
        }

        // 计划修正（按专业组的招生计划变化）
        // 简化：用「今年 vs 去年」的计划变化率统一修正各年百分位（近似），不逐年还原
        if plan, ok := planMap[key]; ok && plan.PrevPlanCount > 0 {
            change := float64(plan.PlanCount-plan.PrevPlanCount) / float64(plan.PrevPlanCount)
            for i := range history {
                history[i].MinPercentile *= (1 + change * config.PlanAdjust)
            }
        }

        // 时效加权 + 概率计算
        prob := calculateProbability(userPercentile, history, config)

        results = append(results, CandidateWithProb{
            Candidate: cand, // 内嵌候选全部字段（UniversityName/MajorGroup/Majors 等）
            ProbEstimate: ProbEstimate{
                Probability: prob,
                Method:      "weighted_hit", // 标注计算方法，满足可审计原则
                DataYears:   len(history),
            },
        })
    }
    return results
}

// —— 以下批量查询在 data 层实现（biz.AdmissionRepo 接口方法），SQL 如下（province/category 等均参数化）——

// AdmissionRepo.BatchGroupHistory：所有院校专业组窗口 [报考年-W, 报考年-1] 的投档线位次，按 GroupKey 分组。
//   SELECT ma.university_code, ma.major_group, ma.year,
//          MIN(ma.min_rank) AS group_min_rank, srm.total_count
//   FROM major_admission ma
//   JOIN (SELECT year, MAX(cumulative_count) AS total_count
//         FROM score_rank_map WHERE province=$1 AND category=$2 GROUP BY year) srm
//     ON ma.year = srm.year
//   WHERE ma.province=$1 AND ma.category=$2 AND ma.batch=$3
//     AND ma.year >= $4           -- $4 = 报考年 - WindowYears（如 2025-3=2022）：窗口下界
//     AND ma.year <= $5           -- $5 = 报考年 - 1（如 2024）：窗口上界，⚠️绝不含报考年(2025)——它是验证 holdout（§3.0）
//     AND ma.min_rank IS NOT NULL
//   GROUP BY ma.university_code, ma.major_group, ma.year, srm.total_count
//   ORDER BY ma.university_code, ma.major_group, ma.year DESC
// 数据契约：返回 map[GroupKey][]AdmissionRecord；因 ORDER BY year DESC，每个 key 的 slice
//   天然按年份降序（窗口最近年 报考年-1 在前），与 calculateProbability 时效衰减权重（index 越小权重越高）对齐；
//   每条只填 GroupMinRank + TotalCount，MinPercentile 留空由 biz 层 rank.RankToPercentile 计算；
//   total_count = 该年一分一段表最低分的 cumulative_count（全省总人数）。

// AdmissionRepo.BatchGroupPlan：所有专业组招生计划变化（报考年 vs 报考年-1），同样避免 N+1。
//   SELECT university_code, major_group, year, SUM(plan_count) AS plan_count
//   FROM enrollment_plan
//   WHERE province=$1 AND category=$2 AND year IN ($3, $4)   -- $3=报考年(2025), $4=报考年-1(2024)
//   GROUP BY university_code, major_group, year
// 数据契约：按 GroupKey 组装 PlanChange{PlanCount: 报考年, PrevPlanCount: 报考年-1}

// —— biz/repo.go：biz 定义接口、data 实现（依赖倒置，wire 注入）——
type AdmissionRepo interface {
    LoadCandidates(ctx, province, category, batch string, year int) ([]Candidate, error)                          // year=报考年：候选池=报考年 enrollment_plan 覆盖的组，统计取 year-1（§3.0/§3.2）
    BatchGroupHistory(ctx, province, category, batch string, year, windowYears int) (map[GroupKey][]AdmissionRecord, error)  // 窗口=[year-windowYears, year-1]（含上下界，绝不含报考年）
    BatchGroupPlan(ctx, province, category string, year, prevYear int) (map[GroupKey]PlanChange, error)           // year=报考年, prevYear=报考年-1
}
type RankRepo interface {
    // LoadScoreRankMap 查【完整】一分一段表（不按 batch 过滤：位次为全省总排位、与批次无关；表内最低分 cumulative=全省总人数，供 RankToPercentile 分母）+ controlScore（本科线，标量，供 MVP 批次校验）
    LoadScoreRankMap(ctx, province, category string, year int) (table []ScoreRankEntry, controlScore int, err error)
}

// calculateProbability 计算单个候选的录取概率（加权命中率法，二元命中）
// 核心思想：统计历史上用户「够得着」的年份比例
// 注：二元命中 → 概率为有限离散值，同档内区分度由 utility 兜底；
//     连续性修正（基于距离的线性修正）为后续优化，见 §6.1「已知局限」。
func calculateProbability(userPercentile float64, history []AdmissionRecord, config RecommendConfig) float64 {
    if len(history) == 0 {
        return 0.01 // 无历史数据，返回最低概率
    }
    
    // 计算各年份权重（时效衰减）
    weights := make([]float64, len(history))
    for i := range history {
        weights[i] = math.Pow(config.DecayFactor, float64(i))
    }
    
    // 加权命中率：统计用户「够得着」的年份
    var sumWeight, sumHit float64
    for i, h := range history {
        sumWeight += weights[i]
        // 判断是否「够得着」：用户百分位 ≤ 录取百分位（用户更强）
        if userPercentile <= h.MinPercentile {
            sumHit += weights[i]
        }
    }
    
    prob := sumHit / sumWeight
    
    // 限制概率范围在 [1%, 99%]
    // 避免极端值，让结果更可信
    return math.Max(0.01, math.Min(0.99, prob))
}
```

**Step 3: 分档（详细）**

```go
func categorize(candidates []CandidateWithProb, config RecommendConfig) RecommendResult {
    result := RecommendResult{TotalCandidates: len(candidates)}
    
    for _, c := range candidates {
        if c.Probability < config.RushThreshold {
            // 概率 < 20%，不推荐（淘汰）
            result.FilteredCount++
            continue
        } else if c.Probability < config.StableThreshold {
            c.Category = "冲"
            result.RushCandidates = append(result.RushCandidates, c)
        } else if c.Probability < config.SafeThreshold {
            c.Category = "稳"
            result.StableCandidates = append(result.StableCandidates, c)
        } else {
            c.Category = "保"
            result.SafeCandidates = append(result.SafeCandidates, c)
        }
    }
    
    // H3/Q33：回填分档 total（EstimateProbReply.rush/stable/safe_total 的来源，不填则前端分页/“还有 N 个”恒为 0）。
    //   MVP PerCategoryLimit=0 不截断 → *Total == len(*Candidates)；
    //   若 PerCategoryLimit>0 截断，则 *Total 记截断前数量、*Candidates 保留截断后（与字段注释“截断前”一致）。
    result.RushTotal = len(result.RushCandidates)
    result.StableTotal = len(result.StableCandidates)
    result.SafeTotal = len(result.SafeCandidates)
    
    return result
}
```

> **Step 4 已移至 3.5 `OptimizeWishlist`**：`RecommendCandidates` 到分档（Step 3）为止，输出分档候选池 `RecommendResult`。按比例选成品表、效用排序、保底健全性检查统一由 3.5 `OptimizeWishlist` 负责，避免职责重复。

**完整示例**：

```
用户输入：
  湖南省、物理类、本科批
  分数 580 → 位次 23180（一分一段表实测；位次是全省总排位、与批次无关）
  选科：物理、化学、生物

Step 1: 候选生成（按专业组聚合，只取数+组装）
  查询 major_admission 2024 物理类本科批 → 按 (学校,专业组) 聚合
  得到 3543 个专业组（实测；每组取组内 MIN(min_rank) 为投档线，组装 Majors/SubjectReq）

Step 1.5: 硬约束过滤（constraint.CheckConstraints）
  选科过滤（物理+化学+生物，组级 match-any）→ 约 3400 个候选专业组
  （示意；category 已过滤首选，再选化学/生物命中绝大多数组）

Step 2: 批量概率计算
  用户百分位 = rank.RankToPercentile(23180, 313685) = 0.0739
    313685 = 2024 湖南物理类全省总人数 = 完整一分一段表最低分 cumulative（跨所有批次，与 BatchGroupHistory 同口径）
  各组录取百分位 = rank.RankToPercentile(组投档线位次, 该年全省总人数)
  （两者复用同一函数、同一分母，才能直接比较）

  示例结果（概率随录取难度递减——越难录取概率越低）：
  - 北京大学（物理组）：概率 0.01（淘汰，顶尖够不着）
  - 中南大学（物理组）：概率 0.18（冲）
  - 湖南大学（105组）：概率 0.35（冲）
  - 长沙理工（物理组）：概率 0.72（稳）
  - 湘潭大学（109组，含法学/经济学类）：概率 0.88（保）
  - 湘潭大学（110组，含计算机/数学）：概率 0.92（保）
  - ...（概率为示意值，真实取决于各组 3 年历史）

Step 3: 分档（示意分布；用户较强 → 保档占多数）
  淘汰（概率<20%）：约 200 个
  冲（20%~50%）：约 400 个
  稳（50%~80%）：约 600 个
  保（80%+）：约 2200 个

Step 3 输出：RecommendResult（全量分档候选池）
  后端默认返回全量（PerCategoryLimit=0 不截断，该字段仅审计参考；最终每档数量由 3.5 OptimizeWishlist 控制）
  附 RushTotal/StableTotal/SafeTotal，分页/筛选/排序交前端

Step 4（交由 3.5 OptimizeWishlist）：生成成品志愿表
  湖南本科批最多填 45 个院校专业组志愿
  均衡风险偏好配比（冲30%/稳40%/保30%，= §3.5 RatioConfig 默认）：13 + 18 + 14 = 45 个
  → 效用分排序 + 保底健全性检查（防滑档）
```

**性能优化**：

| 优化点 | 方案 |
|--------|------|
| 候选数量大 | 批量查询，避免逐条 SQL |
| 概率计算耗时 | **MVP 单线程即够**（瓶颈是 DB N+1、已用批量 SQL 解决 ~32ms；内存循环 ~3400 组纯算术 ≪2s）。并行（goroutine）降级为 **M2+ 按需优化**，且必须 indexed slice 保序（`results[i]` 按输入下标写）以不破坏可复现（Q24/DH1-A，见 §1.2）|
| 历史数据重复查询 | 缓存（同一批次总人数相同） |
| 内存占用 | 流式处理，不一次性加载所有候选 |

**边界情况处理**：

| 情况 | 处理方式 |
|------|----------|
| 候选数量 < 目标数量 | 放宽筛选条件，或提示「候选不足」 |
| 某档候选不足 | 调整比例，如冲档不足则从稳档补充 |
| 所有候选概率都很低 | 提示「建议降低批次或地域要求」 |
| 选科不匹配 | 提前过滤，不进入概率计算 |

---

### 3.3 biz/constraint.go — 硬规则裁决

**职责**：过滤不符合条件的候选

**核心函数**：

```go
// CheckConstraints 检查硬约束（纯函数，作用于 []Candidate——约束过滤先于概率，见 §3.3 编排）
// 返回：通过的候选 + 被拒的候选（带规则 key 与原因）
// 注（G5/G6）：全链统一用 RejectedCandidate（§2.2，嵌入 CandidateWithProb），不再有第二个 Rejection 类型。
//   本纯函数处于概率计算之前，故其 rejected 项的 CandidateWithProb.ProbEstimate 为零值（仅填 Candidate+Rule+Reason）；
//   独立 CheckConstraints RPC 由 usecase 层按 GroupKey 从入参池回填 prob（见 §4.2③）。
func CheckConstraints(query UserQuery, candidates []Candidate) (kept []Candidate, rejected []RejectedCandidate, err error)

// 触发规则 key 常量（RejectedCandidate.Rule 取值，与 proto RejectedCandidate.rule 枚举一致）
//   subject_req / single_score / physical / gender / batch

// SubjectReq 解析后的选科要求（湖南 3+1+2：首选 + 再选）
type SubjectReq struct {
    FirstChoice  string   // 首选：物理/历史/不限
    ReselectMode string   // 再选模式：none(不限)/atLeastOne(2选1)/all(单科或2科必选)
    Reselect     []string // 再选科目（归一化后）；none 时为空
}

// parseSubjectReq 解析文本选科要求（纯函数）
// 数据源 major_admission.subject_req，实测全库仅 30 种，格式统一 "首选X，再选Y"
// 空串/NULL → 视为不限（FirstChoice=不限, ReselectMode=none），不因脏数据误杀
// 无法解析的未知格式 → 返回 error，调用方据此剔除该候选（硬约束保守）
func parseSubjectReq(text string) (SubjectReq, error)

// normalizeSubject 科目名归一化（政治→思想政治 等，与库中口径对齐）
func normalizeSubject(s string) string

// matchSubjects 用户选科是否满足一条选科要求（纯函数）
func matchSubjects(userSubjects []string, req SubjectReq) bool
```

**检查项**：

1. **选科匹配**（湖南 3+1+2：首选物理/历史 + 再选 2 门）
   - **数据源**：`major_admission.subject_req` 文本（非 major.subject_req 的 jsonb）。实测全库仅 30 种 distinct，格式统一 `首选{物理/历史/不限}，再选{...}`；再选只有四类：`不限` / 单科（如`化学`）/ `X/Y(2选1)` / `X、Y(2科必选)`。
   - **解析 `parseSubjectReq`**：按 `，` 切首选/再选；再选分派——含 `(2选1)`→按 `/` 拆、Mode=atLeastOne；含 `(2科必选)`→按 `、` 拆、Mode=all；`不限`→Mode=none；单科→Mode=all（1 门）。科目名经 `normalizeSubject`（政治→思想政治）。
   - **匹配 `matchSubjects`**：首选 req=不限→过，否则须 == 用户首选（候选 SQL 已按 category 过滤，此处冗余保险）；再选 none→过、atLeastOne→用户再选 ∩ req ≠ ∅、all→req ⊆ 用户再选。用户再选 = userSubjects 去掉首选（物理/历史）。
   - **组级 match-any**：`Candidate.SubjectReq` 是组内 distinct 列表（实测 99.76% 长度=1，仅 32 组为 2），满足**任一条**即通过。
   - **边界**：空串/NULL subject_req → 当作"不限"放过（不因脏数据缩候选池）；无法解析的未知格式 → `parseSubjectReq` 返回 error → **剔除该候选并记 `RejectedCandidate.Reason="选科要求无法解析"`**（硬约束保守）。
   - 例：组内要求 `首选物理，再选化学/生物(2选1)`，用户选 `["物理","化学","政治"]` → 再选{化学} ∩ {化学,生物} ≠ ∅ → ✅；用户选 `["物理","政治","地理"]` → 交集为空 → ❌。
   - 注：选科过滤**唯一落本节 CheckConstraints**（编排：LoadCandidates → CheckConstraints → batchEstimateProb → categorize）；§3.2 Step1 的 LoadCandidates 只组装 Candidate.SubjectReq、不做过滤（其签名无 userSubjects）。解析/匹配纯函数均在 constraint.go。

2. **批次/分数线**
   - 用户分数 ≥ 批次线
   - 由 service 层预先过滤

3. **体检限报**
   - 根据《普通高等学校招生体检工作指导意见》
   - 需要 RAG 检索章程中的体检要求
   - **M1 阶段**：暂不实现，标记 TODO

4. **单科要求**
   - 某些专业要求单科成绩（如英语 ≥ 120）
   - **M1 阶段**：暂不实现，标记 TODO

---

### 3.4 biz/utility.go — 效用打分

**职责**：计算用户对每个候选的"心动分"，拉开同档内区分度（概率二元粗粒度的档内排序全靠它兜底，见 §6.1）

**核心函数**（批量入口——升学资源维度需当批全体候选的 min-max 统计，无法单候选独立打分）：

```go
// CalculateUtilityBatch 批量计算效用分（纯函数）
// 流程：① 扫一遍 candidates 算归一化统计 NormStats → ② 逐个算四维得分 → ③ 按权重合成
func CalculateUtilityBatch(profile UserProfile, candidates []Candidate, config UtilityConfig) ([]UtilityScore, error)

// UtilityConfig 效用打分配置
type UtilityConfig struct {
    // 四维默认权重，key：tier/major/location/strength，默认各 0.25。
    // 来源（Q19，补齐 Q12 遗漏）：conf.Bootstrap.Algo.utility_weights（UtilityWeights message，algo.yaml 配），
    //   NewOptimizeUsecase 构造时转成 map 注入（OptimizeUsecase 持有）；yaml 缺失某维→兑底 0.25。
    //   不强制 Σ=1：基准值经 PriorityDims/PreferredTiers ×倍率后统一归一化（见下“权重提取”）。
    DefaultWeights map[string]float64
}

// NormStats 归一化统计（当批全体候选的 min/max，CalculateUtilityBatch 内部第一步算出）
type NormStats struct {
    MasterMin, MasterMax   int // 硕士点数 min/max
    DoctorMin, DoctorMax   int // 博士点数 min/max
    RuankeMax              int // 软科排名最大值（排名越小越好，反向用）
}
```

**打分维度**（维度 key 全局统一为 `tier/major/location/strength`，PriorityDims/Breakdown/Weights/公式四处同口径）：

| key | 维度 | 计算逻辑 | 数据来源 |
|-----|------|---------|---------|
| `tier` | 学校层次 | 985=1.0, 211=0.8, 双一流=0.6, 普通=0.4 | candidate.SchoolTier |
| `major` | 专业匹配 | **关键词包含**：任一 interest 作为子串命中任一组内 MajorName → 1.0，否则 0.0（后续优化：LLM 语义匹配） | profile.Interests vs candidate.Majors[].MajorName |
| `location` | 地域匹配 | 偏好城市命中=1.0, 同省=0.8, 其他=0.5（原"相邻省"档已砍——库中无省份邻接数据，不补配置表，YAGNI） | profile.PreferredCities vs candidate.Location + candidate.Province |
| `strength` | 升学资源 | 0.4×minmax(NumMaster) + 0.3×minmax(NumDoctor) + 0.3×(1 − RuankeRank/RuankeMax)；minmax(x)=(x−min)/(max−min)，max==min 时取 0.5；缺失值（如 RuankeRank≤0）该项按 0 计 | candidate.NumMaster/NumDoctor/RuankeRank + NormStats |

**权重提取**：
- 基准 = `config.DefaultWeights`（四维各 0.25）
- `profile.PriorityDims` 命中的维度权重 ×2
- **tier 维额外规则**：`profile.PreferredTiers` 非空也视为"用户强调学校层次" → tier 权重 ×2；与 PriorityDims 含 "tier" 是同一信号，**封顶 ×2 不叠加**（避免过度主导）。PreferredTiers **只调权重、不作硬过滤、不改 tier 得分**（得分仍用 SchoolTier 固定映射）——非偏好层次的候选仍保留，只是相对靠后。
- 所有倍率应用后整体归一化使 Σweight=1
- 例：`PriorityDims=["major"]` 且 `PreferredTiers=["985","211"]` → tier ×2、major ×2、location ×1、strength ×1 → 归一化后 0.33/0.33/0.17/0.17

**计算公式**：
```go
score = tier_score * weights["tier"] +
        major_score * weights["major"] +
        location_score * weights["location"] +
        strength_score * weights["strength"]   // Σweights=1 → score ∈ [0,1]
```

---

### 3.5 biz/optimize.go — 志愿表优化

**职责**：从候选中选出最优 L 个，并排序

**核心函数**：

```go
// OptimizeWishlist 优化志愿表
func OptimizeWishlist(candidates []CandidateWithScore, params OptimizeParams) (WishlistResult, error)

// OptimizeParams 优化参数（默认值来自 conf.Bootstrap.Algo / algo.yaml；per-request profile 覆盖在 OptimizeUsecase 内合并，见 §4.1②/Q20）
type OptimizeParams struct {
    MaxWishlistSize int     // 最多填几个志愿（L）
    MinSafeCount    int     // 最少「稳拿保底」数（p≥SafeGuardThreshold 的硬下限，显式可审计）。
                            //   默认 = conf.Algo.min_safe_count（yaml 绝对值 9），非运行时跑公式；
                            //   max(3, ⌈L×20%⌉)（L=45→9）只是该默认值的推导说明（Q22/DF1-A）。
                            //   checkSafeGuard 用它作 minSafe（不再内联公式）；MVP 无请求级覆盖入口，恒取 conf 值。
    TargetRatio     RatioConfig // 目标冲稳保比例（配比单一真相源）
    // 注（Q20/DR1）：RiskAppetite 字段已删除——它是用户偏好、已在 UserProfile（per-request profile 携带），
    //   不在算法参数结构里重复；OptimizeWishlist 只认 TargetRatio。RiskAppetite→TargetRatio 转换见 RatioFromRisk。
}

// RatioConfig 冲稳保比例配置
type RatioConfig struct {
    Rush   float64 // 默认 0.3
    Stable float64 // 默认 0.4
    Safe   float64 // 默认 0.3
}

// RatioFromRisk 风险偏好 → 冲稳保配比（纯函数，实现 §3.6 表；Q20/DR2）
// 空/未知 risk → balanced 默认（防御，不报错）。可单测。
func RatioFromRisk(risk string) RatioConfig {
    switch risk {
    case "conservative": return RatioConfig{0.2, 0.4, 0.4}
    case "aggressive":   return RatioConfig{0.45, 0.35, 0.2}
    default:             return RatioConfig{0.3, 0.4, 0.3} // balanced / 空 / 未知
    }
}
```

**算法**：

**Step 1: 分档统计**
```go
rushCands := filter(candidates, c => c.Category == "冲")
stableCands := filter(candidates, c => c.Category == "稳")
safeCands := filter(candidates, c => c.Category == "保")
```

**Step 2/3: 按配比选择与补位**
- 各档配额按 `round(L×ratio)` 计算，保档吸收取整余数，各档内按效用降序、GroupKey 升序选取。
- 某档不足时，剩余空位按全局效用补齐；全程按 GroupKey 去重且总数不超过 L。
- 保底由 Step5 统一处理，不执行额外局部搜索。

**Step 4: 排序**
- 按**全序比较器**排列：`UtilityScore` 降序 + `GroupKey` 升序 tie-break（同 Step2，Q24/DH2-A）；FinalRank = 排序后下标+1，随之确定
- 不强制概率递增；保底补齐后再次全表排序并重赋 FinalRank。

**Step 5: 保底健全性检查（防滑档）**

复用 §3.2 的概率进行保底数量检查。`Π(1−pᵢ)` 仅是独立事件假设下的简化指标，未建模相关性、退档、调剂及政策变化，不可解释为真实滑档率或录取保证。

```go
// SafeGuardThreshold 稳拿保底阈值（比分档 SafeThreshold=0.8 更严）：
//   分档的"保"= p≥0.8（进保档池）；此处"稳拿保底"= p≥0.9（防滑档硬标准）。
//   落地为 config 字段 params.SafeGuardThreshold（conf.Algo.safe_guard_threshold），代码兜底默认 defaultSafeGuardThreshold=0.9。

// checkSafeGuard 统计稳拿数，在 limit 内补入或替换最低效用非稳拿项。
// 契约：safePool = 调用方传入的「p≥threshold 且未入选 wishlist」候选（稳拿池，已按 p 降序），
//       故追加的必然达标、safeGuard++ 不虚高；minSafe = params.MinSafeCount（不再内联公式）。
// ⚠️ 实现修正（Phase 4）：wishlist 用指针 *[]CandidateWithScore 传入——文档初稿签名返回值切片，
//       但内部 append 的保底无法传播回调用方（Go 值切片 append 不改变调用方底层数组），故改指针。
//       threshold 亦作参数传入（非硬编码 0.9），由 params.SafeGuardThreshold 提供。
func checkSafeGuard(wishlist *[]CandidateWithScore, safePool []CandidateWithScore, minSafe int, threshold float64, limit int) OptimizationReport {
    safeGuard := count(*wishlist, func(c CandidateWithScore) bool { return c.Probability >= threshold })

    sortByUtility(*wishlist)
    for _, c := range safePool {
        if safeGuard >= minSafe { break }
        if len(*wishlist) < limit {
            *wishlist = append(*wishlist, c)
        } else {
            replace := -1
            for i := len(*wishlist)-1; i >= 0; i-- {
                if (*wishlist)[i].Probability < threshold { replace = i; break }
            }
            if replace < 0 { break }
            (*wishlist)[replace] = c
        }
        safeGuard++
    }
    sortByUtility(*wishlist)
    for i := range *wishlist { (*wishlist)[i].FinalRank = i+1 }

    // 独立事件近似指标，并非真实滑档风险
    slipRate := 1.0
    satSum := 0.0
    for _, c := range *wishlist {
        slipRate *= (1 - c.Probability)
        satSum += c.Score // 内嵌 UtilityScore.Score（效用总分）
    }
    expSat := 0.0
    if n := len(*wishlist); n > 0 { expSat = satSum / float64(n) } // 空表防除零
    return OptimizationReport{
        SafeCount:            safeGuard,
        SafeGuardOK:          safeGuard >= minSafe,
        SlipRate:             slipRate,
        ExpectedSatisfaction: expSat,
    }
}
```

> **实现注记（Phase 4 落地差异）**：
> - **Step2/3 合并为 `selectByRatio`**：文档初稿「先全局贪心填 L、再比例调整」两步，实现改为**按配比定向配额**——各档配额 `round(L×ratio)`（保档配额兜底 `L−rushT−stableT` 吸收取整误差），各档内按全序比较器取配额最优；某档不足配额时空位按全局 utility 降序从剩余候选补齐。确定性更强、直接命中目标配比，无「调整」的收敛歧义。
> - **`checkSafeGuard` 指针签名**：见上（append 传播修正）。
> - **RushCount/StableCount** 由 `OptimizeWishlist` 在 checkSafeGuard 之后从最终志愿表按 Category 统计（保底补入或替换可能改变分布），checkSafeGuard 只算 SafeCount/SafeGuardOK/SlipRate/ExpectedSatisfaction。

- `minSafe = params.MinSafeCount`，默认取 `conf.Algo.min_safe_count`（yaml 绝对值 9）——**非运行时跑公式**；`max(3, ⌈L×20%⌉)`（L=45→9）只是该默认值的**推导说明**（Q22/DF1-A：绝对值显式可审计，优于比例派生）
- **config 耦合 note（Q22）**：`min_safe_count` 语义上 = `max(3, ⌈max_wishlist_size×20%⌉)`，改 `max_wishlist_size` 时需**同步** `min_safe_count`；二者不一致时**以显式 `min_safe_count` 为准**（它是硬下限真相源）
- **DF2 预留（MVP 不实现）**：L(max_wishlist_size) 当前 **config-only**（OptimizeWishlistRequest/UserProfile 均无 L 字段）→ 运行时 minSafe 恒 = conf 值，无“重算 vs 用 9”歧义。若未来给请求/UserProfile 加 L 覆盖入口，则 minSafe 必须随覆盖后的 L 重算 `max(3,⌈L×20%⌉)`（届时 DF1 应切到比例派生 B）
- **safePool 由调用方在 Step5 前构造**：从全部候选筛出「p≥SafeGuardThreshold(0.9) 且不在 wishlist」的组，按 p 降序供追加——因此追加即达标，`safeGuard++` 不会虚高
- 满额时保底通过替换补齐，不突破 L；池不足则告警。乘积数值再小也不能据此作“不会滑档”的承诺。
- 若稳拿池不足（如压线考生凑不出 p≥0.9 的保底），`SafeGuardOK=false`，上抛告警「保底不足，有滑档风险」，交 orchestrator 提示用户。**这是告警非 error（Q21/DE3）**：OptimizeWishlist 仍正常返回 WishlistResult（SafeGuardOK=false 在 OptimizationReport 内），不返回 gRPC error——见 §4.4。

---

### 3.6 输出模式与交互契约

**产品形态：混合模式（AI 生成草稿 + 候选池自选）**

算法层不决定交互方式，只同时提供两种输出能力，由 orchestrator/前端编排：

| 模式 | 接口 | 输出 | 前端用途 |
|------|------|------|----------|
| 一键生成草稿 | `OptimizeWishlist`（3.5） | 成品志愿表（L 个，已排序+配比+保底检查） | 主视图 |
| 候选池浏览 | `EstimateProb`（3.2） | 全量分档候选（前端分页/筛选/排序） | 用户增删改 |

**交互流程**：

```
① 进入 → 一键生成草稿（两步：先出候选池，再优化）
   EstimateProb(score,…) → 候选池 []CandidateWithProb（含概率/分档）
   OptimizeWishlist(candidates, profile)   // profile.RiskAppetite → usecase 内 RatioFromRisk 派生 TargetRatio；MaxWishlistSize 默认 45（←conf，Q20）
   → 45 个志愿（冲稳保分色）+ 保底健全性检查

② 浏览候选池（想换某个志愿时）
   EstimateProb(query, {SortBy, PreferCities...})   // 默认全量，不截断
   → 冲/稳/保 全量返回（约 600/900/1500），前端做分页/筛选/排序/虚拟滚动

③ 用户增删改 → 实时反馈
   每次改动后把编辑后的 candidates 重跑 OptimizeWishlist（不重新取数/算概率）
   → 重新计算冲稳保配比 + 保底检查
   → 保底不足（SafeGuardOK=false，即稳拿保底数 < params.MinSafeCount）时告警
```

**为什么由前端分页**：候选池约 3000+ 个专业组（冲600/稳900/保1500），原始 JSON ~4MB、gzip 后 ~600KB，一次传输可接受。后端**不做分页/截断**（省去游标/offset 复杂度），一次返回全量分档候选；分页、筛选、排序、虚拟滚动全在前端本地完成——用户增删改志愿时候选池已在本地，交互零延迟。`RushTotal/StableTotal/SafeTotal` 供前端显示分档统计。

> 演进：当前仅限湖南省、候选量级可控；若未来扩到多省份导致候选增至数万条，再引入后端分页。

**风险偏好驱动草稿风格**（`UserProfile.RiskAppetite` → 经 `RatioFromRisk` 纯函数转 `OptimizeParams.TargetRatio`，见 §3.5/Q20）：

| 偏好 | 冲稳保比例 | 适合人群 |
|------|-----------|---------|
| conservative 保守 | 20% / 40% / 40% | 求稳，优先不滑档 |
| balanced 均衡（默认，空/未知 risk 亦归此） | 30% / 40% / 30% | 大多数考生 |
| aggressive 激进 | 45% / 35% / 20% | 分数有优势，愿冲名校 |

> 转换 + 合并在 **OptimizeUsecase 内部**（Q20/DR3）：默认 TargetRatio←conf.Algo.target_ratio（=balanced）；若 profile.RiskAppetite 非空 → 用 RatioFromRisk(risk) 覆盖。service 只解析 profile、不做合并（薄 service，Q18）。MVP 不暴露裸 TargetRatio 给请求（DR4）。

**职责边界**：
- **algo 层**：纯计算、无状态，对外暴露 `EstimateProb`（候选池，内部调 biz `RecommendCandidates`）和 `OptimizeWishlist`（成品表）两个 RPC
- **orchestrator/前端**：决定“先给草稿还是先给候选池”、如何呈现、如何缓存用户已选
- **草稿状态**（已选/已改）由上层（会话/DB）维护；algo 每次接收完整候选列表重新计算，不保存中间状态

---

## 四、Service 层集成（service/algo.go）

### 4.1 配置加载与依赖注入（conf.Bootstrap + wire）

**① 配置加载（conf.Bootstrap，A1 外部化）**

Kratos 正统：`conf/conf.proto` 定义 `Bootstrap`（Server/Data/Algo 三段），`main.go` 用 `config.New(config.WithSource(file.NewSource(*confPath)))` 加载单个 YAML 并 `Scan` 进 Bootstrap，再 `wireApp(&bc)` 注入。`-conf` 默认 `configs/algo.yaml`；不能加载整个 configs 目录，避免其他服务配置覆盖。环境变量 `ALGO_DATA_DSN` 优先于 YAML；配置仅在启动时读取，不承诺热更新。

```proto
// conf/conf.proto
message Bootstrap {
  Server server = 1;
  Data   data   = 2;
  Algo   algo   = 3;   // 算法参数（A1：外部化到 yaml，可不重编译调参）
}
// Server 段（对齐现有 configs/algo.yaml 的 server.grpc.{addr,timeout}；G3 补齐定义）
message Server {
  message GRPC { string addr = 1; string timeout = 2; }  // 如 addr="0.0.0.0:9002"、timeout="30s"
  GRPC grpc = 1;
}
message Data { message Database { string dsn = 1; } Database database = 1; }
message Algo {
  int32  window_years         = 1;  // 默认 3
  double decay_factor         = 2;  // 默认 0.7
  double plan_adjust          = 3;  // 默认 0.5
  double rush_threshold       = 4;  // 默认 0.2
  double stable_threshold     = 5;  // 默认 0.5
  double safe_threshold       = 6;  // 默认 0.8
  double safe_guard_threshold = 7;  // 默认 0.9（计入稳拿保底的最低录取概率）
  int32  max_wishlist_size    = 8;  // 默认 45（湖南本科批 L）
  int32  min_safe_count       = 9;  // 默认 9 = max(3, ⌈max_wishlist_size×20%⌉) 的推导值（Q22/DF1-A：存绝对值、显式可审计）；改 max_wishlist_size 需同步本值，不一致以本值为准
  message Ratio { double rush = 1; double stable = 2; double safe = 3; }
  Ratio  target_ratio         = 10; // 默认 0.3/0.4/0.3
  int32  per_category_limit   = 11; // 默认 0=全量（Q1）
  // Q19：utility 四维默认权重（补齐 Q12 遗漏）；缺失维构造时兑底 0.25，不强制 Σ=1（最终由 PriorityDims/PreferredTiers ×倍率后归一化）
  message UtilityWeights { double tier = 1; double major = 2; double location = 3; double strength = 4; }
  UtilityWeights utility_weights = 12; // 默认各 0.25
}
```

```yaml
# configs/algo.yaml 新增 algo 段（server/data 段已有）
algo:
  window_years: 3
  decay_factor: 0.7
  plan_adjust: 0.5
  rush_threshold: 0.2
  stable_threshold: 0.5
  safe_threshold: 0.8
  safe_guard_threshold: 0.9
  max_wishlist_size: 45
  min_safe_count: 9          # = max(3, ⌈max_wishlist_size×20%⌉)；改 max_wishlist_size 需同步（Q22）
  target_ratio: { rush: 0.3, stable: 0.4, safe: 0.3 }
  per_category_limit: 0      # 0=全量（Q1）
  utility_weights: { tier: 0.25, major: 0.25, location: 0.25, strength: 0.25 }  # Q19：四维默认权重（缺失兑底 0.25，不强制 Σ=1）
```

```go
// cmd/algo/main.go（弃用旧骨架：不再硬编码 :9002 / algo.NewService()）
// ⚠️ 配置源必须精确到本服务文件 configs/algo.yaml，不能用 file.NewSource("configs/")：
//    configs/ 目录下并存 gateway.yaml / llm-gateway.yaml，Kratos file source 会合并全部 yaml，
//    llm-gateway.yaml 的 server.grpc.addr=0.0.0.0:9001 会覆盖本服务的 9002（实现期实测踩坑）。
c := config.New(config.WithSource(file.NewSource("configs/algo.yaml")))
c.Load(); var bc conf.Bootstrap; c.Scan(&bc)
app, cleanup, _ := wireApp(&bc)   // 注入 Bootstrap
defer cleanup(); app.Run()
```

**② 配置两来源（关键，service 层合并）**

- **静态算法常量** ← `conf.Bootstrap.Algo`（algo.yaml）：wire 注入 → `biz.NewRecommendUsecase(AdmissionRepo, *RankUsecase, *conf.Bootstrap)` / `NewOptimizeUsecase(*conf.Bootstrap)` 据此构造**默认 RecommendConfig / OptimizeParams / UtilityConfig（utility_weights，Q19）**（usecase 持有）。
- **每请求用户偏好** ← proto `profile`（UserProfile message：RiskAppetite/Interests/PreferredCities/PriorityDims）+ `per_category_limit`：service 层只做 **proto→biz 解析**（薄 service，不合并），把 profile 传给 usecase；**合并（用请求值覆盖 yaml 默认）在 usecase 内部完成**（Q20/DR3）——如 OptimizeUsecase 内 profile.RiskAppetite 经 RatioFromRisk 覆盖默认 TargetRatio。调 `RecommendUsecase.EstimateProb(query, profile)` / `OptimizeUsecase.OptimizeWishlist(candidates, profile)`。
- 原则：**算法常量来自 yaml（可调、不改代码）；用户偏好来自每请求 profile**。

**③ 依赖注入（wire）**

kratos 正统 + wire（技术栈已含 Google Wire v0.6.0）：`data` 实现 Repo → 注入 `biz` usecase → 注入 `service`。

```go
// cmd/algo/wire.go
//go:build wireinject
func wireApp(*conf.Bootstrap) (*kratos.App, func(), error) {
    panic(wire.Build(
        data.NewData,             // func(*conf.Bootstrap) → 读 bc.Data.Database.Dsn 建 pgxpool
        data.NewAdmissionRepo,    // 返回 biz.AdmissionRepo
        data.NewRankRepo,         // 返回 biz.RankRepo
        biz.NewRankUsecase,       // func(RankRepo) → *RankUsecase（承接 RankConvert；被 RecommendUsecase 复用，DZ4）
        biz.NewRecommendUsecase,  // func(AdmissionRepo, *RankUsecase, *conf.Bootstrap) → 从 bc.Algo 构造默认 RecommendConfig（承接 EstimateProb + CheckConstraints）
        biz.NewOptimizeUsecase,   // func(*conf.Bootstrap) → 从 bc.Algo 构造默认 OptimizeParams（承接 OptimizeWishlist）
        service.NewAlgo,          // func(*RankUsecase, *RecommendUsecase, *OptimizeUsecase) → 持有各 usecase，实现 algov1.AlgoServer
        newApp,                   // func(*conf.Bootstrap, *service.Service, log.Logger) → *kratos.App（建 gRPC server + RegisterAlgoServer，见下方定义）
    ))
}
```

```go
// internal/algo/service/algo.go
type Service struct {
    algov1.UnimplementedAlgoServer
    rank      *biz.RankUsecase
    recommend *biz.RecommendUsecase
    optimize  *biz.OptimizeUsecase
}
func NewAlgo(rank *biz.RankUsecase, recommend *biz.RecommendUsecase, optimize *biz.OptimizeUsecase) *Service { ... }
```

```go
// cmd/algo/main.go —— newApp：kratos 启动 glue（wire.Build 的最后一个 provider，G3 补齐定义）
// 职责：读 bc.Server.Grpc 建 gRPC server → 注册 AlgoServer → 组装 *kratos.App。
// 旧骨架（硬编码 :9002、调作废的 algo.NewService()）弃用，改为从 bc 读 addr/timeout。
func newApp(bc *conf.Bootstrap, svc *service.Service, logger log.Logger) *kratos.App {
    // ⚠️ bc.Server.Grpc.Timeout 是 string（如 "30s"），而 grpc.Timeout 需 time.Duration；
    //    必须 time.ParseDuration 解析 + 兜底（解析失败/≤0 回退 30s），不能直接把 string 传进去（实现期实测踩坑）。
    grpcCfg := bc.GetServer().GetGrpc()
    addr := grpcCfg.GetAddr(); if addr == "" { addr = "0.0.0.0:9002" }
    timeout, err := time.ParseDuration(grpcCfg.GetTimeout()); if err != nil || timeout <= 0 { timeout = 30 * time.Second }
    gs := grpc.NewServer(
        grpc.Timeout(timeout),
        grpc.Address(addr),  // 如 0.0.0.0:9002
    )
    algov1.RegisterAlgoServer(gs, svc)  // svc 实现 algov1.AlgoServer（4 个 RPC）
    return kratos.New(
        kratos.Name("svc-algo"),
        kratos.Server(gs),
        kratos.Logger(logger),
    )
}
// wire 依赖链：data.NewData → biz.New*Usecase → service.NewAlgo(*Service) → newApp(*kratos.App)；
// logger 由 wire 提供（main.go 内 log.NewHelper / log.With(logger,"caller",...) 作为 provider 注入）。
```

```go
// internal/algo/biz/ —— 三个 usecase 的 struct + 构造 + 方法签名（H4/Q34：编排落点的唯一权威规格）
// 构造函数由 wire 注入（provider 集见 §4.1③）；方法吃 §2 biz struct、返回 §2 struct，签名里不出现 pb（biz 不 import pb）。

// RankUsecase 承接 RankConvert；被 RecommendUsecase 复用做分数→百分位换算（DZ4）
type RankUsecase struct { repo RankRepo }
func NewRankUsecase(repo RankRepo) *RankUsecase
// Convert：LoadScoreRankMap(报考年) → ConvertByScore/RankToPercentile → RankResult（表缺失报错，DY5）
func (u *RankUsecase) Convert(ctx context.Context, q RankQuery) (RankResult, error)

// RecommendUsecase 承接 EstimateProb + CheckConstraints；持 conf 构造的默认 RecommendConfig
type RecommendUsecase struct {
    admission  AdmissionRepo
    rank       *RankUsecase
    defaultCfg RecommendConfig   // ← conf.Bootstrap.Algo（NewRecommendUsecase 内构造）
}
func NewRecommendUsecase(admission AdmissionRepo, rank *RankUsecase, bc *conf.Bootstrap) *RecommendUsecase
// EstimateProb：rank→LoadCandidates→CheckConstraints→Repo 取 historyMap/planMap→prob.RecommendCandidates（§4.2②）
func (u *RecommendUsecase) EstimateProb(ctx context.Context, q UserQuery, profile UserProfile) (RecommendResult, error)
// CheckConstraints：委托 constraint 纯函数，按 GroupKey 回填 prob（§4.2③）
func (u *RecommendUsecase) CheckConstraints(ctx context.Context, subjects []string, category string, candidates []CandidateWithProb) ([]CandidateWithProb, []RejectedCandidate, error)

// OptimizeUsecase 承接 OptimizeWishlist；持 conf 构造的默认 OptimizeParams + UtilityConfig
type OptimizeUsecase struct {
    defaultProps OptimizeParams  // ← conf.Bootstrap.Algo
    defaultUtil  UtilityConfig   // ← conf.Bootstrap.Algo.utility_weights（utility 只在 OptimizeWishlist 用，Q19）
}
func NewOptimizeUsecase(bc *conf.Bootstrap) *OptimizeUsecase
// OptimizeWishlist：合并 profile.RiskAppetite→RatioFromRisk 覆盖默认 TargetRatio（usecase 内，Q20/DR3）→ utility 批量 + optimize + 保底检查（§4.2④）
func (u *OptimizeUsecase) OptimizeWishlist(ctx context.Context, candidates []CandidateWithProb, profile UserProfile) (WishlistResult, error)
```

**④ RPC ↔ usecase 映射（Q17：一个 RPC = 一个 usecase 方法；service 只做 DTO 映射，编排全在 usecase → 收口 Q18）**

| RPC | usecase.method | 碰 Repo | 编排内容 |
|---|---|---|---|
| `RankConvert` | `RankUsecase.Convert` | RankRepo | LoadScoreRankMap + ConvertByScore/RankToPercentile |
| `EstimateProb` | `RecommendUsecase.EstimateProb` | RankRepo(经 RankUsecase)+AdmissionRepo | rank→候选→约束→概率→分档 |
| `CheckConstraints` | `RecommendUsecase.CheckConstraints` | 否（纯） | 委托 constraint.CheckConstraints（与 EstimateProb 内部同一 biz fn，DZ3） |
| `OptimizeWishlist` | `OptimizeUsecase.OptimizeWishlist` | 否（纯） | utility 批量 + optimize 配比排序 + 保底健全性检查 |

> **service 薄契约（Q18）**：每个 RPC 方法体只做三件事——① 解析/校验入参（如 `req.Year` string→int）② 调**一个** usecase 方法 ③ biz↔pb 映射（§4.3）。**禁止** service 直接调 Repo 或串多个 biz 步骤；所有编排（取数/概率/效用/保底）都在 usecase 内部。

### 4.2 RPC 方法实现

> svc-algo 实现 `api/algo/v1/algo.proto` 的 **4 个 RPC**：`RankConvert`、`EstimateProb`、`CheckConstraints`、`OptimizeWishlist`（注意 proto 用 `Reply` 后缀，非 `Response`）。
>
> **年份口径（Q16/§3.0）**：`RankConvert`/`EstimateProb` 的 `req.Year`（string）= **报考年**（演示 2025），service 层 `strconv.Atoi` 后驱动全链路（窗口=[报考年-W, 报考年-1]、score_rank/plan/候选池均按报考年）；`CheckConstraints`/`OptimizeWishlist` 不带 year（消费上游候选池）。缺失报考年数据→明确报错，不回退。

**① RankConvert：分数 → 位次**（year=报考年，见 §3.0）
```go
func (s *Service) RankConvert(ctx context.Context, req *pb.RankConvertRequest) (*pb.RankConvertReply, error) {
    // service 薄：解析入参 + 调一个 usecase 方法 + biz→pb 映射（不碰 Repo，编排全在 usecase，见 §4.1④/Q18）
    // 1. year：req.Year（string，报考年如"2025"）→ strconv.Atoi；空/非法 → 报错
    // 2. res, err := s.rank.Convert(ctx, biz.RankQuery{Province, Category, Year, Score})
    //      └ RankUsecase.Convert 内部：RankRepo.LoadScoreRankMap(报考年) → 完整一分一段表（位次绝对、不分 batch）+ controlScore（报考年本科线）；
    //        表缺失→明确报错"报考年 {year} 一分一段表未入库"不回退（DY5）；rank.ConvertByScore(score,table,controlScore)：score<本科线→报错；
    //        percentile 分母=表内最低分 cumulative（全省总人数）→ biz.RankResult{Rank,Percentile,TotalCount,ControlScore}
    // 3. biz→pb 映射 res → RankConvertReply
}
```

**② EstimateProb：录取概率（重构后 = 候选池推荐）**（year=报考年，一切年份由此推导，见 §3.0）
```go
func (s *Service) EstimateProb(ctx context.Context, req *pb.EstimateProbRequest) (*pb.EstimateProbReply, error) {
    // service 薄：解析 year/profile + 调 s.recommend.EstimateProb + biz→pb 映射（编排全在 usecase，见 §4.1④/Q18）
    // 1. year：req.Year（string，报考年如"2025"）→ strconv.Atoi；窗口 W ← conf.Algo.WindowYears（默认3）
    // 2. res, err := s.recommend.EstimateProb(ctx, biz.UserQuery{...}, profile)  // profile=biz.UserProfile；usecase 内部合并 per_category_limit 等默认（薄 service 不合并，Q20/DR3）
    //      └ RecommendUsecase.EstimateProb 内部编排（year=报考年驱动全年份口径，§3.0）：
    //        a. 复用注入的 *RankUsecase.Convert(报考年) → 用户百分位（一分一段表缺失→报错，DY5）
    //        b. AdmissionRepo.LoadCandidates(报考年)——报考年 enrollment_plan 覆盖的组、统计取报考年-1（§3.0/DY4；不用废弃 admission_score 空表）
    //        c. constraint.CheckConstraints 硬过滤（选科/批次）
    //        d. usecase 调 AdmissionRepo.BatchGroupHistory(报考年,W)→窗口[报考年-W,报考年-1] 得 historyMap、
    //           BatchGroupPlan(报考年,报考年-1) 得 planMap；⚠️窗口绝不含报考年(2025=验证holdout)
    //           → 把 kept + 用户百分位 + historyMap + planMap + config 注入纯函数 prob.RecommendCandidates（批量算概率+分档，不碰 SQL）
    //        → biz.RecommendResult（冲/稳/保 三个 []biz.CandidateWithProb + AuditBasis）
    // 3. biz→pb 映射组装 EstimateProbReply：rush/stable/safe_candidates（repeated CandidateWithProb）
    //      + rush/stable/safe_total + basis（AuditBasis：用户位次/百分位、SampleYears=[报考年-W,报考年-1]、method=weighted_hit）
}
```

**③ CheckConstraints：硬规则裁决**
```go
func (s *Service) CheckConstraints(ctx context.Context, req *pb.CheckConstraintsRequest) (*pb.CheckConstraintsReply, error) {
    // service 薄：pb→biz 映射 + 调 s.recommend.CheckConstraints + biz→pb 映射（纯裁决，不碰 Repo，见 §4.1④/DZ3）
    // 1. pb→biz 映射：req.Subjects([]string)、req.Category(string)、req.Candidates（repeated CandidateWithProb）→ biz 领域 struct
    // 2. kept, rejected, err := s.recommend.CheckConstraints(ctx, req.Subjects, req.Category, candidates)
    //      └ RecommendUsecase.CheckConstraints(ctx, subjects []string, category string, candidates []CandidateWithProb)
    //          → (kept []CandidateWithProb, rejected []RejectedCandidate)：
    //        a. 从入参 []CandidateWithProb 抽出 []Candidate，构造 UserQuery{Subjects: subjects, Category: category}
    //        b. 委托 constraint.CheckConstraints(query, []Candidate) 纯函数（与 EstimateProb step c 同一 biz fn，DZ3）
    //           → kept []Candidate + rejected []RejectedCandidate（rejected 项 prob 为零值）
    //        c. 按 GroupKey{UniversityCode,MajorGroup} 从入参池回填 kept/rejected 的 Probability/Category（选科不依赖 prob，回填仅为契约形状完整）
    // 3. biz→pb 映射组装 CheckConstraintsReply：kept（repeated CandidateWithProb）+ rejected（repeated RejectedCandidate）
}
```

**④ OptimizeWishlist：成品志愿表（消费上游候选池，不重跑取数/概率）**
```go
func (s *Service) OptimizeWishlist(ctx context.Context, req *pb.OptimizeWishlistRequest) (*pb.OptimizeWishlistReply, error) {
    // service 薄：pb→biz 映射 + 调 s.optimize.OptimizeWishlist + biz→pb 映射（编排全在 usecase，不碰 Repo，见 §4.1④/Q18）
    // 输入契约：req.Candidates（repeated CandidateWithProb）= 上游 EstimateProb 产出的分档候选池
    //   （rush/stable/safe 三档合并，每项自带 prob.category），或用户增删改后回传的同一结构。
    //   ⚠️ 本 RPC 不做 rank/prob（请求无 score/year/province/category/subjects）——概率已在 candidates 内。
    // 1. pb→biz 映射：req.Candidates → []biz.CandidateWithProb（已含 Probability/Category）；req.Profile → biz.UserProfile
    // 2. res, err := s.optimize.OptimizeWishlist(ctx, candidates, profile)  // 不传 mergedParams：合并归 usecase（Q20/DR3）
    //      └ OptimizeUsecase.OptimizeWishlist 内部编排：
    //        a. 边界：item 缺 prob（池外新增）→ 报错"候选无概率，请先经 EstimateProb"（MVP 局限，DA4）
    //        b. utility.CalculateUtilityBatch(profile, candidates, utilCfg) 一次性批量打分（当批 min-max 归一化）→ []biz.CandidateWithScore
    //        c. 构造 OptimizeParams（默认←conf.Algo；profile.RiskAppetite 非空→RatioFromRisk(risk) 覆盖默认 TargetRatio，§3.5/§4.1②/Q20）；optimize 冲稳保配比+排序 → biz.WishlistResult
    //        d. 保底健全性检查（解析式滑档率 Π(1−pᵢ) + 保底达标，复用 candidates 已有 Probability）→ 回填 OptimizationReport
    // 3. biz→pb 映射组装 OptimizeWishlistReply.wishlist（WishlistResult）
}
```

**数据流（两个 RPC、两次调用；biz↔pb 映射在 service 层，CandidateWithProb→CandidateWithScore 组装在 OptimizeWishlist step 2）**：

```
[EstimateProb RPC]（有 score/year/province/category/subjects）
  score_rank_map ─→ rank ─→ 用户百分位
  major_admission ─(usecase 经 Repo 取 historyMap/planMap 注入)→ prob.RecommendCandidates ─→ biz.RecommendResult（冲/稳/保 三档 []CandidateWithProb）
                              └─→ biz→pb 映射，返回 rush/stable/safe_candidates（repeated CandidateWithProb）+ basis
                              │
        orchestrator 合并 3 档为单数组 candidates（repeated CandidateWithProb，用户可增删改后回传）
                              ↓
[OptimizeWishlist RPC]（消费 candidates，不重跑 rank/prob）
  req.Candidates ─→ pb→biz 映射 ─→ []biz.CandidateWithProb（已含 Probability/Category）
                              │
                    utility.CalculateUtilityBatch（批量，req.Profile）
                              ↓
                    []biz.CandidateWithScore（= CandidateWithProb + UtilityScore）
                              ↓
                    optimize.OptimizeWishlist ─→ biz.WishlistResult ─→ biz→pb ─→ reply.wishlist
```

> ✅ **algo.proto 契约（Q15：类型化 message，非 *_json 字符串）**：
> - `track` → `category`（对齐 DB `score_rank_map.category`；`track` 只在废弃的 admission_score 空表中）
> - 全部跨服务载荷用类型化 message（共 12 个，定义在 algo.proto）：`UserProfile`/`Candidate`/`MajorDetail`/`AdmissionRecord`/`ProbEstimate`/`CandidateWithProb`/`UtilityScore`/`CandidateWithScore`/`OptimizationReport`/`WishlistResult`/`AuditBasis`/`RejectedCandidate`
> - `EstimateProbRequest.profile`（UserProfile）；`EstimateProbReply`：`rush/stable/safe_candidates`（repeated CandidateWithProb）+ `*_total` + `basis`（AuditBasis）
> - `CheckConstraints`：`.profile`/`.candidates` → `.kept`（repeated CandidateWithProb）/`.rejected`（repeated RejectedCandidate）
> - `OptimizeWishlistRequest.candidates`（repeated CandidateWithProb）/`.profile`；`Reply.wishlist`（WishlistResult）
> - `RankConvertReply` 含 `percentile`（与概率计算口径一致）
> - pb message = wire DTO；§2 biz 领域 struct 与之分离，service 层映射（§4.3）。改 proto 跑 `make api` 重生成 Go+Python，勿手改 `*.pb.go`（gitignore）

### 4.3 biz↔pb 映射约定（Q15：类型化契约）

**为什么分两层**：pb message（algo.proto 生成）是 **wire DTO**——跨 Go/Python 网线、snake_case、嵌套组合、带 protobuf 包袱（getter/state）；§2 的 biz 领域 struct 是 **算法内部模型**——Go 嵌入扁平化（`cwp.Probability` 直接可用）、纯函数友好。两者字段一一对应但**形状不同**，由 **service 层**（薄）做映射，biz 层永不 import pb。

**映射规则**：

| biz 领域 struct（§2） | pb message（algo.proto） | 形状差异 |
|---|---|---|
| `UserProfile` | `UserProfile` | 字段一致（PascalCase↔snake_case） |
| `Candidate`（含 `[]MajorDetail`/`[]AdmissionRecord`） | `Candidate` | 一致 |
| `CandidateWithProb{Candidate; ProbEstimate}`（**嵌入扁平**） | `CandidateWithProb{candidate; prob}`（**嵌套**） | 扁平↔嵌套：`cwp.Probability` ↔ `cwp.Prob.Probability` |
| `CandidateWithScore{CandidateWithProb; UtilityScore; FinalRank}` | `CandidateWithScore{cwp; utility; final_rank}` | 三层嵌入↔三级嵌套 |
| `WishlistResult{Candidates; Report}` | `WishlistResult{candidates; report}` | 一致 |
| `OptimizationReport` / `AuditBasis` | 同名 | 一致 |
| `RejectedCandidate{CandidateWithProb; Rule; Reason}` | `RejectedCandidate{candidate; rule; reason}` | 嵌入↔嵌套 |

**实现约定**：
- service 层提供 `toPb*` / `fromPb*` 映射函数（每类型一对），集中在 `internal/algo/service/dto.go`（H5/Q35：归 service 层，与 §1.1 目录树一致；biz 不 import pb）。
- 映射是**纯机械转换**（无业务逻辑），可单测：`fromPb(toPb(x)) == x`（round-trip 保真）。
- biz 层（rank/prob/optimize/constraint/utility）**只认 §2 struct**，签名里不出现 `pb.`/`algov1.`。
- Python(orchestrator) 侧：`make api-py` 生成 `services/orchestrator/pb/api/<服务>/v1/*_pb2.py`，统一从 `api.<服务>.v1` 导入。Makefile 设置 orchestrator 与 pb 两个根路径，wheel 包含 api 命名空间；直接用类型化 message，给 LLM 喂文本时才用 `MessageToDict`/`MessageToJson` 转 JSON。

### 4.4 错误模型（Q21：Kratos errors + 分层，biz 框架无关）

**分层原则（DE4）**：biz 纯函数返回**框架无关的领域 sentinel**（stdlib `errors.New`，定义在 `internal/pkg/errors`（package `errs`））；usecase 透传/包装；**service 层统一 `toKratosErr(err)` 映射为 Kratos `errors.New(code, reason, message)`**（自动转 gRPC status）。biz 层（rank/prob/optimize/constraint/utility）**不 import Kratos errors**，保持纯净（与 §4.3“biz 不 import pb”同理）。

**错误码表（DE2：reason 用稳定枚举，上层按 reason 分支、不靠 message 文案）**：

| reason（稳定枚举） | gRPC status | code | 触发场景 | 可见性 | 领域 sentinel（errs） |
|---|---|---|---|---|---|
| `INVALID_YEAR` | InvalidArgument | 400 | req.Year 空/非数字 | 用户 | —（service 直接构造） |
| `SCORE_BELOW_CONTROL_LINE` | FailedPrecondition | 400 | 分数 < 报考年本科线 | 用户 | `ErrBelowControlLine` |
| `SCORE_ABOVE_MAX` | InvalidArgument | 400 | 分数 > 表内最高分(750)（Q23/DG1-A） | 用户 | `ErrScoreAboveMax` |
| `CANDIDATE_WITHOUT_PROB` | FailedPrecondition | 400 | OptimizeWishlist 池外候选无 Probability（DA4） | 用户 | `ErrCandidateWithoutProb` |
| `MISSING_SCORE_RANK_MAP` | Internal | 500 | 报考年一分一段表未入库（DY5） | 内部 | `ErrMissingScoreRankMap` |
| `NO_RELIABLE_DATA` | Internal | 500 | 本科线/位次/分母不可靠 | 内部 | `ErrNoReliableData` |
| `INVALID_QUERY` | InvalidArgument | 400 | 非湖南、非法科类/年份范围/批次/非有限分数 | 用户 | `ErrInvalidQuery` |
| `INVALID_SUBJECTS` | InvalidArgument | 400 | 非法 3+1+2 选科或科类冲突 | 用户 | `ErrInvalidSubjects` |
| `INVALID_CANDIDATE` | InvalidArgument | 400 | 缺组主键、未知分档 | 用户 | `ErrInvalidCandidate` |
| `DUPLICATE_CANDIDATE` | InvalidArgument | 400 | 输入候选组重复 | 用户 | `ErrDuplicateCandidate` |
| `INVALID_PROBABILITY` | InvalidArgument | 400 | 非有限概率或正数不在 [0.01,0.99] | 用户 | `ErrInvalidProbability` |
| `INVALID_CONFIG` | Internal | 500 | 合并默认值后的算法配置非法 | 内部 | `ErrInvalidConfig` |
| `MISSING_ENROLLMENT_PLAN` | Internal | 500 | 报考年本省本类本批计划整批缺失 | 内部 | `ErrMissingEnrollmentPlan` |
| `CANCELED` | Canceled | 499 | 请求取消 | 调用方 | `context.Canceled` |
| `DEADLINE_EXCEEDED` | DeadlineExceeded | 504 | 请求超时 | 调用方 | `context.DeadlineExceeded` |
| `NOT_IMPLEMENTED` | Unimplemented | 501 | 未实现接口 | 调用方 | `ErrNotImplemented` |
| `INTERNAL` | Internal | 500 | 未分类异常；不返回底层错误原文 | 内部 | 其他错误 |

**Phase 6 边界补充**：零/负有限概率继续映射 `CANDIDATE_WITHOUT_PROB`；空约束/优化输入可返回空结果。配置零值仍表示沿用默认值（proto3 无 presence），不能用显式 0 关闭计划修正等功能。启动在连接数据库前验证算法配置；连接错误不暴露 DSN。历史审计回填实际参与计算的原始 rank/total_count，计划修正只影响计算副本。缺年份按记录下标衰减，不改为自然年差。候选源中组主键为空或同组院校 ID/名称冲突时保守排除，等待离线数据治理；不任取一条身份。

> **实现说明（bufconn 回归已通过，见 dev-log §15）**：Kratos 默认将 HTTP 400 转为 `InvalidArgument`。`toKratosErr` 仅对表中两类前置条件错误返回本地 `preconditionError` 包装：底层仍为 Kratos HTTP 400，经 `GRPCStatus()` 在状态副本上覆盖为目标码，保留 message、ErrorInfo.reason/metadata；`Unwrap()` 保持 `errors.As/Is` 与 `kerrors.Code/Reason` 兼容。其余错误沿用默认转换，不修改全局转换器。

**SafeGuardOK=false 是告警非 error（DE3）**：保底不足**不返回 error**——OptimizeWishlist 正常返回 `WishlistResult`，`OptimizationReport.SafeGuardOK=false` + 原因，由 orchestrator/前端告警。这是“合法且必须被明确表达的结论，不是异常”（见 §3.5）。MVP 不设“保底严重不足直接拒绝”的硬红线。

**落地（Phase 5）**：扩充 `internal/pkg/errors`（补 `ErrBelowControlLine`/`ErrScoreAboveMax`/`ErrCandidateWithoutProb`/`ErrMissingScoreRankMap` 领域 sentinel，保留现有 `ErrNoReliableData`/`ErrBudgetExceeded`）；service 层加 `toKratosErr(err)`（`errors.Is` 判 sentinel → 映射对应 reason/code）。

---

## 五、实现顺序与里程碑

### Phase 1: 分层骨架 + 基础（3 天）
- [x] 建 biz/data/service 三层目录 + wire 骨架（cmd/algo/wire.go、wire_gen.go）
- [x] 建 conf/conf.proto（Bootstrap：Server/Data/Algo 三段）+ 生成 conf.pb.go；configs/algo.yaml 补 algo 段（§4.1①）
- [x] go.mod 引入 pgx/v5、google/wire；data 层接 pgxpool
- [x] 定义 biz/types.go 领域结构 + biz/repo.go 的 AdmissionRepo/RankRepo 接口
- [x] data 层实现 RankRepo.LoadScoreRankMap（查完整一分一段表[位次绝对/不分batch] + control_score 本科线）
- [x] 实现 biz/rank.go（纯函数：分数→位次→百分位）+ RankUsecase（持 RankRepo，承接 RankConvert，见 §4.1④）
- [x] rank 单元测试（注入假 table，无需 DB）

### Phase 2: 概率估计 + data 层（4 天）
- [x] data 层实现 AdmissionRepo：LoadCandidates（按院校专业组聚合候选）、BatchGroupHistory、BatchGroupPlan（批量 SQL）
- [x] biz/rank.go：RankToPercentile（位次→百分位，跨年可比）
- [x] biz/prob.go：calculateProbability（加权命中率法）+ batchEstimateProb（注入 historyMap/planMap，含时效加权与计划修正）
- [x] biz/recommend.go：RecommendUsecase（注入 AdmissionRepo + *RankUsecase + conf）编排 EstimateProb（rank→取数→约束→算概率→分档冲/稳/保）
- [x] 单元测试：概率纯函数注入假 historyMap（覆盖 ≥3 年、1~2 年、0 年三种情况，无需 DB）；data 层用测试库跑集成测试

### Phase 3: 约束与效用（2 天）
- [x] 实现 biz/constraint.go（选科匹配纯函数）+ RecommendUsecase.CheckConstraints 方法（承接 CheckConstraints RPC，委托同一 biz fn）
- [x] 实现 biz/utility.go（效用打分）
- [x] 编写单元测试

### Phase 4: 志愿表优化（2 天）
- [x] 实现 biz/optimize.go（贪心选择 + 排序 + 保底健全性检查纯函数）+ OptimizeUsecase（持 conf，承接 OptimizeWishlist：utility 批量+optimize+保底）
- [x] 编写集成测试

### Phase 5: Service 集成（2 天）
- [x] 同步更新 algo.proto（track→category，EstimateProb 升级为候选池推荐）——已完成，Go 代码已重生成并编译通过
- [x] service/algo.go 实现 4 个 RPC（RankConvert/EstimateProb/CheckConstraints/OptimizeWishlist）：只做 DTO 映射 + 调对应 usecase 方法（一个 RPC=一个 usecase 方法，编排不入 service，见 §4.1④映射表）——RankConvert 于 Phase 1 落地；EstimateProb/CheckConstraints/OptimizeWishlist 于 Phase 5 落地（dto.go 全量 biz↔pb 映射 + AuditBasis basis 管道）
- [x] 跑通 wire（cmd/algo/wire.go → wire_gen.go），main.go 加载 conf.Bootstrap（config.New+Scan）→ wireApp(&bc) 启动（弃用旧骨架硬编码 :9002）——Phase 1 已装配，Phase 5 冒烟复验（config loaded + wireApp 建图 + DB Ping 通过）
- [x] 端到端测试——service 层集成测试 `TestIntegration_Service_FourRPCs`（真实库串 4 RPC：RankConvert→EstimateProb→CheckConstraints→OptimizeWishlist，验证 pb DTO 映射 + basis.user_rank 与 RankConvert 口径一致）

### Phase 6: 文档与优化（1 天）
- [x] 更新 algorithms.md：百分位、候选单位、历史窗口、算法边界与风险表述
- [x] 性能基线与低风险优化：请求内选科缓存、移除临时权重 slice（无跨请求缓存）
- [x] 一次完整代码审查，确认问题修复并补回归；证据见 dev-log §16

**总计：约 12 天**

---

## 六、关键决策点

### 6.1 概率计算方法
- **选择**：加权命中率法（二元命中：用户百分位 ≤ 校线百分位即"够得着"）
- **理由**：
  - 数据量少（通常只有 3 年），无法准确估计均值和标准差
  - 正态分布假设不一定成立
  - 加权命中率法简单透明，有统计学依据（频率学派）
- **已知局限（MVP 阶段有意暂不处理）**：二元命中 + 3 年窗口下，概率只取有限离散值（命中组合最多 2³ 种），同档内大量候选并列同值、区分度不足。
  - **为何暂缓**：概率主要用于冲稳保**分档**（阈值 0.2/0.5/0.8，粗粒度足够，82% 与 95% 同为"保"）；档内排序由 **utility 效用打分**兜底（optimize 按效用降序、GroupKey 升序排序），不依赖概率精度；线性修正需引入 band 超参并调参验证，MVP 阶段属过度设计。
  - **后续优化（触发条件：档内区分度不足，或"并列 99%"观感问题）**：引入基于距离的线性修正，把二元 hit 换成连续"够得着程度"（如 `clip((MinPercentile−userPercentile)/band+0.5, 0, 1)`），使同档概率连续分布（如保档 80%→99%）。
- **方法升级**：若积累更多历史数据（≥5 年），可考虑核密度估计或 Logistic 回归。

### 6.2 效用权重提取
- **方案A**：从 LLM 提取的 profile 中直接读取（推荐）
- **方案B**：用户手动配置（复杂度高，不推荐）

---

## 七、风险与应对

| 风险 | 影响 | 应对 |
|------|------|------|
| 历史数据不足 | 无法计算概率 | 明确标注"依据不足"，不推荐 |
| 新高考第一年 | 历史数据不可比 | 降低该候选权重，标注"数据不足，仅供参考" |
| 某校大幅扩招/缩招 | 录取线波动大 | 使用计划修正因子，并标注"波动大，仅供参考" |
| 计算性能瓶颈 | 响应时间长 | 缓存历史数据，批量计算 |
| 保底不足（压线考生） | 候选池里凑不出足够稳的保底，有滑档风险 | SafeGuardOK=false 时告警；提示用户接受风险或降低批次预期 |

---

## 八、验收标准

- [x] 核心算法有默认单测，biz/service 包覆盖率分别 ≥80%；data 真实库覆盖率单独报告，不代表每函数均达此比例
- [x] algo 端到端：真实库 Go/Python TCP 四 RPC，临时配置生效；数量上限、唯一性、排名、审计、错误详情有明确断言
- [x] 边界测试：分数/年份/缺数据、两科类、选科/概率/配置、空池/重复组/保底告警、并发/取消/超时；三轮 race/shuffle 与两项 fuzz 通过
- [x] 性能测试：固定本地数据单并发100请求均 <2秒（包含回环 TCP），另记录8并发100请求；首次应用请求不称为数据库冷缓存
- [x] 一次独立代码审查已完成，确认问题修复并回归；未做第二次独立复审，TODO 按未完成业务能力保留，不靠删标签宣称完成
- [x] Python 离线逻辑、全 API 重新生成、全仓 build/vet/test、wheel 构建及隔离目录导入通过
- [x] Docker 引擎及原生 Compose 配置校验：OrbStack Linux/arm64 环境通过
- [x] Docker 镜像构建及容器冒烟：用户授权 DaoCloud，四个 Linux/arm64 镜像构建通过；algo 容器四 RPC、Python 镜像12项离线测试及其余服务启动检查通过，详见 dev-log §19；未启动整栈或验证 amd64/GPU

验收环境、覆盖率、性能实测与修复清单见 [dev-log §17–19](dev-log.md)，接手入口见 [开发指南](development-guide.md)。通过范围仅为 algo 和全仓已有实现检查，不包含 gateway/LLM gateway/完整 LangGraph 业务闭环，也不代表真实录取准确率评测通过。

---

**Phase 1–6 实现与审查收口记录见 dev-log；最终系统验收状态以 §八与开发指南为准。**
