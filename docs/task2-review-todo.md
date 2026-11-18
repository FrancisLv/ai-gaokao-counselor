# Task 2 算法层审查 · 问题清单与结论

> 对 `docs/task2-implementation-plan.md` 逐项审查发现的问题，按优先级排序，逐个讨论确认。
> 每确认一个，就把结论写回本文件 + 同步改 task2 文档。（原建于 `.qoder/` 被 IDE 清理，迁移到 `docs/` 持久保存。）

## 进度总览

> ✅ **全部收口**：Q1–Q35 已逐项讨论确认并落地（task2-implementation-plan.md 同步改）。算法口径（Q1–Q11）+ 开发落地缺口（Q12–Q24）+ 二次开发视角审查（Q25–Q30）+ 三次审查·prob/usecase 层签名收口（Q31–Q35）四层审查均完成。

- ✅ Q1 PerCategoryLimit 未使用 —— 已确认 + 改文档
- ✅ Q2 数据库操作放哪层 —— 已确认 kratos 三层 + wire + 改文档
- ✅ Q3 概率估计统计学依据 —— 已确认（只用加权命中率/删CI/二元粗粒度）+ 改文档
- ✅ Q4 checkSafeGuard safePool + MinSafeCount —— 已确认（MinSafeCount复活/双阈值具名/Step2去约束）+ 改文档
- ✅ Q5 utility 打分维度 —— 已确认（关键词包含/砍相邻省三档/删CareerGoal/加Province/min-max/统一key）+ 改文档
- ✅ Q6 Step1 SQL 字段缺口 + SchoolTier 推导 —— 已确认（SQL补5列/deriveSchoolTier纯函数/PreferredTiers调权）+ 改文档
- ✅ Q7 选科匹配用哪个 subject_req + 解析算法 —— 已确认（用major_admission文本/四类解析/match-any/空值放过/未知剔除）+ 改文档
- ✅ Q8 controlScore（批次线）来源 —— 已确认（score_rank_map.control_score/MVP固定本科批/RankConvert无需batch）+ 改文档
- ✅ Q9 Candidate.Majors 组装逻辑 —— 已确认（data层两SQL按GroupKey合并/SubjectReq去重回填/LEFT JOIN+NULL容错）+ 改文档
- ✅ Q10 选科过滤职责重叠 —— 已确认（唯一归CheckConstraints/编排显式插入/流程图独立小框/LoadCandidates不加userSubjects）+ 改文档
- ✅ Q11 示例数字链条不一致 —— 已确认（【真bug】totalCount口径190233→313685/载完整表；【展示】重写示例3543+概率修复倒置+PerCategoryLimit）+ 改文档
- ✅ Q12 配置加载来源未说明 —— 已确认（A1 外部化 conf.Bootstrap{Server/Data/Algo}+两来源合并+顺手改 proto）+ 改文档

### 第二轮 · 开发落地视角（Q13–Q24，按 P0/P1/P2 分级）

- ✅ Q13🔴 DB schema 源 —— 已确认（删 0001_init.sql / init_db→00_schema+平铺 01~06 / 留 school_admission / 数据 2022-24 训练、25 验证）+ 已重构 migrations/ + 修 dev-log 悬挂引用
- ✅ Q14🔴 OptimizeWishlist 输入契约 —— 已确认（DA1 消费 candidates_json 不重跑 / DA2 三档合并单数组带 Category / DA3 两次调用模型 / DA4 池外无概率报错）+ §4.2④/数据流图/§3.6/proto 已改
- ✅ Q15🔴 跨服务 *_json 契约 —— 已确认（ND1 pb=DTO/§2 biz struct 保留/service 映射；ND2 嵌套组合；ND3 12 message 替换 9 处 *_json；ND4 make api 重生成）+ algo.proto 重写 + task2 §1.2/§2/§3.6/§4.2/§4.3 已改
- ✅ Q16🔴 基准年 resolution —— 已确认（year=报考年请求驱动·无 base_year config / 窗口=[year-W,year-1] 不含报考年 / score_rank·plan·候选池均按报考年 / 候选SQL 去硬编码 2024+EXISTS 报考年计划 / 窗口加上界防 2025 验证泄露 / 缺失报错不回退）+ task2 §3.0 新增·§2·§3.x·§4.2·algo.proto 已改
- ✅ Q17🟠 RankUsecase wire 缺失 + 4 RPC↔usecase 映射不明 —— 已确认（DZ1 建 RankUsecase持RankRepo / DZ2 一RPC=一usecase方法映射表 / DZ3 CheckConstraints归RecommendUsecase / DZ4 RecommendUsecase注入*RankUsecase复用 / DZ5 wire.Build补NewRankUsecase / DZ6 文档biz-data-service为准·实现阶段重写骨架）+ task2 §1.1/§1.2/§4.1③④/§4.2/里程碑已改
- ✅ Q18🟠 service“薄”与 §4.2 重编排职责矛盾 —— 已确认（与 Q17 同源收口：service 只做「解析入参+调一个 usecase 方法+biz↔pb 映射」三件事，禁直接调 Repo/串多 biz 步骤；全部编排移进 usecase）+ task2 §4.1④薄契约/§4.2 四 RPC 伪代码已改写
- ✅ Q19🟠 UtilityConfig.DefaultWeights 来源未纳入 conf —— 已确认（DX1 并入 conf.Algo·补齐 Q12 遗漏 / DX2 嵌套 message UtilityWeights{tier,major,location,strength}=字段12·与 Ratio 风格一致 / DX3 OptimizeUsecase 持有 / DX4 不强制Σ=1·缺失兑底0.25）+ task2 §3.4/§4.1①②已改
- ✅ Q20🟠 RiskAppetite→TargetRatio 映射位置未定 —— 已确认（DR1 删 OptimizeParams.RiskAppetite 只留 TargetRatio / DR2 biz 纯函数 RatioFromRisk / DR3 转换+合并归 OptimizeUsecase 内部·service 薄 / DR4 MVP 不暴露裸 TargetRatio）+ task2 §3.5/§3.6/§4.1②/§4.2②④已改
- ✅ Q21🟡 错误模型（错误码/gRPC status）未定 —— 已确认（DE1 Kratos errors / DE2 错误码表 reason 稳定枚举·用户可见 vs 内部分类 / DE3 SafeGuardOK=false 是告警非 error / DE4 biz 返回框架无关 sentinel·service 层 toKratosErr 映射）+ task2 新增 §4.4·§3.5 告警语义已改
- ✅ Q22🟡 MinSafeCount：yaml 固定值 vs 派生公式 —— 已确认（DF1-A yaml 存绝对值 min_safe_count=9·公式降为推导说明·零 schema 改动 / DF2 预留未来 L 请求级覆盖时切比例派生 / DF3 OptimizeParams.MinSafeCount 保留·默认由 conf 注入）；实测 L config-only（请求/UserProfile 无 L 字段）→运行时无“重算 vs 用9”歧义 + task2 §3.5/§4.1①已改
- ✅ Q23🟡 ConvertByScore 边界：上界处理 + float→int 匹配 —— 已确认（DG1-A 上界 s>750 报 error SCORE_ABOVE_MAX·与下界对称·严禁外推 / DG2-A 入口 int(math.Round(score)) 一次性归整）；实测厘清表用 score_range 区间匹配（顶段 694-750 无独立行、被 score=693 行 '693-750' 覆盖）+ task2 §3.1/§4.4 已改
- ✅ Q24🟡 并行计算 vs 可复现（结果顺序确定性）—— 已确认（DH1-A MVP 单线程·并行降级 M2+ 按需且须 indexed slice 保序 / DH2-A 排序全序比较器 utility DESC + GroupKey ASC tie-break / DH3 禁 map 迭代产序）；实测瓶颈是 DB N+1（已批量 SQL 解决）、内存循环无需并行 + task2 §1.2/性能表/§3.5 Step2⁄4 已改

### 第三轮 · 二次开发视角审查（Q25–Q30，按 P0/P1/P2 分级）

- ✅ Q25🔴 CheckConstraints RPC 物理上做不了选科匹配 + biz/usecase 签名类型全不匹配 —— 已确认（DG5-1-A proto 补 subjects+category / DG5-2 usecase 签名统一为 (ctx,subjects,category,[]CandidateWithProb)→([]CandidateWithProb,[]RejectedCandidate)·内部拆 Candidate 调纯函数再按 GroupKey 回填 prob）+ algo.proto/§3.3/§4.2③ 已改·make api 重生成 Go+Py·go build 通过
- ✅ Q26🟠 conf.proto 缺 message Server + newApp 无定义 + 无 gRPC 注册 glue（app 起不来）—— 已确认（补 message Server{GRPC{addr,timeout}} 对齐真实 algo.yaml / 补 newApp(bc,svc,logger) 定义含 grpc.NewServer+RegisterAlgoServer+kratos.New / wire.Build newApp 注签名）+ task2 §4.1①③ 已改
- ✅ Q27🟠 RankResult 字段三处不一致（编译不过）—— 已确认（§2.2 统一为 {Rank,Percentile,TotalCount,ControlScore} 四字段 / §4.2① 伪代码构造补全四字段 / proto RankConvertReply 保持只回 {rank,percentile} 薄契约）+ task2 §2.2/§4.2① 已改
- ✅ Q28🟠 Rejection vs RejectedCandidate 双类型 + 缺 Rule —— 已确认（删 Rejection·全链统一 RejectedCandidate{CandidateWithProb,Rule,Reason}·biz 纯函数直接返回 []RejectedCandidate 且 rejected 项 prob 为零值由 usecase 回填·补 Rule key 常量）+ task2 §3.3 已改
- ✅ Q29🟡 RecommendConfig.PreferTiers/ExcludeTiers 死参 —— 已确认（删除·YAGNI 同 CareerGoal·层次偏好统一走 UserProfile.PreferredTiers 在 utility 调权非硬过滤）+ task2 §3.2 已改
- ✅ Q30🟡 文档一致性小修合集（接口名/旧公式/位置/路径）—— 已确认（G7 §3.6 RecommendCandidates→EstimateProb·G7b L1074 旧公式 max(3,L×20%)→MinSafeCount·G4 RecommendResult 加交叉引用·G2 §2/§4/§3.x 标题路径 types⁄service.go→biz⁄service/algo.go）+ task2 §2/§3.x/§3.6/§4 标题已改

### 第四轮 · 三次审查·prob/usecase 层签名收口（Q31–Q35，纯文档层）

- ✅ Q31🟠 RecommendCandidates 签名与“biz 纯函数、IO 归 usecase”矛盾（孤儿/越权签名）—— 已确认（DH1-A 重定义为纯组合函数 = batchEstimateProb+categorize，签名改吃注入数据 (candidates,userPercentile,historyMap,planMap,config)→RecommendResult；Repo 调用归 RecommendUsecase.EstimateProb）+ task2 §3.2/§4.2② step d/数据流图已改
- ✅ Q32🟠 biz.RankQuery 被引用但全文无定义（编译不过）—— 已确认（DH2-A §2.1 补轻量 RankQuery{Province,Category,Year,Score}，不复用带 Rank/Percentile 回填字段的 UserQuery）+ task2 §2.1 已改
- ✅ Q33🟠 categorize() 不回填 *Total → EstimateProbReply.rush/stable/safe_total 恒为 0—— 已确认（DH3 categorize 收尾补 RushTotal/StableTotal/SafeTotal=len(各档) + TotalCandidates=入参数 + FilteredCount=淘汰数；note：MVP PerCategoryLimit=0 不截断故 *Total==len(*Candidates)）+ task2 §3.2 categorize 已改
- ✅ Q34🟡 usecase 层无一处 Go 定义（struct/构造/方法签名只散在注释与调用点）—— 已确认（DH4 §4.1③ 后新增“usecase 契约”代码块：三 struct 字段 + 构造函数完整签名 + 4 方法签名（Convert/EstimateProb/CheckConstraints/OptimizeWishlist）作为 biz↔service 边界唯一真相源）+ task2 §4.1 已改；连带§4.2③ CheckConstraints 伪代码补 err 返回
- ✅ Q35🟡 dto.go 路径与分层归属矛盾—— 已确认（DH5 §4.3 internal/algo/dto.go→internal/algo/service/dto.go·§1.1 目录树 service/ 下补 dto.go 行，归 service 层、biz 不 import pb）+ task2 §1.1/§4.3 已改

---

## ✅ Q1. PerCategoryLimit 字段定义了但没被使用（已确认）

- **问题**：`RecommendConfig.PerCategoryLimit` 与 `RecommendResult.PerCategoryLimit` 定义了，但分档是概率阈值驱动（数量不定），真正控数量的是 `optimize.OptimizeParams`（MaxWishlistSize×TargetRatio=13/18/14），导致该字段形同虚设、两处数量口径矛盾。
- **结论（方案 A）**：每档数量由 `optimize.OptimizeWishlist` 的 `OptimizeParams` 控制——`MaxWishlistSize`（L，湖南本科批=45）× `TargetRatio`（`RatioConfig` 默认 冲0.3/稳0.4/保0.3）派生 = 13/18/14（注：`RushCount/StableCount/SafeCount` 是 `OptimizationReport` 的**输出统计**字段、非 OptimizeParams 入参；原 note 误写「OptimizeParams 的 RushCount/SteadyCount/SafeCount 默认 30/45/25」已于 Q11 附带修正）。`RecommendResult.PerCategoryLimit` 改为注释「仅审计参考，实际每档数量由 optimize 控制」。service 层 step3 把三档合并为**全量候选池**交给 optimize，由它按数量+排序+约束选最终志愿表。
- **文档改动**：`RecommendResult.PerCategoryLimit` 注释、service step2-3 描述、3.6 OptimizeWishlist 开头说明。

## ✅ Q2. 数据库操作放哪一层？（已确认：kratos 正统三层 + wire）

- **问题**：`batchQueryGroupHistory`/`batchQueryGroupPlan` 直接跑 SQL，放算法层违反"纯函数、无 IO"。
- **结论**：
  - 采用 kratos 三层：`service`（proto 对接）→ `biz`（领域算法 + Repo 接口）→ `data`（实现 Repo、跑 SQL，用 pgx/v5）。
  - **DB 操作全在 data 层**；算法层（rank/prob/optimize…）归 biz，**纯函数无 IO**，历史数据由 data 查好后注入。
  - **依赖倒置**：biz 定义 `AdmissionRepo`/`RankRepo` 接口，data 实现；batchQuery* → `AdmissionRepo.BatchGroupHistory/BatchGroupPlan`。
  - **引入 wire**（技术栈已含 Google Wire v0.6.0）：`data.NewXxxRepo → biz.NewXxxUsecase → service.NewAlgo`。
  - `batchEstimateProb` 加 `historyMap`/`planMap` 参数（RecommendUsecase 调 repo 取得后传入）。
  - 旧代码骨架作废，按文档重写。
- **文档改动**：骨架作废说明、1.1 目录（biz/data/service+wire）、1.2 原则、3.2（注入 + SQL 归 data + Repo 接口契约）、4.1（wire）、Phase1-5。

---

## ✅ Q3. 概率估计的统计学依据（已确认）

- **问题**：algorithms.md（正态/KDE + CI）与 task2（加权命中率 + 无CI）矛盾；命中率二元化导致概率离散。
- **结论**：
  1. **只用加权命中率法，放弃正态/KDE**（3年小样本估不准分布），**删除 CI**（小样本 CI 伪精确，可审计性由 DataYears+Method+basis 承载）。与既有记忆决策一致。
  2. **命中率保持二元（粗粒度）**：走 A。理由——概率首要用途是分档（阈值0.2/0.5/0.8，粗粒度够）；档内区分度由 utility 兜底（optimize 按"概率×效用"排序）；线性修正属 MVP 过度设计。定性为"有意简化"而非缺陷。
  3. **连续性修正记为后续优化**（触发条件：档内区分不足/并列99%观感）；已更新记忆 ee4ee83b：从"必须做"改为"MVP暂缓、按需优化"。
  4. **无数据→不推荐**（return 0.01 被 categorize <0.2 过滤）；"依据不足"展示归前端，后端不显式标注。
  5. λ=DecayFactor=0.7、PlanAdjust=0.5（两份文档一致；上一条误说 0.8 已纠正）。
- **文档改动**：algorithms.md §3（方法表格/输出/§9 三处）、task2 §6.1（决策+已知局限+优化方向）、calculateProbability 注释、记忆 ee4ee83b 更新。
- **遗留提醒**：本决策依赖"utility 能拉开档内区分度"→ Q5 审 utility 时重点确认。

## ✅ Q4. checkSafeGuard 的 safePool 来源 + MinSafeCount 关系（已确认）

- **问题**：MinSafeCount 形同虚设（实际用公式 max(3,L×20%)）；safePool 来源未交代；保底阈值 0.9 与分档阈值 0.8 语义未区分；safeGuard++ 无条件自增在 safePool 含 0.8~0.9 候选时虚高；Step2 与 Step5 保底检查职责重叠。
- **结论**：
  1. **MinSafeCount 复活**：`minSafe = params.MinSafeCount`，默认 `max(3,⌈L×20%⌉)`，上游 orchestrator 可按风险偏好覆盖；算法层只提供可配参数+默认，覆盖策略在上层。
  2. **safePool 明确定义**：调用方在 Step5 前构造「p≥SafeGuardThreshold(0.9) 且未入选 wishlist」的稳拿池，按 p 降序。
  3. **双阈值具名**：新增常量 `SafeGuardThreshold=0.9`（稳拿保底），与分档 `SafeThreshold=0.8` 区分并注明语义。
  4. **safeGuard++ 虚高 bug 消除**：因 safePool 严格 =p≥0.9，追加即达标，无条件 ++ 不再虚高。
  5. **Step2 去掉保底约束**：Step2 只按效用+配比选 L 个，保底红线统一交 Step5 兜底（单一职责）。
  - 附带澄清（非缺陷）：c.Score 非笔误（内嵌 UtilityScore.Score 字段提升，合法）；algorithms.md §7 与 task2 一致（无矛盾）。
- **文档改动**：task2 §3.5（OptimizeParams.MinSafeCount 语义、Step2 去约束、checkSafeGuard 加 SafeGuardThreshold 常量+safePool 契约+minSafe 来源、Step5 说明）；OptimizationReport 的 SafeCount/SafeGuardOK 注释。记忆 2aa83f01 更新为决策后状态。

## ✅ Q5. utility 打分维度（已确认）

- **诊断纠正**：标题说"维度不可实现"不准确——4 个打分维度字段基本都有（SchoolTier/Location/PreferredCities/NumMaster/NumDoctor/RuankeRank）。真正卡点是**打分算法细节未定义**。
- **结论（6 个问题全部落定）**：
  1. **专业匹配用关键词包含**：任一 interest 作为子串命中任一组内 MajorName → 1.0，否则 0.0；纯函数无 IO 可测试（符合 Q2 biz 层约束），LLM 语义匹配记为后续优化。
  2. **"相邻省"档直接砍掉**（库中无省份邻接数据，不补配置表，YAGNI）：简化为 偏好城市=1.0 / 同省=0.8 / 其他=0.5。
  3. **Candidate 加 Province 字段**：SQL 从 university.province 取（候选查询已 JOIN university，增量成本近零）。
  4. **升学资源用 min-max 归一化**（当批全体候选）：0.4×minmax(NumMaster)+0.3×minmax(NumDoctor)+0.3×(1−RuankeRank/RuankeMax)；max==min 取 0.5，缺失值该项按 0 计。因此入口改为**批量函数 `CalculateUtilityBatch`**（单候选无法独立算 min-max），service 编排同步改批量调用。
  5. **维度 key 全局统一为 tier/major/location/strength**：PriorityDims/Breakdown/Weights/公式四处同口径，PriorityDims 补 strength 可选项；默认权重四维各 0.25，PriorityDims 命中×2 后归一化使 Σ=1。
  6. **CareerGoal 删除**（死参数，MVP 按 YAGNI 移除）：后续若需按职业目标调权重再加回。
- **文档改动**：task2 §2 UserProfile（PriorityDims key 统一、删 CareerGoal）/ Candidate（加 Province）、§3.4 全重写（批量签名+NormStats+四维表+权重规则）、§4.2 step3 与数据流图改批量；algorithms.md §4（维度表 5→4 维、删风险偏好行、公式改四维、补 3 条 why）。
- **闭环 Q3 遗留提醒**：utility 打分算法已定义清楚，档内区分度兜底成立。
- **关联**：SchoolTier 推导逻辑（数据库是 is_985/is_211/is_dual_class 布尔）→ Q6。

## ✅ Q6. Step1 SQL 字段缺口 + SchoolTier 推导（已确认）

- **问题**：聚合 SQL 只 SELECT `is_985/is_211/city`，缺 Candidate 所需的 `is_dual_class/province/num_master/num_doctor/ruanke_rank`（university 表 psql 实测均有）；SchoolTier 字符串推导优先级未定义。
- **结论**：
  1. **SQL 补 5 列**：聚合 SQL 的 SELECT 与 GROUP BY 同步加 `u.is_dual_class, u.province, u.num_master, u.num_doctor, u.ruanke_rank`（均函数依赖 university_id，不改变分组粒度）；专业明细 SQL 不涉这些字段，不动。
  2. **SchoolTier 推导优先级 985>211>双一流>普通**（与 utility tier 打分 1.0/0.8/0.6/0.4 排序一致；布尔重叠时优先级保证唯一标签）。
  3. **方案 B：biz 层纯函数 `deriveSchoolTier(is985,is211,isDualClass)→string`**（而非 SQL CASE）：SQL 只选三个布尔，data 层行→Candidate 映射时调用该函数；优先级规则显式在代码、可单测。
  4. **PreferredTiers 复活→调权重**：`profile.PreferredTiers` 非空 → tier 维权重 ×2（与 PriorityDims 含 tier 同一信号，封顶 ×2 不叠加）；**只调权重、不硬过滤、不改 tier 得分**（得分仍用 SchoolTier 固定映射）。
- **文档改动**：task2 §2 Candidate.SchoolTier 注释、§3.2 Step1 聚合 SQL（SELECT+GROUP BY 补 5 列）、§3.2 新增 deriveSchoolTier 函数块、§3.4 权重提取（tier 额外规则）；algorithms.md §4 补"软偏好非硬门槛"科普 why。
- **⚠️ 新发现待决（未改）**：`RecommendConfig` 已有 `PreferTiers`/`ExcludeTiers`（L319-320，prob 模块），与 `UserProfile.PreferredTiers` 可能重复/冲突，且 Step1 候选生成流程未见实际使用（疑死参）→ 待定：是否合并为单一层次偏好口径 / ExcludeTiers 是否做硬过滤。

## ✅ Q7. 选科匹配用哪个 subject_req + 解析算法（已确认）

- **问题**：`major_admission.subject_req` 是文本（"首选物理，再选不限"）、`major.subject_req` 是 jsonb；§3.3 只写"用户选科 ⊇ 要求"和一个简单集合例子，未体现湖南 3+1+2 首选/再选双层结构，解析算法空缺。
- **数据实测（psql）**：major_admission.subject_req 全库仅 **30 种 distinct**，格式统一 `首选{物理/历史/不限}，再选{...}`；再选四类：`不限`/单科/`X/Y(2选1)`/`X、Y(2科必选)` + 12 行空值。同组内 req：14156 组唯一（99.76%）、32 组有 2 个、2 组全空。
- **结论**：
  1. **数据源用 `major_admission.subject_req` 文本**（用户拍板）。
  2. **解析 `parseSubjectReq`**：按 `，` 切首选/再选；再选含`(2选1)`→`/`拆/atLeastOne、含`(2科必选)`→`、`拆/all、`不限`→none、单科→all(1门）。
  3. **匹配 `matchSubjects`**：首选不限→过否则==用户首选（SQL 已按 category 过滤，冗余保险）；再选 none→过/atLeastOne→交集非空/all→子集。
  4. **组级 match-any**：SubjectReq 为组内 distinct 列表，满足任一条即过（99.76% 组长=1，match-any≡match-all，仅对 32 混合组取宽松）。
  5. **空值/NULL → 当"不限"放过**（不因脏数据缩候选池）；**无法解析的未知格式 → 剔除并记 Rejection.Reason**（硬约束保守）。
  6. **科目名归一化 `normalizeSubject`**（政治→思想政治），与库中口径对齐。
  7. 解析/匹配均为纯函数，**统一落 constraint.go**；prob Step1 与本节两处复用同一套（唯一归属收口留 Q10）。
- **文档改动**：task2 §3.3（新增 SubjectReq/parseSubjectReq/normalizeSubject/matchSubjects 签名、检查项1 选科匹配全重写含边界）、§3.2 Step1 SQL 选科过滤注释交叉引用 §3.3。algorithms.md 不改（L200 "选科/体检/批次提前踢掉"已覆盖硬约束科普 why，解析规则属实现细节不复述）。
- **关联**：选科过滤职责重叠（prob Step1 vs constraint）→ Q10。

## ✅ Q8. controlScore（批次线）来源（已确认）

- **问题**：`ConvertByScore(score, table, controlScore)` 需批次线，但 `RankConvertRequest` 无 batch/controlScore 字段，未说明从哪取。
- **数据实测（psql）**：`score_rank_map` 本身有 `control_score` 列（smallint），按 (province,year,category,batch) 存储、**完全填充、每组恰 1 值**，= 官方批次线（2024 物理本科 422、历史本科 438、专科 200）。**一分一段表跨批次按分数完全不重叠**（所有 year/category overlap=0：本科批从本科线起、专科批到本科线−1 止）。
- **结论（MVP 固定本科批）**：
  1. **controlScore 来源 = `score_rank_map.control_score`**（batch='本科批' 的行 = 本科线），data 层查一分一段表时一并带出（同组常量），无需新表/配置/硬编码。
  2. **RankConvertRequest 不加 batch 字段**（用户洞察正确）：① MVP 内部固定本科批；② 跨批次分数不重叠，score 本身即可唯一定位行。撤回之前"加 batch 字段"的建议。
  3. **ConvertByScore 签名不变**（原拟把 control_score 塑进 ScoreRankEntry/去参的方案不再需要：固定本科批只一个分区，controlScore 来源唯一），仍为纯函数；RankRepo.LoadScoreRankMap 返回改为 `(table, controlScore int, err)`。
  4. **校验语义**：score < controlScore（本科线）→ 报错"低于本科线，MVP 仅支持本科批"。
  5. **候选生成 batch 固定本科批**（prob Step1 SQL 已是 batch='本科批'，保持）；**MVP 局限**：不支持专科批考生（低于本科线会得空/全冲结果），后续按 score 派生 batch 再扩展。
- **文档改动**：task2 §3.1（ConvertByScore 注释、新增 controlScore 来源/无batch理由/MVP局限说明块、步骤1+边界改本科线语义）、§3.2 数据来源表 score_rank_map 补 control_score、RankRepo.LoadScoreRankMap 签名改返回 controlScore、§4.2 RankConvert service 步骤、Phase1 任务。algorithms.md 不改（controlScore 属实现细节）。
- **关联**：候选 batch 由 score 派生的扩展（专科批支持）已记为 MVP 局限，如后续要做得单独开项。

## ✅ Q9. Candidate.Majors 组装逻辑缺失（已确认）

- **问题**：Step1 两条 SQL（聚合出组骨架 + 明细出专业行），但 `LoadCandidates` 只写"候选生成"，未描述两者如何按 GroupKey 合并回填 `Candidate.Majors`；连带 `Candidate.SubjectReq []string` 从哪填也未交代。
- **数据实测（psql, 2024物理本科批 17222 明细行）**：min_rank/min_score/subject_req 零 NULL（本科批范围内干净，Q7 那 12 行空值不在本科批2024）；23 行 major_id 为 NULL（LEFT JOIN 拿不到 duration/tuition）、167 行 tuition 本身 NULL。
- **结论**：
  1. **组装放 data 层 `LoadCandidates`**（符合 Q2：DB+行映射归 data）。
  2. **方案 A：两 SQL + 内存 `map[GroupKey]int` 合并**（与 batchEstimateProb 同一 key 模式）；不用 array_agg 单 SQL 嵌套（方案 B，SQL 复杂不推）。
  3. 步骤：聚合 SQL1 建 []Candidate 骨架+下标索引 → 明细 SQL2（已 ORDER BY …,min_rank）逐行构造 MajorDetail 按 GroupKey append 到 Majors（天然按专业级位次排序）。
  4. **`Candidate.SubjectReq` 从明细 DISTINCT subject_req 回填**（用 set 收集），喂给 §3.3 选科 match-any。
  5. **NULL 容错 + 保留 LEFT JOIN**：Duration NULL→""、Tuition NULL→0、MinScore/MinRank NULL→0；LEFT JOIN 不丢 23 个无 major 的专业。无明细行的组→Majors 留空不报错。
- **文档改动**：task2 §3.2（新增"候选组装"说明块，4 步合并算法+NULL容错）、AdmissionRepo.LoadCandidates 注释补充。algorithms.md 不改（纯 data 层实现细节）。

## ✅ Q10. 选科过滤职责重叠（已确认）

- **问题**：选科过滤同时出现在 §3.2 prob Step1（流程图/示例/SQL注释）与 §3.3 constraint CheckConstraints，职责重叠。
- **核心发现（比重叠更严重）**：`CheckConstraints` 全文只有定义、**无任何调用点**（孤儿函数）；recommend.go 编排原写「取数 → 算概率 → 分档」**不含硬约束步骤**。而 `LoadCandidates(ctx,province,category,batch,year)` 签名**无 userSubjects**，选科过滤不可能在 data 层做（且 biz 纯函数不该被 data 调，违反分层）。附带 stale：流程图原写「用户选科 ⊇ 组内要求」是 match-all，与 Q7 定的 match-any 矛盾。
- **结论（Option A）**：
  1. **选科过滤唯一归 `constraint.CheckConstraints`**，编排显式插入这一步：`LoadCandidates → CheckConstraints → batchEstimateProb → categorize`（过滤先于概率，不给不可报的组白算概率）。
  2. **`LoadCandidates` 签名保持不加 userSubjects**（选科过滤不在这层，只组装 Candidate.SubjectReq）。
  3. **流程图把 CheckConstraints 画成独立小框**（Step 1.5，介Step1与Step2之间）。
  4. 备选 Option B（选科过滤内联留 Step1、CheckConstraints 退化为只管批次/体检/单科）已否：会令 prob 跨模块调 constraint 解析函数、CheckConstraints 名不副实。
- **文档改动**：task2 ①recommend.go 编排注释（取数→硬约束过滤→算概率→分档）；②流程图 Step1 删选科过滤行+新增 Step 1.5 硬约束过滤独立小框；③SQL 注释 L501 改为"不在SQL/Step1做、由CheckConstraints判断"；④batchEstimateProb 编排注释补 CheckConstraints；⑤§3.3 L802"两处复用"改为"唯一落 CheckConstraints"。
- **遗留→Q11**：示例 L704「选科过滤 → 约 4000」仍在 Step1 下，数字链条（4541/4000/3500）留 Q11 统一校（结构上选科过滤已移CheckConstraints，示例需同步拆分）。
- **关联**：algorithms.md 不改（硬约束科普 why 已覆盖）。

## ✅ Q11. 示例数字链条不一致（已确认）

- **问题**：示例数字多处不统一且与实测不符。已归档关键事实（原 open-questions）：专业组数 4541→4000→~3500 多处不一；总人数示例用 190233。
- **实测核对（psql, 2024湖南物理）**：专业组数=**3543**（distinct 组，均含min_rank；2023=2305/2022=1802）；全省总人数=**313685**（一分一段表 MAX(cumulative) 跨批次）；本科批 MAX(cumulative)=190233（=本科线上人数）；650分→位次2302、580分→23180、620分→7846。
- **🔴 真逻辑 bug（非展示）：用户百分位与历史百分位分母口径不一致**。历史侧 BatchGroupHistory 子查询 `MAX(cumulative_count)` **不带 batch 过滤**→313685；用户侧 rank 模块 L244「全省总人数从表里最低分 cumulative 取」，而 Q8 「MVP 固定本科批」使 LoadScoreRankMap 只载本科批表→最低分=本科线422→190233。两侧分母 313685 vs 190233 不一致，违反 L247「口径一致」→概率被系统性低估、分档偏保守。
- **根因**：用户澄清「位次是全省总排位、与批次无关」。Q8 把 rank 表限制为本科批后，「表里最低分 cumulative」从全省总人数（313685）退化为本科线上人数（190233）。
- **修复（Q11a）**：`LoadScoreRankMap` 改为载入**完整一分一段表（不按 batch 过滤）**→表内最低分 cumulative 自然=全省总人数 313685，L244 叙述自动正确，**ConvertByScore 签名不变**（仍 table+controlScore）；controlScore（本科线）仍作标量单独返回供 MVP 批次校验。MVP固定本科批仅限：controlScore取本科线 + candidate SQL固定本科批 + score<本科线报错（不限制 rank 表）。
- **展示修复（Q11b）**：重写示例块——用户改 580分/位次23180/percentile 0.0739（全真）；专业组数 3543；补 Step 1.5 硬约束过滤（Q10）；**修复概率逻辑倒置**（原中南0.96>湘潭0.45 反了，改为随难度递减：北大0.01<中南0.18<湖大0.35<长沙理工0.72<湘潭0.88/0.92）；分档数标示意且链自3400；PerCategoryLimit=50→0全量（符合Q1）。组数统一：L473 ~3500→3543、L556 3531→3543。
- **文档改动**：task2 §3.1（L244 百分位步骤补全省总人数口径+⚠️不可只载本科批警告）、Q8 note块（LoadScoreRankMap载完整表）、RankRepo.LoadScoreRankMap 注释、§4.2 RankConvert、Phase1、§3.2 SQL注释组数、batchEstimateProb N+1 组数、完整示例块重写。algorithms.md 不改（示例属实现版细节）。
- **附带发现（已修正）**：review-todo Q1 note 原写「OptimizeParams 的 RushCount/SteadyCount/SafeCount 默认 30/45/25」与 §3.5 不符（OptimizeParams 无这三字段，那是 OptimizationReport 输出；每档数量实为 MaxWishlistSize=45 × RatioConfig 0.3/0.4/0.3 → 13/18/14）。已修正 Q1 note。
- **关联**：Q11a 是对 Q8 结论的细化（LoadScoreRankMap 载完整表而非本科批），已同步回写 Q8 note 与相关记忆。

## ✅ Q12🟢. 配置加载来源未说明（已确认 + 改文档）

- **问题**：`RecommendConfig`/`OptimizeParams` 含 DecayFactor/Threshold/RatioConfig 等配置字段，但未说明从哪加载。实查发现更严重：task2 L1031 `wireApp(*conf.Bootstrap)` 引用了 **不存在的 conf 包**（全仓无 conf/ 目录、无 conf.proto）；`configs/algo.yaml` 只有 server+database、无算法参数段；`cmd/algo/main.go` 是旧骨架（硬编码 :9002、调作废的 algo.NewService()、根本不加载配置）。
- **建议**：明确配置加载方式（Kratos 惯例：conf.proto + configs/algo.yaml + conf.Bootstrap 注入）。
- **结论（A1 外部化 + 顺手改 proto）**：✅ 采纳
- **修复（Q12）**：补全配置加载架构——①新建 `conf/conf.proto` 定义 `Bootstrap{Server,Data,Algo}`，`Algo` 段承载全部算法参数（window_years/decay_factor/plan_adjust/三阈值/safe_guard_threshold/max_wishlist_size/min_safe_count/target_ratio/per_category_limit）；②`configs/algo.yaml` 补 `algo:` 段填默认值；③`main.go` 改为 Kratos 惯例 `config.New(WithSource(file.NewSource("configs/")))`→`Scan(&bc)`→`wireApp(&bc)`（弃用旧骨架）；④wire provider 注入 `*conf.Bootstrap`：`data.NewData` 读 dsn、`biz.NewRecommendUsecase/NewOptimizeUsecase` 从 `bc.Algo` 构造默认 config；⑤明确**两来源**：算法常量←yaml（conf.Bootstrap.Algo），用户偏好←每请求 profile_json（UserProfile），service 层合并覆盖。
- **附带修复（proto）**：`per_category_limit` 注释默认 50→0=全量（对齐 Q1）；`wishlist_json` 注释 MonteCarlo→解析式 SlipRate（对齐 sim 废弃）；.proto + .pb.go 同步。
- **文档改动**：task2 §1.1（目录树加 conf/）、§1.2（加“配置驱动”原则）、§4.1（重构为“配置加载与依赖注入”：conf.proto/algo.yaml/main.go/两来源/wire provider）、RecommendConfig+OptimizeParams 注释、Phase1+Phase5 清单。algo.proto/.pb.go 2 处注释。algorithms.md 不改（配置属实现细节）。
- **备注**：conf.proto/conf.pb.go/algo.yaml algo段/main.go 重写属 Phase 实现产物（task2 已给出完整规格）；.pb.go 注释已手改同步，下次 `make api` regen 会生成相同内容。go build 通过。

---

# 第二轮 · 开发落地视角审查（Q13–Q24）

> 第一轮（Q1–Q12）对齐了**算法口径**；本轮从“真正动手写代码”视角排查**工程契约/装配/数据源**缺口。按 P0（阻塞）/P1（装配）/P2（决策边界）分级，逐条讨论确认。

## ✅ Q13🔴. DB schema 源：新旧 migration 冲突 + 数据子目录不自动加载（已确认 + 已重构 migrations/）

- **进展**：真实 schema 已落库——`migrations/init_db.sql`（13 表：university/major[含duration,tuition]/enrollment_plan[含major_group]/major_admission/school_admission[院校投档线,含control_score]/score_rank_map[category,cumulative_count,control_score,score_range,segment_count,rank_range]/charter_*/chat_session/...）+ `migrations/sql/01~06_*.sql`（真实数据，major_admission ~6万行、湖南2022-2025）。**task2 的 SQL 列与 init_db.sql 逐列核对完全对得上**。
- **问题**：① 旧 `migrations/0001_init.sql`（重构前 schema：college/major[id TEXT,无duration]/admission_score/score_rank_map[track,score,rank]/policy_*/plan_checkpoint/llm_call_log）与新 `init_db.sql` **表名重叠且列不兼容**（major/score_rank_map/plan_checkpoint/llm_call_log 两者都建）；init_db.sql 用 `CREATE TABLE`（无 IF NOT EXISTS），docker-entrypoint 按字母序先跑 0001（建了 major）再跑 init_db（`CREATE TABLE major` 报错、ON_ERROR_STOP 中止）→ fresh 环境半残库。② postgres entrypoint **不递归子目录** → `migrations/sql/` 的 6 个数据文件不会自动加载，fresh 环境有 schema 无数据。③ 字母序下 `init_db.sql`(i) 排在 `01_`~`06_`(数字) 之后 → 若扁平化会数据先于 schema。
- **影响**：fresh `docker-compose up` 起不来可用的库；task2 所有 SQL 无法在新环境验证。
- **初步建议**：删除废弃的 `0001_init.sql`；schema+data 统一数字前缀排序（如 `00_schema.sql`←init_db、`01_university.sql`…`06_score_rank_map.sql`）并全部平铺到 `migrations/` 顶层（entrypoint 才会跑），或用一个 `00_load.sh` 显式按序 psql；确认 `school_admission`（task2 未用）是否保留备用。
- **结论（D1 删 / D2 方案A / D3 留 / D4 不跑）**：✅ 已落地
  - **D1**：删除废弃的 `0001_init.sql`（重构前 schema，已被 init_db.sql 完全取代；用户已手动删除）。
  - **D2 方案A（平铺）**：`init_db.sql`→`migrations/00_schema.sql`；`migrations/sql/01~06_*.sql`→提到 `migrations/` 顶层；删除空的 `sql/` 子目录。最终布局：`00_schema.sql`+`01_university`+`02_major`+`03_enrollment_plan`+`04_major_admission`+`05_school_admission`+`06_score_rank_map`。docker-entrypoint 按字母序＝建表在前、灌数在后、FK 顺序正确（university→major→enrollment_plan/major_admission→school_admission→score_rank_map）。数据文件经核为**纯 INSERT**（无 DDL），不会与 00_schema 冲突。
  - **D3**：`school_admission` 保留（真实数据、无害，备用院校级查询/交叉校验）。
  - **D4**：不跑 psql——数据是**故意用 2022-2024 三年做训练、2025 留作验证**（面试项目 train/validation 划分）。⚠️ `00_schema.sql` 的 data_version INSERT 注释写“2022-2025”与实际训练数据(2022-2024)不符，留 Q16 一并校。
  - **附带修复**：`docs/dev-log.md` L205 悬挂引用 `migrations/0001_init.sql`（已删）→ 改为 `00_schema.sql` + `01~06` 循环导入。
  - **基准年事实（喂 Q16）**：base year=2024，WindowYears=3→2022/2023/2024（与数据文件完全吻合）；2025 为验证 holdout、不在训练数据内。

## ✅ Q14🔴. OptimizeWishlist 输入契约自相矛盾（已确认 + task2/proto 已改）

- **问题**：`OptimizeWishlistRequest` 只有 {trace, candidates_json, profile_json}，**无 score/year/province/category/subjects**；但 §4.2 ④ 伪代码 step1-2 要 `rank.ConvertByScore(score)`+`prob.RecommendCandidates(需 province/year/category/score)` → 字段不存在、无法执行。且重跑 RecommendCandidates 会**丢弃用户在 candidates_json 的增删改**（与 §3.6 ③矛盾）。又 EstimateProbReply 返 3 个 JSON、OptimizeWishlistRequest 只收 1 个，合并方式未定义。
- **初步建议**：OptimizeWishlist 改为**消费 candidates_json（已含概率）→ utility → optimize**，不重跑 rank/prob；§4.2 ④ step1-2 重写；定 candidates_json 合并格式（3 档合一 or 带 category 标记的单数组）。
- **结论（DA1-DA4 全采纳，Design A：消费而非重跑）**：✅ 已落地
  - **DA1**：OptimizeWishlist 改为**消费 candidates_json**（解析→[]CandidateWithProb，已含 Probability/Category）+ profile_json（utility 打分）→ optimize → checkSafeGuard，**不再调 rank/prob**。§4.2 ④ 伪代码 step1-2（rank/prob）已重写为 step1（解析 candidates_json）+ step2（utility），数据流图同步。
  - **DA2**：candidates_json = EstimateProbReply 的 rush/stable/safe **三档合并为单数组**（每项自带 Category，optimize Step1 的 filter(Category==冲) 直接可用）；orchestrator 负责合并。proto 单字段不动。
  - **DA3**：确认 **两次调用模型**——EstimateProb（有 score 等，做 rank/prob 出候选池）→（用户浏览/增删改）→ OptimizeWishlist（吃 candidates_json）。§3.6 ①③ 已更新体现。
  - **DA4**：池外新增（item 无 Probability）→ **报错“候选无概率，请先经 EstimateProb”**（MVP 局限，已写入 §4.2 ④ step1 边界）。
  - **文档改动**：task2 §3.6 ①③ / §4.2 ④ 伪代码 / 数据流图；algo.proto candidates_json 注释（.pb.go 同步）。字段级 JSON schema 留 Q15。

## ✅ Q15🔴. 跨服务 *_json 契约未定义（已确认：契约定义在 .proto 类型化 message）

- **问题**：proto 大量 *_json（profile_json/candidates_json/rush·stable·safe_candidates_json/wishlist_json/basis_json）无任何 JSON schema：字段命名风格、Go struct↔JSON 映射、orchestrator(Python)↔algo(Go) 序列化协议全缺 → Phase5 DTO 映射 + 集成必卡。
- **初步建议**：定一套约定（建议 snake_case + 每个 *_json 明确对应结构体/字段清单），写进 task2 或独立契约文档；Go 侧 struct 加 json tag。
- **结论（ND1-ND4 全采纳：契约定义在 .proto、类型化 message，用户主动提出）**：✅ 已落地
  - **ND1（关键）**：pb message = wire DTO；**§2 biz 领域 struct 保留**（Go 嵌入扁平化、算法友好）；**service 层做 biz↔pb 映射**（option b，符合已定 Kratos 三层）。否决 option a（biz 直接吃 pb 类型→算法变 GetXxx() 链、背 protobuf 依赖）。
  - **ND2**：proto 用**嵌套组合**（CandidateWithProb{candidate; prob}、CandidateWithScore{cwp; utility; final_rank}），非字段平铺；扁平↔嵌套差异由 service 映射消化。
  - **ND3**：algo.proto 定义 **12 个 message**（UserProfile/Candidate/MajorDetail/AdmissionRecord/ProbEstimate/CandidateWithProb/UtilityScore/CandidateWithScore/OptimizationReport/WishlistResult/**AuditBasis**/**RejectedCandidate**），**9 处 *_json 全替换为类型化字段**（rush_candidates_json→repeated CandidateWithProb rush_candidates、basis_json→AuditBasis basis、wishlist_json→WishlistResult wishlist、profile_json→UserProfile profile 等）。breaking 改名零成本（pb.go gitignore + RPC 未实现）。
  - **ND4**：改完 .proto 跑 `make api` 重生成 Go+Python；**不再手改 *.pb.go**（gitignore、由 codegen 覆盖；Q12/Q14 手改 pb.go 注释属多余）。
  - **作废的原 DB1/DB5/DB6**：snake_case json tag、Pydantic 镜像、round-trip 单测——codegen 天然保证零漂移，全部不需要。
  - **文档改动**：algo.proto 重写（+138/-19）；task2 §1.2 加“类型化契约”原则、§2 头加 biz vs DTO 说明、§2.2 补 AuditBasis/RejectedCandidate struct、§3.6/§4.2②③④/数据流图去 *_json、新增 §4.3 biz↔pb 映射约定、proto note 更新。

## ✅ Q16🔴. 基准年（今年/当年）resolution 策略缺失（P0）

- **问题**：示例硬编码 year=2024，现在 2026。UserQuery.Year/proto year 由 orchestrator 传——考试年(2026)还是最新数据年(2024/2025)？传 2026 而数据只到 2024/2025 → LoadScoreRankMap/LoadCandidates 返空 → 全链路报错。enrollment_plan“今年计划”对考试年是否存在？
- **初步建议**：定 base-year 策略（请求年 vs 库内 MAX(year) fallback；WindowYears 相对 base-year；psql 实测库内最新年）。
- **结论（DY1-DY5 全采纳；用户澄清 year=报考年，推翻我原「base_year=2024 config」初案）**：✅ 已落地
  - **DY1 year 语义**：`year`=**报考当年**（每请求传入，演示 2025），**无 base_year config**；**禁用服务器日历年、禁用库内 MAX(year)**（库内现存 2022-2025，取 MAX 会把验证年当训练年）。一切年份由 year 推导。
  - **DY2 概率窗口**=`[year-WindowYears, year-1]`（W=3、year=2025→**[2022,2024]，不含报考年**）。⚠️**关键**：04_major_admission 现已入库 2025，它是**验证 holdout**，窗口过滤**必须加上界 `ma.year<=year-1`**——原 L637 只有下界 `ma.year>=$4`，2025 入库后会把答案漏进训练窗口、回测失效。已修 BatchGroupHistory（$4=year-W、$5=year-1）+ 签名加 year。
  - **DY3**：score_rank_map 取 year（2025 control_score=405）、enrollment_plan 计划修正今年=year/去年=year-1。
  - **DY4 候选池**=报考年 enrollment_plan 实际招生的组；候选 SQL 去硬编码 `ma.year=2024`→参数化（$4=报考年-1 统计源、$5=报考年）+`EXISTS enrollment_plan(报考年)` 限定；统计仍取 major_admission(报考年-1)。
  - **DY5**：用户已补齐 2025 三表数据（实测存在）；缺失报考年数据→**明确报错、不静默回退**。
  - **数据事实修正**：原「仅 2022-2024、2025 未入库」已过时——2025 plan/score_rank/admission **均已入库**（2025=验证集，非缺失）。
  - **文档改动**：task2 新增 §3.0 年份口径契约表 + §2/§3.1/§3.x（WindowYears/PlanChange/候选SQL/§3.2/BatchGroupHistory窗口/BatchGroupPlan/Repo签名）/§4.2 RPC year 契约+防御；algo.proto 3 处 year 注释；kratos proto client 重生成 + go build 绿。

## ✅ Q17🟠. RankUsecase wire 缺失 + 4 RPC↔usecase 映射不明（P1）

- **问题**：`NewAlgo` 签名要 `rank *biz.RankUsecase`，但 `wire.Build` 无 `biz.NewRankUsecase` → wire 生成失败；RankUsecase 全文无定义（谁调 LoadScoreRankMap+ConvertByScore 不明）；4 RPC 各落哪个 usecase 未明确。
- **初步建议**：补 biz.NewRankUsecase（持 RankRepo）+ 加进 wire.Build；明确 RPC↔usecase 映射表（RankConvert→RankUsecase、EstimateProb→RecommendUsecase、CheckConstraints→?、OptimizeWishlist→OptimizeUsecase）。
- **结论（DZ1-DZ6 全采纳推荐）**：✅ 已落地（文档层；DZ6=A 实现阶段再重写旧骨架，本条不动代码）
  - **DZ1**：建 `RankUsecase{RankRepo}`，承接 `RankConvert`（薄 service 不能碰 Repo，必须 usecase 承接 LoadScoreRankMap+ConvertByScore）。
  - **DZ2 一 RPC=一 usecase 方法**：RankConvert→RankUsecase.Convert / EstimateProb→RecommendUsecase.EstimateProb / CheckConstraints→RecommendUsecase.CheckConstraints / OptimizeWishlist→OptimizeUsecase.OptimizeWishlist。
  - **DZ3**：CheckConstraints 作 RecommendUsecase 方法（与 EstimateProb 内部同一 constraint biz fn），不新增 usecase。
  - **DZ4**：RecommendUsecase 注入 `*RankUsecase` 复用换算（DRY），不再自持 RankRepo。
  - **DZ5 wire.Build**：补 `biz.NewRankUsecase(RankRepo)`；`NewRecommendUsecase(AdmissionRepo,*RankUsecase,*conf.Bootstrap)`；`NewOptimizeUsecase(*conf.Bootstrap)`；`NewAlgo(*RankUsecase,*RecommendUsecase,*OptimizeUsecase)`。
  - **DZ6**：文档 §1.1 `biz/data/service` 为准；实际 `internal/algo/{rank,prob,constraint,optimize}+service.go` 旧骨架（prob 带 CI/多方法已被 Q3 废、rank 用 Segment/int 已被 Q8/Q23 改）实现阶段按文档重写，本条不动代码。
  - **文档改动**：task2 §1.1（补 RankUsecase/OptimizeUsecase 归属）、§1.2（wire 链）、§4.1③（wire.Build+NewAlgo 签名）、§4.1④（新增 RPC↔usecase 映射表）、§4.2（四 RPC 伪代码）、里程碑 Phase 1-5。

## ✅ Q18🟠. service“薄”与 §4.2 重编排职责矛盾（P1）

- **问题**：§1.2/§3.2 说 service 薄、biz usecase 编排；但 §4.2 RPC 伪代码让 service 直接串 rank→prob→utility→optimize→checkSafeGuard。编排到底在哪层？
- **初步建议**：统一为 service 只做 DTO 映射 + 调一个 usecase 方法；多步编排（含 utility 批量、checkSafeGuard）进 OptimizeUsecase/RecommendUsecase；§4.2 伪代码改写为“usecase 内部步骤”。
- **结论（与 Q17 同源，一并收口）**：✅ 已落地
  - **编排层归属**：全部编排（取数/概率/效用/保底）在 **usecase 内部**；service 每个 RPC 方法体只做三件事——① 解析/校验入参（如 req.Year string→int）② 调**一个** usecase 方法 ③ biz↔pb 映射（§4.3）。
  - **禁止**：service 直接调 Repo 或串多个 biz 步骤（原 §4.2 伪代码让 service 直接调 RankRepo.LoadScoreRankMap/rank.ConvertByScore/constraint.CheckConstraints，已改写为 usecase 内部步骤）。
  - **文档改动**：task2 §4.1④ 新增“service 薄契约”、§4.2 四 RPC 伪代码全部改写为“service 薄 + usecase 内部编排”。

## ✅ Q19🟠. UtilityConfig.DefaultWeights 来源未纳入 conf（P1，Q12 遗漏）

- **问题**：Q12 的 conf.Bootstrap.Algo 收了衰减/阈值/配比，但**没含 utility 四维默认权重**；CalculateUtilityBatch 的 UtilityConfig.DefaultWeights（各 0.25）从哪来未定。
- **初步建议**：并入 conf.Bootstrap.Algo（加 utility_weights 段）或明确为代码内常量；与 Q12 两来源原则一致（算法常量←yaml）。
- **结论（DX1-DX4 全采纳推荐）**：✅ 已落地（文档层；conf.proto/algo.yaml 的 algo 段仍属 Phase 1 待建，与 Q12 同）
  - **DX1**：并入 `conf.Bootstrap.Algo`（补齐 Q12 自记的遗漏），与“算法常量←yaml”两来源原则一致（否决代码内常量：会造成部分外部化割裂）。
  - **DX2**：嵌套 message `UtilityWeights{double tier=1;major=2;location=3;strength=4;}` + `Algo.utility_weights=12`（与既有 `Ratio target_ratio` 风格一致、类型安全防 yaml key 拼错）；algo.yaml 加 `utility_weights: {tier:0.25,major:0.25,location:0.25,strength:0.25}`。
  - **DX3**：默认 UtilityConfig 由 **OptimizeUsecase 持有**（utility 只在 OptimizeWishlist 用），`NewOptimizeUsecase(*conf.Bootstrap)` 从 bc.Algo.UtilityWeights 构造（4 固定字段→map 转换在构造时做一次）。
  - **DX4**：yaml 值**不强制 Σ=1**（最终由 PriorityDims/PreferredTiers ×倍率后统一归一化）；缺失维**兑底 0.25**（防手误致崩）。
  - **文档改动**：task2 §3.4（UtilityConfig 注明 DefaultWeights 来源+兑底）、§4.1①（conf.proto Algo 加 UtilityWeights+字段12、algo.yaml 加 utility_weights）、§4.1②（构造说明补 UtilityConfig）。

## ✅ Q20🟠. RiskAppetite→TargetRatio 映射位置未定（P1）

- **问题**：§3.6 有 conservative/balanced/aggressive→配比表，但 OptimizeParams 同时有 RiskAppetite 和 TargetRatio，谁把前者转后者（service？optimize 内部？usecase 构造默认时？）未写。
- **初步建议**：定一个纯函数 `ratioFromRisk(risk)→RatioConfig`（biz 层），构造 OptimizeParams 时应用；per-request RiskAppetite 覆盖默认 TargetRatio。
- **结论（DR1-DR4 全采纳推荐）**：✅ 已落地（文档层）
  - **实测证据**：RiskAppetite 已在 UserProfile（L77，per-request profile 携带），OptimizeParams.RiskAppetite 是冗余重复字段——即“两字段谁赢”歧义根源。
  - **DR1**：删除 `OptimizeParams.RiskAppetite`，只留 `TargetRatio`（配比单一真相源，符合 Q5“要么生效要么删除”）；optimize.OptimizeWishlist 纯函数只认 TargetRatio。
  - **DR2**：biz 纯函数 `RatioFromRisk(risk)→RatioConfig`（放 optimize.go）：conservative{0.2,0.4,0.4}、balanced{0.3,0.4,0.3}、aggressive{0.45,0.35,0.2}；**空/未知 risk→balanced 默认**（防御不报错）。
  - **DR3**：转换+合并在 **OptimizeUsecase 内部**（持 conf 默认 OptimizeParams + 接收 profile）：默认 TargetRatio←conf.Algo.target_ratio；profile.RiskAppetite 非空→RatioFromRisk 覆盖。**service 保持薄**（只 parse proto profile→biz、传 candidates+profile）；顺带修正 §4.1②“service 层覆盖”旧措辞（合并归 usecase）+ §4.2②④ 签名去 mergedCfg/mergedParams。
  - **DR4**：MVP **不暴露裸 TargetRatio** 给请求（proto UserProfile 只带 RiskAppetite 枚举，防前端传非法/不归一配比）。
  - **文档改动**：task2 §3.5（OptimizeParams 删 RiskAppetite + 加 RatioFromRisk）、§3.6（配比表标注转换链 + 合并归属 note）、§3.6 交互流 L1056（OptimizeWishlist(candidates,profile)）、§4.1②（合并归 usecase）、§4.2②④（签名去 merged*）。

## ✅ Q21🟡. 错误模型未定（P2）

- **问题**：要“明确报错”但无错误码/gRPC status/Kratos errors 约定（internal/pkg/errors 存在未对接）。“低于本科线”“选科无法解析”“保底不足”各返回什么？
- **初步建议**：定 algo 服务错误码表（Kratos errors.New(code,reason,message)），区分用户可见（低于本科线）vs 内部（数据缺失）；SafeGuardOK=false 是告警非 error。
- **结论（DE1-DE4 全采纳推荐）**：✅ 已落地（文档层；errs 扩充 + service toKratosErr 属 Phase 5 实现）
  - **DE1**：采用 Kratos `errors` 包（`errors.New(code,reason,message)`，自动映射 gRPC status + 跨服务统一读 reason）；现有 `internal/pkg/errors`（stdlib sentinel）保留为领域 sentinel 定义中心。
  - **DE2 错误码表**（reason 稳定枚举、不靠 message 文案）：INVALID_YEAR(InvalidArgument/400) / SCORE_BELOW_CONTROL_LINE(FailedPrecondition/400) / CANDIDATE_WITHOUT_PROB(FailedPrecondition/400) 为用户可见；MISSING_SCORE_RANK_MAP(Internal/500) / NO_RELIABLE_DATA(Internal/500) 为内部。
  - **DE3**：保底不足 **不返回 error**——OptimizeWishlist 正常返回 WishlistResult（OptimizationReport.SafeGuardOK=false + 原因），交 orchestrator/前端告警（“合法结论非异常”）；MVP 不设硬红线拒绝。
  - **DE4 分层**：biz 纯函数返回**框架无关领域 sentinel**（stdlib errors，不 import Kratos）；usecase 透传/包装；**service 层统一 `toKratosErr(err)`** 映射为 Kratos errors（errors.Is 判 sentinel→reason/code）——与 Q18 薄 service、§4.3 biz 不 import pb 同理。
  - **文档改动**：task2 新增 §4.4 错误模型（分层原则 + 错误码表 + SafeGuard 告警语义 + Phase 5 落地）；§3.5 checkSafeGuard 注明 SafeGuardOK=false 是告警非 error。

## ✅ Q22🟡. MinSafeCount：yaml 固定值 vs 派生公式（P2）

- **问题**：yaml min_safe_count=9（固定），但语义是 max(3,⌈L×20%⌉)（随 L 派生）。若 per-request 覆盖 L（如 30），minSafe 该重算(6)还是用 yaml 的 9？
- **初步建议**：明确优先级——L 被覆盖时 minSafe 按公式重算，yaml 值仅作 L=45 默认/下限；或 yaml 存“比例”而非绝对值。
- **结论（DF1-DF3 全采纳推荐）**：✅ 已落地（纯文档层，无 conf schema 改动）
  - **先破题（实测）**：Q22 假设的“per-request 覆盖 L=30”场景在 MVP **不存在**——OptimizeWishlistRequest={trace,candidates,profile}、UserProfile 无 L、EstimateProbRequest 只有 per_category_limit（非总 L）→ **L(max_wishlist_size) config-only**，运行时 minSafe 恒 = conf.Algo.min_safe_count = 9，无优先级歧义。真正耦合在 config 内部：9 = max(3,⌈45×20%⌉) 是 max_wishlist_size 的派生值，改 L 时 min_safe_count 会静默漂移（与 Q20 冗余同类信号），但矛盾只在配置维护层、不在运行时。
  - **DF1-A**：yaml 保留 `min_safe_count=9` **绝对值**（Q4 可审计性优先），公式 `max(3,⌈L×20%⌉)` 降级为“默认值的推导说明”而非运行时计算；补 config 耦合 note（改 max_wishlist_size 需同步 min_safe_count，不一致时以显式 min_safe_count 为准）。零 conf schema 改动。（B 比例派生 min_safe_ratio+floor 被否：实际数字 9 不再显式可见、与 Q4“红线显式可审计”冲突，且 MVP 无请求级 L 覆盖→漂移仅运维改 yaml 时发生，文档 note 约束即可。）
  - **DF2（预留、MVP 不实现）**：若未来给请求/UserProfile 加 L 覆盖入口，minSafe 必须随覆盖后 L 重算 max(3,⌈L×20%⌉)，届时 DF1 应切到比例派生 B。仅登记 note。
  - **DF3（Q4 不变、仅澄清）**：OptimizeParams.MinSafeCount 字段保留（checkSafeGuard 读 params.MinSafeCount 显式 int）；默认由 OptimizeUsecase 从 conf.Algo.min_safe_count（绝对值）注入，非运行时跑公式；MVP 无请求级覆盖入口（proto 未暴露）。
  - **文档改动**：task2 §3.5（OptimizeParams.MinSafeCount 注释 + minSafe note + config 耦合 note + DF2 预留 note）、§4.1① conf.proto（min_safe_count 注释补耦合）、algo.yaml（min_safe_count 行注释）。

## ✅ Q23🟡. ConvertByScore 边界：上界 + float→int（P2）

- **问题**：score>表内最高分“返回错误或外推”二选一未定；UserQuery.Score 是 float64 而表 score 是 int，浮点分数如何匹配 score_range（截断/四舍五入）未定。
- **初步建议**：上界→clamp 到最高分段（不外推、可审计）；float score→先 round 到 int 再查（高考分多为整数）；均补单测。
- **结论（DG1-DG2 全采纳推荐）**：✅ 已落地（纯文档层；rank.go 重写属 Phase 实现）
  - **实测地基**（migrations/00_schema.sql + 06_score_rank_map.sql）：表列 score SMALLINT / score_range VARCHAR / segment_count / cumulative_count / rank_range / control_score，UNIQUE(province,year,category,batch,score)。顶段合并行 `score=693, score_range='693-750', cum=51, rank_range='1-51'`——**score 列=区间下限**，694-750 无独立行、被 693 行的 range 覆盖→**匹配必须用 score_range 解析出的 [ScoreRangeLow,ScoreRangeHigh]**，§3.1 ScoreRankEntry 结构正确非过时。表内最高 ScoreRangeHigh=750（=高考总分上限）。
  - **DG1-A（修正初案 clamp→error）**：归整后 `s > 750` → 错误 `SCORE_ABOVE_MAX`（InvalidArgument/400，用户可见）。理由：与下界 SCORE_BELOW_CONTROL_LINE 对称；750 是绝对上限、>750 必是脏输入，静默 clamp 到 rank1 会给“能上任何学校”垃圾推荐（更坏的静默失败）；Q21 已建错误模型加一个 reason 成本极低。**严禁外推**插值公式（s>750 使 (750−s)/(750−693) 变负→rank<1）。
  - **DG2-A**：入口 `s := int(math.Round(score))` 一次性四舍五入，后续匹配（ScoreRangeLow≤s≤ScoreRangeHigh）与顶段插值都用 s（高考分本整数、round 消浮点毛刺、确定性，与 Q24 衔接）。（B floor 被否：692.5→692 轻微低估位次。）
  - **连带修正**：记忆 b3489712 Note “score_range/rank_range 非现行算法所用字段”不准确（顶段必需 score_range）、且其提到的 total_count 列实际不存在（total=MAX(cumulative_count) 派生）——已修正该记忆。rank.go 旧骨架（score int + Segment）与 §3.1 脱节属 Q17 已知“实现时重写”，Q23 不动代码。
  - **文档改动**：task2 §3.1（step0 float→int round + step1/step2 用 s、score_range 区间匹配 + 边界处理定上界 error/禁外推）、§4.4（错误码表加 SCORE_ABOVE_MAX 行 + Phase5 sentinel 补 ErrScoreAboveMax）。

## ✅ Q24🟡. 并行计算 vs 可复现（P2）

- **问题**：性能表建议 goroutine 并行算概率，但“相同输入相同输出”要求结果顺序确定；并发 append 会乱序。
- **初步建议**：并行时预分配 indexed slice（results[i]）或事后按 GroupKey 稳定排序；MVP 候选量级（~3500）下单线程可能已够快（<2s 验收），并行可记为按需优化。
- **结论（DH1-DH3 全采纳推荐）**：✅ 已落地（纯文档层）
  - **实测地基**：候选输入序确定（SQL ORDER BY university_code,major_group,min_rank）；batchEstimateProb 现为单线程纯函数（按 candidate 序 append、historyMap/planMap 按 key 查非迭代）→ 当前已确定。真实瓶颈是 DB N+1（已用 2 条批量 SQL 解决 ~32ms），内存概率循环 ~3400 组纯算术 ≪2s 验收线——**并行是 premature optimization**。
  - **DH1-A**：MVP **保持单线程**；性能表“并行计算（goroutine）”降级为 **M2+ 按需优化**，且注明若并行必须用预分配 indexed slice（results[i] 按输入下标写）或事后稳定排序保序。（B 现在就上并行被否：无实测瓶颈支撑、复杂度高、YAGNI。）
  - **DH2-A**：§3.5 Step2/Step4 排序改用**全序比较器**（UtilityScore DESC，并列时 GroupKey{UniversityCode,MajorGroup} ASC）→ 彻底确定、不依赖输入序，`sort.Slice` 即可无并列残留；FinalRank=排序后下标+1 随之确定。（B 仅 SliceStable+单键被否：确定性依赖上游 SQL ORDER BY 不变。）
  - **DH3-A**：立规矩——任何产生**有序输出**的地方禁止直接 `range map`（Go map 序随机），需有序则收集 key 排序后遍历；Breakdown/Weights 等 map 仅按 key 取值/序列化（pb map 序不影响语义）。batchEstimateProb 按 key 查 map（非迭代）→已安全。
  - **文档改动**：task2 §1.2（可复现原则扩写为三条纪律：全序排序/禁 map 迭代产序/并行须保序）、性能优化表 L805（并行降级 M2+ + indexed slice 约束）、§3.5 Step2/Step4（排序补全序 tie-break）。

---

## ✅ Q25🔴. CheckConstraints RPC 物理上做不了选科匹配 + biz/usecase 签名类型全不匹配（P0）

- **问题**：①`CheckConstraintsRequest={trace,profile,candidates}`、`UserProfile` 无 subjects 字段，但 §3.3 选科匹配 `matchSubjects(userSubjects,req)` 的 userSubjects 来自 `UserQuery.Subjects`→RPC 拿不到选科、核心硬约束无法执行（其余约束在 M1 都不落地→该 RPC M1 实际无事可做）；②biz 纯函数 `CheckConstraints(query UserQuery,[]Candidate)→([]Candidate,[]Rejection,err)` 与 usecase `s.recommend.CheckConstraints(ctx,profile,[]CandidateWithProb)→([]CandidateWithProb,[]RejectedCandidate)` 入/出参类型全不匹配。
- **初步建议**：A 给 proto 补 subjects+category、usecase 签名统一、内部拆 Candidate 调纯函数再包回；B 砍掉该独立 RPC（选科过滤已在 EstimateProb 内做）。
- **结论（DG5 全采纳推荐 A）**：✅ 已落地（proto 契约变更 + 文档 + make api 重生成）
  - **DG5-1-A**：`CheckConstraintsRequest` 补 `repeated string subjects=4` + `string category=5`（UserProfile 不含选科、故独立传入，与 EstimateProbRequest.subjects 同源）。保留 A 而非砍 RPC（B）：orchestrator 可能需要“用户手动加了池外院校、单独轻量校验选科”的入口。
  - **DG5-2**：usecase 签名统一为 `RecommendUsecase.CheckConstraints(ctx, subjects []string, category string, candidates []CandidateWithProb) → (kept []CandidateWithProb, rejected []RejectedCandidate)`；内部：从 []CandidateWithProb 抽 []Candidate + 构 `UserQuery{Subjects,Category}` → 调 constraint 纯函数（与 EstimateProb step c 同一 biz fn，DZ3）→ 按 `GroupKey{UniversityCode,MajorGroup}` 从入参池回填 kept/rejected 的 Probability/Category（选科不依赖 prob、回填仅为契约形状完整）。
  - **验证**：`kratos proto client api/algo/v1/algo.proto` 重生成→algo.pb.go 现含 GetSubjects()/GetCategory()；Python stub 同步（algo_pb2 序列化含 subjects\x18\x04/category\x18\x05）；`go build ./...` exit=0。
  - **文档改动**：algo.proto（CheckConstraintsRequest 补两字段）、task2 §3.3（biz 纯函数签名 + 统一 RejectedCandidate 说明）、§4.2③（usecase 签名与三步编排伪代码）。

## ✅ Q26🟠. conf.proto 缺 message Server + newApp 无定义 + 无 gRPC 注册 glue（P1）

- **问题**：§4.1① Bootstrap 引用 `Server server=1` 但全文无 `message Server` 定义；wire.Build 有 `newApp,` 但全文无 newApp 定义；也无 gRPC server 注册 glue（grpc.NewServer + RegisterAlgoServer + kratos.New）→照文档搭不出能启动的 kratos app。
- **结论（DG3 全采纳推荐）**：✅ 已落地（纯文档层，代码属 Phase 1）
  - **实测地基**：真实 `configs/algo.yaml` 仅 `server.grpc.{addr:0.0.0.0:9002, timeout:30s}` + `data.database.dsn`；旧 `cmd/algo/main.go` 骨架已示范 kratos glue 模式（grpc.NewServer(grpc.Address) + algov1.RegisterAlgoServer(gs,svc) + kratos.New(Name,Server)）。
  - **补 message Server**：`message Server { message GRPC { string addr=1; string timeout=2; } GRPC grpc=1; }`（字段严格对齐 algo.yaml，不多不少）。
  - **补 newApp**：`func newApp(bc *conf.Bootstrap, svc *service.Service, logger log.Logger) *kratos.App`——读 `bc.Server.GetGrpc().GetAddr()/GetTimeout()` 建 gRPC server、`algov1.RegisterAlgoServer(gs,svc)`、`kratos.New(Name("svc-algo"),Server(gs),Logger(logger))`；旧骨架硬编码 :9002 弃用。wire.Build `newApp,` 补注签名。
  - **文档改动**：task2 §4.1①（conf.proto 补 message Server）、§4.1③（wire.Build newApp 注签名 + 新增 newApp 定义代码块）。

## ✅ Q27🟠. RankResult 字段三处不一致（编译不过）（P1）

- **问题**：同一 struct 三个版本——§2.2 `{Rank,Percentile,TotalCount}`；§4.2① 伪代码 `biz.RankResult{Rank,Percentile,ControlScore}`（ControlScore 非 §2.2 字段、直接构造编译失败）；proto `RankConvertReply` 只 `{rank,percentile}`。
- **结论（DG1 全采纳推荐）**：✅ 已落地（纯文档层）
  - biz `RankResult` 定为四字段 `{Rank int; Percentile float64; TotalCount int; ControlScore int}`（TotalCount=百分位分母、ControlScore=本科线下界校验，均 ConvertByScore 内部派生量）；§4.2① 伪代码构造补全为四字段；proto `RankConvertReply` 保持只回 `{rank,percentile}`（内部字段不外泄，符合薄契约）。
  - **文档改动**：task2 §2.2（RankResult 补 ControlScore + proto 只回二字段 note）、§4.2①（伪代码构造改四字段）。

## ✅ Q28🟠. Rejection vs RejectedCandidate 双类型 + 缺 Rule（P1）

- **问题**：§3.3 `Rejection{Candidate;Reason}`（无 Rule）与 §2.2 `RejectedCandidate{CandidateWithProb;Rule;Reason}` 两类型并存；proto `RejectedCandidate` 要 rule 字段→biz 返回的 Rejection 无 Rule、service 映射时 rule 无从填。
- **结论（DG6 全采纳推荐）**：✅ 已落地（纯文档层）
  - 删 `Rejection`，全链统一 `RejectedCandidate{CandidateWithProb;Rule;Reason}`（§2.2/proto/§4.3 映射表一致）。biz `CheckConstraints` 直接返回 `[]RejectedCandidate`（选科不过→`Rule="subject_req"`）；因纯函数处于概率计算之前，其 rejected 项 `CandidateWithProb.ProbEstimate` 为零值（仅填 Candidate+Rule+Reason），独立 RPC 由 usecase 按 GroupKey 回填 prob。补触发规则 key 常量枚举（subject_req/single_score/physical/gender/batch）。连带修正 §3.3 边界文案 `Rejection.Reason`→`RejectedCandidate.Reason`。
  - **文档改动**：task2 §3.3（删 Rejection struct + biz fn 返回类型 + Rule 常量 + 边界文案）。

## ✅ Q29🟡. RecommendConfig.PreferTiers/ExcludeTiers 死参（P2）

- **问题**：`RecommendConfig.PreferTiers/ExcludeTiers`（§3.2）全文只在定义 + §3.6 举例出现，无任何过滤逻辑消费（与 UserProfile.PreferredTiers 不是一回事，后者在 utility 调权重）→死参。
- **结论（DG9 全采纳推荐）**：✅ 已落地（纯文档层）
  - 删除 PreferTiers/ExcludeTiers（YAGNI，同 CareerGoal 处理）；学校层次偏好统一走 `UserProfile.PreferredTiers`（在 utility 层调 tier 权重、非硬过滤，§3.4）；后续若真需层次硬过滤再加回。留 note 说明去向。
  - **文档改动**：task2 §3.2（RecommendConfig 删两字段 + YAGNI note）。

## ✅ Q30🟡. 文档一致性小修合集（P2）

- **问题**：G7 §3.6 交互流把 biz 内部纯函数 `RecommendCandidates` 当对外接口呈现（实际 RPC 是 EstimateProb）；G7b §3.6 保底告警仍用 Q22 之前的内联公式 `max(3,L×20%)`；G4 `RecommendResult` 定义在 §3.2 而非 §2（组织不一致）；G2 §2 标题 `types/types.go`、§4 标题 `service.go` 与 §1.1 实际布局 `biz/types.go`+`service/algo.go` 漂移。
- **结论（全采纳推荐）**：✅ 已落地（纯文档层）
  - **G7**：§3.6 表格/交互流/职责边界的 `RecommendCandidates` 统一改为对外 RPC `EstimateProb`（并注明内部调 biz RecommendCandidates）。
  - **G7b**：§3.6 保底告警 `max(3,L×20%)`→`SafeGuardOK=false，即稳拿保底数 < params.MinSafeCount`（与 §3.5 Q22 收口一致）。
  - **G4**：§2.2 末尾加交叉引用 note（RecommendResult 就近定义在 §3.2，§2 不重复）。
  - **G2**：§2 标题 `types/types.go`→`biz/types.go`、§4 标题 `service.go`→`service/algo.go`；连带 §3.1~§3.5 子标题 `rank/rank.go` 等旧扁平路径→`biz/rank.go` 等（对齐 §1.1 布局）。
  - **文档改动**：task2 §2.2/§3.1~§3.5 子标题/§3.6/§4 标题。

---

## ✅ Q31🟠. RecommendCandidates 签名与“biz 纯函数、IO 归 usecase”矛盾（P1）

- **问题**：§3.2 `RecommendCandidates(query UserQuery, config RecommendConfig) (RecommendResult, error)` 只吃 query+config、无法拿到候选/历史/计划（那些要查库）；但 §4.2② step d 又让它自己调 BatchGroupHistory/BatchGroupPlan（IO），与同章 L607“batchEstimateProb 是纯函数、historyMap/planMap 由 data 注入”及“SQL 不得入 prob 包”铁律相矛盾。是 Q17/Q18 编排上移 usecase 后 §3.2 老“does-everything”签名未同步的残骸（孤儿函数类）。
- **结论（DH1-A 采纳）**：✅ 已落地（纯文档层）
  - 重定义 `RecommendCandidates` 为**纯组合函数** = batchEstimateProb + categorize，签名改吃注入数据：`func RecommendCandidates(candidates []Candidate, userPercentile float64, historyMap map[GroupKey][]AdmissionRecord, planMap map[GroupKey]PlanChange, config RecommendConfig) RecommendResult`（不再返 error）；编排（rank→LoadCandidates→CheckConstraints→调 Repo 取 map）归 RecommendUsecase.EstimateProb。B（删函数、usecase 直调两子步）被否：保留 prob 层单一入口、可单测，与 §3.6/数据流图对 prob.RecommendCandidates 的既有引用一致。
  - **文档改动**：task2 §3.2（RecommendCandidates 签名+注释）、§4.2② step d（usecase 调 Repo 取 map 再注入纯函数）、数据流图 L1366（major_admission 经 usecase/Repo 注入→RecommendCandidates→RecommendResult）。

## ✅ Q32🟠. biz.RankQuery 被引用但全文无定义（编译不过）（P1）

- **问题**：§4.2① `s.rank.Convert(ctx, biz.RankQuery{Province,Category,Year,Score})` 引用 `RankQuery`，但 §2 只定义了 UserQuery、无 RankQuery→照拄即 `undefined: biz.RankQuery`（RankConvert 是 Phase 1 首个实现的 RPC）。
- **结论（DH2-A 采纳）**：✅ 已落地（纯文档层）
  - §2.1 补轻量 `RankQuery{Province string; Category string; Year int; Score float64}`（只带换算必需四项，比复用带 Rank/Percentile 回填字段的 UserQuery 更干净）。B（直接复用 UserQuery）被否。
  - **文档改动**：task2 §2.1（新增 RankQuery struct）。

## ✅ Q33🟠. categorize() 不回填 *Total → EstimateProbReply 三个 total 恒为 0（P1）

- **问题**：`RecommendResult` 声明 RushTotal/StableTotal/SafeTotal/TotalCandidates/FilteredCount 五个 int，proto `EstimateProbReply` 也要 rush/stable/safe_total；但 `categorize()` 只 append 三个 *Candidates 切片、从不给 total 赋值→回前端的分档统计全为 0，§3.6“还有 N 个”/分页显示失效。
- **结论（DH3 采纳）**：✅ 已落地（纯文档层）
  - categorize 收尾补：`RushTotal=len(RushCandidates)`（余同）、`TotalCandidates=入参候选数`、`FilteredCount=概率<RushThreshold 淘汰数`；note：MVP PerCategoryLimit=0 不截断故 *Total==len(*Candidates)，若 >0 截断则 *Total 记截断前、*Candidates 留截断后（与字段注释“截断前”一致）。
  - **文档改动**：task2 §3.2 categorize（初始化 TotalCandidates + 淘汰计数 FilteredCount++ + 收尾回填三 *Total + note）。

## ✅ Q34🟡. usecase 层无一处 Go 定义（struct/构造/方法签名）（P2）

- **问题**：三个 usecase 是 Q17/Q18 后所有编排的落点，但全文 grep 不到 `type XxxUsecase struct`、也无任一方法的 Go 签名；构造参数只写在 wire.Build 行尾注释、方法签名只能从 §4.2 调用点反推。biz↔service 边界（谁持 conf/Repo、方法吃 biz struct 还是散参）无一处权威定义。
- **结论（DH4 采纳）**：✅ 已落地（纯文档层）
  - §4.1③ newApp 块后新增“usecase 契约”代码块：`RankUsecase{repo RankRepo}` / `RecommendUsecase{admission AdmissionRepo; rank *RankUsecase; defaultCfg RecommendConfig}` / `OptimizeUsecase{defaultProps OptimizeParams; defaultUtil UtilityConfig}` 三 struct + 三构造函数完整签名 + 4 方法签名（`Convert(ctx,RankQuery)(RankResult,error)` / `EstimateProb(ctx,UserQuery,UserProfile)(RecommendResult,error)` / `CheckConstraints(ctx,subjects []string,category string,[]CandidateWithProb)([]CandidateWithProb,[]RejectedCandidate,error)` / `OptimizeWishlist(ctx,[]CandidateWithProb,UserProfile)(WishlistResult,error)`）。
  - **连带修正**：§4.2③ CheckConstraints 伪代码 `kept, rejected :=`→`kept, rejected, err :=`（与契约块 error 返回一致）。
  - **文档改动**：task2 §4.1（新增 usecase 契约块）、§4.2③（补 err）。

## ✅ Q35🟡. dto.go 路径与分层归属矛盾（P2）

- **问题**：§4.3 说 toPb/fromPb“集中在 `internal/algo/dto.go`”，但 §1.1 布局 service 层在 `internal/algo/service/`，且 biz↔pb 映射是 service 职责（biz 不得 import pb）。放 algo 根目录归属不明（与上轮 G2 同类路径漂移）。
- **结论（DH5 采纳）**：✅ 已落地（纯文档层）
  - §4.3 `internal/algo/dto.go`→`internal/algo/service/dto.go`；§1.1 目录树 service/ 下补一行 `dto.go # biz↔pb 映射（toPb*/fromPb*）`。
  - **文档改动**：task2 §1.1（目录树补 dto.go）、§4.3（路径修正）。

> 本轮（Q31–Q35）无 proto/代码改动，均为纯文档层，无需 make api / go build。根因同一：Q17/Q18 把编排上移 usecase 后，§3.2 的 prob 层老签名与 usecase 层未同步收口（H1/H2/H3），以及 usecase 层本身缺一份权威签名规格（H4）。
