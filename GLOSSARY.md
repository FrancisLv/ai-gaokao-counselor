# GLOSSARY

本仓库的**单一上下文术语表**（single-context，约定见 `docs/agents/domain.md`）。输出中涉及领域概念时（issue 标题、重构提案、假设、测试名），用此处定义的术语，不要漂移到「避免」列点名的同义词。`docs/prd.md §11` 术语表是本表的子集，**以本表为准**。

术语缺口是信号：要么你在发明项目不用的语言（请重考虑），要么是真实缺口（记下来交给 `/domain-modeling`）。

## 高考志愿领域术语

| 术语 | 定义 | 避免 / 备注 |
| --- | --- | --- |
| 冲 / 稳 / 保 | 按录取概率分层的志愿策略：冲刺 / 稳妥 / 保底 | — |
| 冲稳保配比 | 最终志愿表中冲、稳、保三档各自占用的名额结构；可随用户风险偏好调整 | **避免把“增加几个冲”直接理解为追加候选或突破总数上限** |
| 院校专业组 | 湖南省新高考平行志愿的投档单位（一组选科要求相同的专业），候选生成的基本单位 | 不要退化为「院校 + 专业」二元 |
| 当年计划专业 | 报考年 `enrollment_plan` 中实际列出的专业代码/名称及其计划信息；决定候选组内可报专业与资格输入 | 不以上一年 `major_admission` 专业组合作替代 |
| 专业（major） | 报考年招生计划中的一个本科专业名称及其代码，是院校专业组的成员身份 | 不等同院校专业组，也不把兴趣方向直接当专业名称；同代码不同名称不构成同一专业身份 |
| 拟填院校专业组 | 用户在志愿草稿中保留的院校专业组及其填报顺序；一份已保存草稿至少 1 个、至多 45 个 | 不表示必须填满 45 个；单组操作称“移出”，整份志愿表操作才称“删除” |
| 未纳入志愿表的推荐专业组 | 正式报告中已推荐、但不在当前志愿草稿的拟填院校专业组集合中的组；用户可将其重新加入 | 不等于删除正式推荐结果，也不等于未通过资格审查的候选 |
| 拟填专业 | 用户在某个院校专业组内决定填报的专业及顺序，每组至少 1 个、至多 6 个 | 不等于组内全部计划专业；不能把未选专业从组身份中删除 |
| 志愿草稿 | 基于已发布正式推荐结果保存的用户拟填院校专业组、各组拟填专业及顺序；可版本化恢复，但不改变正式报告 | 不是 `REVISE_PLAN` 结果，也不是新的正式推荐版本 |
| 草稿风险摘要 | 基于当前拟填院校专业组集合及已验证概率，由确定性代码重新计算的志愿表级风险摘要 | 不是正式报告风险结论；不得由 LLM 计算或改写 |
| 默认专业推荐 | 系统从当年计划专业中按偏好与历史参考确定性选出的至多 6 个可填专业，用户可替换和重排 | 不是不可修改的硬约束，也不得包含资格未知或失败的专业 |
| 专业级录取概率 | 具体专业在同 release 历史专业录取参考下的估计命中概率；无足够可靠历史时为 UNKNOWN | 不复制组级进档概率，不按专业名称模糊拼接历史 |
| 专业兴趣（major interest） | 用户想了解、优先考虑或希望就读的专业或学科方向；默认是软偏好，只影响排序与解释 | 不因“想学”自动升级为专业限定 |
| 专业限定（major restriction） | 用户明确要求只看、仅限或必须包含的专业范围；执行前须确认精确名称或相关专业群 | 只有明确限定语义才能作为硬筛选 |
| 高中选科科目 | 新高考 3+1+2 中的物理、历史、化学、生物、政治、地理等考试科目 | 「物理」是选科科目，不是科类或本科专业 |
| 选科组合（3+1+2） | 首选科目（物理 / 历史）+ 再选 2 门 | — |
| 科类（category） | 物理类 / 历史类（新高考 3+1+2）；位次与概率计算的必需过滤条件 | **避免 track**（track 是废弃表 admission_score 的旧字段名，活跃表统一用 category）；也不要把「想学物理学」推断为「物理类」 |
| 批次（batch） | 招生录取批次；V1 仅支持本科批，缺失时使用本科批默认值并在画像确认卡展示 | 其他批次不属于 V1 支持范围 |
| 一分一段表 | 省考试院发布的分数 → 累计位次映射表（`score_rank_map`） | — |
| 位次（rank） | 考生在全省同科类的排名 | — |
| 百分位（percentile） | `rank / total_count` | — |
| 批次控制线（control_score） | 各批次录取最低控制分数 | — |
| 加权命中率法（weighted_hit） | 现行录取概率估计方法（详见 `docs/algorithms.md`） | **避免「分位数法 / 核密度」**（已废弃设计，非 V1 承诺） |
| 临界命中 | 用户百分位与历史录取百分位相差不超过 0.2 个百分点的年份；按 0.5 个命中权重参与加权 | 不记为完整命中或完整未命中；计划变化不修改该判断 |
| 专业组历史录取参考 | 同一院校代码、专业组代码和年份下，按最低录取分及同条记录最低位次形成的历年参考；数据缺失时明确标注，不插值 | **避免称为官方组投档线**；当前来源是专业录取明细聚合，不是考试院正式组线 |
| 专业组 lineage | release 绑定的跨年身份映射：当年院校专业组可映射到一个历史年份的录取专业组，并记录匹配方式、置信度和专业名称证据 | 不等同院校层级匹配；低置信或有歧义时不建立映射 |
| 大区表达（macro-region expression） | 用户以“北方”“南方”“华南”“东北”等非单一省份的地域称谓表达范围；必须先澄清为受控省份集合 | **不得直接写入 `required_provinces` / `excluded_provinces`，也不得以未确认的内置映射形成硬约束** |
| 大区快捷组（region preset） | release 绑定的省份组合，用于辅助用户选择；“选中”只填充待确认省份，不直接形成筛选条件 | **不得作为画像字段，不得绕过省份展示与用户确认** |
| 地域硬约束 | 用户明确限定或排除院校所在省份 / 城市的筛选条件；省份与城市分别表示，模糊大区表达须先澄清 | **避免把“广东”塞进城市字段或暗扩为固定城市清单** |
| 滑档率（slip_rate） | 平行志愿下全部未投档的解析式估算 Π(1−pᵢ) | 是近似估计，**非录取保证** |
| 期望满意度（expected_satisfaction） | 志愿表效用分均值 | — |
| 适用域（applicability） | 文档或规则适用的省份、科类、批次和生效区间 | 不是排序因素，必须在计算相关度前过滤 |
| 生效区间（valid interval） | 文档或规则开始、结束有效的日期范围；结束日期为空表示持续有效 | 年度章程不得只靠“当前年份”推断 |

## 系统 / 身份模型术语

| 术语 | 定义 | 避免 / 备注 |
| --- | --- | --- |
| 用户账号 | 受控多用户系统中用户持有的持久身份；账号资料只保存登录与账户管理所需信息，不承载考生规划画像 | **避免把账号或账号资料称为用户画像** |
| 邀请码 | 受控发放的一次性注册凭据，用于限制谁能创建用户账号 | 不是长期共享口令，也不承担用户身份 |
| 恢复码 | 注册时生成、由用户自行保存的一次性备用凭据，仅用于重设密码 | 不是日常登录方式；不得以明文持久化 |
| 登录会话 | 用户在某个浏览器或设备上的可撤销认证状态，服务端据此解析出 principal；用户可退出当前会话，改密或重设密码会撤销其他会话 | **避免与对话 session 混用；V1 不提供用户可见的“登录设备”管理列表** |
| 账户删除 | 用户主动终止用户账号并删除其全部私有规划资料的操作；完成后不可登录或恢复 | 不等于删除单份志愿草稿 |
| 志愿表删除 | 用户删除一份已保存志愿表的全部草稿版本并解除父结果的长期保留保护；不直接删除正式报告 | 不等于账户删除，也不是只隐藏最新版本 |
| 志愿表状态 | “我的志愿表”中当前可用、已被新方案替代、已失效、已删除/不可用四种用户可见状态的统称 | 不把“被新方案替代”等同于“安全失效” |
| principal（principal_id） | 经可信认证产生的用户或受控匿名主体；决定 session/run 的访问归属 | **避免用请求正文自称身份** |
| session（session_id） | 会话容器，可包含多个 run；不是恢复或幂等单位 | **避免以 session 代替 run** |
| run / interrupt | run = 一次任务执行（每次生成 / 修订新建）；interrupt = run 内一次人工中断。恢复与幂等以 run/interrupt 为单位 | **避免以 session 为恢复单位** |
| 考生画像 | 特定规划上下文中经用户确认的报考事实与偏好；按 `profile_version` 保存为不可变版本 | **避免把分数、选科、兴趣等规划数据存入账号资料** |
| profile_version | 不可变画像 revision 的服务端标识；确认操作绑定具体 version | **避免原地修改已确认画像** |
| state_version | run 对外可见状态的乐观并发版本，每次合法状态变化递增 | 不等于 LangGraph checkpoint 编号 |
| result_version | run 通过全部门禁后发布的不可变结果版本 | — |
| event_seq | run 内持久业务事件的单调递增序号，用于补发和去重 | SSE 心跳不占序号 |
| terminal_reason | run 进入 FAILED/EXPIRED 等终态时的稳定业务原因 | 不把原因只写在日志 |
| release_id | run 绑定的不可变数据 / 规则 / 索引发布清单版本（内容哈希，见 ADR-0004）；各读取端按它选数据、缓存按它失效 | **避免 data_version**（可变 SERIAL int，已弃用过渡） |
| 三类证据 | 文档事实（绑原文片段）/ 结构化事实（绑行身份 + 字段 + 版本）/ 派生事实（绑输入快照 + 样本 + 算法参数版本，确定性重算） | — |
| Verifier（事实校验器） | 按三类证据将报告事实性陈述逐条比对的环节 | — |
| HITL | Human-in-the-loop，流程中的人工确认 / 补问暂停点（interrupt/resume） | — |
| Budget Guard | 会话级 token / 成本预算闸门，原子预占 + 结算 | — |
| logical_call_id / attempt_id | logical_call_id 标识一次语义调用；attempt_id 标识一次供应商尝试 | — |
| budget_scope_id | 服务端生成的 `principal + session` 预算作用域；跨 run 不重置 | 不接受客户端覆盖 |
| 预占 / 结算（reservation / settlement） | 调用前按成本上界锁定预算，调用后按实际 usage 结算并释放差额 | 未知费用不得立即释放 |
| price_table_version | 带生效日期、货币和汇率版本的模型价格表版本 | 不使用未版本化价格 |
| AuditBasis（审计依据） | algo 输出的可复现依据：用户位次 / 百分位、样本年份、method、province、category | — |
| RAG | 检索增强生成；本系统**仅用于非结构化章程文本** | 强结构化数据走 SQL，不走 RAG |
| RRF | 倒数排名融合，合并向量与关键词两路检索结果（Σ 1/(k+rank)，k=60） | — |
| policy_chunk / policy_doc | 招生章程 RAG 的**活表**（向量 + trgm） | **避免 charter_chunk / charter_doc**（孤儿空表，已废弃，见 ADR-0006） |
| golden set | 版本化管理的标注问答集，用于 CI 回归门禁（检索集 + 端到端集，见 ADR-0005） | — |
| SSE | Server-Sent Events，服务端流式推送 | — |
| ALGORITHM_PREVIEW | 仅使用确定性算法生成的内部受控验证结果，不宣称当年资格已完整核验 | 不得作为正式志愿方案 |
| V1_DEMO | 完成 P0、验收场景与安全门禁后可公网演示的版本，结果仍标注为历史参考 | **避免称为正式推荐发布** |
| V1_FORMAL | 完成全部 P0 + P1，并关闭全部 `REVIEW-P1-*` 阻塞问题的正式推荐发布版本 | — |
| RuleVersion | 一条结构化硬规则的具体不可变版本，包含适用域、所需字段、条件和来源证据 | 自然语言条款本身不是 RuleVersion |
| RuleCoverage | 某规则类型在指定范围内的覆盖声明：FULL/PARTIAL/NONE | 无规则不等于无限制 |
| PASS / FAIL / UNKNOWN | 资格规则或候选的三态结果；UNKNOWN 表示无法确定，不得按通过处理 | 不得用“低置信通过”替代三态 |
| EvidenceRef | 指向文档、结构化行或算法派生事实的不可变证据引用，必须绑定 release | 不是单纯 URL |
| Claim（事实陈述） | 报告中的规范化事实及其证据引用 | 文案不是权威事实本身 |
| VERIFIED / CONTRADICTED / UNVERIFIABLE | 事实校验的三种结果；CONTRADICTED 必须阻断或回算 | 不得静默放行 |
| EvalDataset | 由 domain owner 定标并内容哈希版本化的检索/端到端评测集 | 不在原版本上覆盖用例 |
| EvalRun | 在固定代码、release、模型路由和环境下执行一次评测的不可变记录 | 不把单次热库测试当作 P99 |
| recall@k | 至少命中一条预期证据的查询数 / 有效查询数 | NO_EVIDENCE 用例不进入分母 |
| citation coverage | 有至少一条 EvidenceRef 的事实陈述数 / 全部事实陈述数 | 最终报告要求 100% |
| hallucination rate | CONTRADICTED 或 UNVERIFIABLE 的事实陈述数 / 全部事实陈述数 | 安全门禁不接受容差 |
| active execution latency | run 处于 RUNNING 的累计耗时，不含排队和 HITL 等待 | 与 wall clock 分开统计 |
| 数据分类（data classification） | PUBLIC / PRINCIPAL / INQUIRY / SENSITIVE_ELIGIBILITY / OPERATIONAL / SECRET | 敏感原文默认不进云模型 |
| 删除 tombstone | 记录删除请求已完成范围的最小审计标记，不含被删内容 | 备份恢复前必须重放 |

## 前端交互术语

| 术语 | 定义 | 避免 / 备注 |
| --- | --- | --- |
| 对话（chat） | 用户界面中一条连续的聊天记录；内部对应一个 session，可包含多次生成或修订产生的 run | 面向用户不显示 runId / stateVersion；避免把每次 run 当成一条新对话 |
| 对话消息（chat_message） | 对话时间线中的一条用户、助手或系统消息，可关联产生它的 run；正文与执行结果分开保存 | 不是 run event；不要把事件日志直接当作用户可见聊天记录 |
| 报告修订（revision） | 基于指定父 `result_version` 创建的新 `REVISE_PLAN` run；重新确认画像并生成不可变新版报告 | 不原地修改旧报告；不要用模型直接删除、重排或改写候选身份与概率；明确增档却无法实际改善时不得发布同内容新版 |
| 报告后意图 | 用户在已有正式报告后的输入意图：询问现有结果、修改筛选条件，或信息不足需澄清 | 不把明确否定条件当作解释问题；模型不得直接执行画像或方案修改 |
| 志愿域 | 当前 V1 支持湖南 2025 本科批物理类/历史类的画像采集、方案生成、报告解释与修订 | 不把其他省份、年份、升学类型或艺术体育默认纳入 |
| 我的志愿表 | 用户查看自己已保存志愿草稿的功能入口；每份正式结果默认展示一条当前项，并可查看该草稿的版本历史与有效性 | **避免把该页面称为新的正式推荐结果，也不要与对话列表混为同一资源** |
| 范围外问题（OUT_OF_SCOPE） | 与升学有关但超出当前支持省份、年份、批次或升学类型的问题 | 不要当作闲聊，也不要创建规划 run |
| 无关问题（OFF_TOPIC） | 与志愿填报无关的天气、编程、娱乐、医疗等普通问题 | 不要尝试回答，不要猜测用户意图后改写方案 |
| 安全危机输入 | 表达自伤、自杀、现实人身危险等需要优先保障安全的输入 | 不受普通业务范围模板限制；不得先做志愿任务或普通意图分类 |
| LLM 降级 | LLM 不可用、超时或输出越界时，保留确定性主流程并显式记录 `DETERMINISTIC_FALLBACK` 与原因 | 不等于静默失败；不得把确定性模板结果冒充模型输出 |
| 静态推荐报告 | 从已发布 result_version 生成的不可变 HTML 视图，包含一个志愿方案汇总页和每所推荐学校的详情页，可通过固定链接直接打开 | 不是重新查询后实时渲染的页面；避免与内部 verification_report 混用 |
| PDF 志愿报告 | 基于已保存志愿草稿快照与同一父静态推荐报告生成的个人报告导出物；记录对应的 `result_version` 与 `draft_version` | 不由页面未保存修改派生；不是新版正式推荐结果，也不是录取保证 |

## 已拍板决策索引（详见 `docs/adr/`）

| ADR | 决策 |
| --- | --- |
| [0001](docs/adr/0001-defer-zhparser-keep-trgm.md) | 维持 pg_trgm，推迟 zhparser |
| [0002](docs/adr/0002-langgraph-official-postgres-saver.md) | 官方 langgraph-checkpoint-postgres |
| [0003](docs/adr/0003-deepseek-early-llm-experimental.md) | 早期 DeepSeek 实验链路，M5 收口网关 |
| [0004](docs/adr/0004-immutable-content-hash-release-id.md) | 内容哈希不可变 release_id |
| [0005](docs/adr/0005-golden-set-human-labeled.md) | golden set 由 domain owner 定标 |
| [0006](docs/adr/0006-consolidate-policy-tables-drop-charter.md) | 收口 policy_*，废弃 charter_* |
| [0007](docs/adr/0007-static-html-recommendation-report.md) | 推荐结果生成不可变静态 HTML 汇总页与学校详情页 |
| [0008](docs/adr/0008-server-persisted-conversation.md) | 对话消息服务端持久化，报告后筛选创建不可变修订 run |
| [0009](docs/adr/0009-group-history-lowest-score-pair.md) | 专业组历史最低分与位次取同一条最低分记录，并用于概率口径 |
| [0010](docs/adr/0010-current-plan-majors-define-members.md) | 候选组内专业身份由报考年招生计划决定 |
| [0011](docs/adr/0011-release-bound-group-lineage.md) | 跨年专业组通过 release 绑定的 lineage 映射历史录取 |
| [0012](docs/adr/0012-explicit-report-intent-revises-plan.md) | 明确筛选指令直接创建报告修订，LLM 仅作受限兜底 |
| [0013](docs/adr/0013-governed-llm-enhancement-with-fallback.md) | LLM 仅增强语义与文案，失败时显式审计降级 |
| [0014](docs/adr/0014-province-city-hard-filters.md) | 省份与城市分别建模为地域硬约束 |
| [0015](docs/adr/0015-conversation-scope-and-safety.md) | 区分无关问题、范围外问题与安全危机输入 |
| [0016](docs/adr/0016-exact-major-interest-and-restriction.md) | 专业兴趣与专业限定使用受控专业名称，相关专业须明确确认 |
| [0017](docs/adr/0017-governed-domain-queries-not-text-to-sql.md) | 精确数据查询使用受控领域查询，不做通用 Text-to-SQL |
| [0018](docs/adr/0018-wishlist-selection-is-versioned-draft.md) | 用户选专业保存为独立版本化志愿草稿 |
| [0019](docs/adr/0019-major-level-probability-is-independent.md) | 专业级录取概率独立估计，缺失时为 UNKNOWN |
| [0020](docs/adr/0020-browser-print-pdf-from-saved-wishlist-draft.md) | PDF 志愿报告使用已保存草稿与浏览器原生打印 |
| [0021](docs/adr/0021-major-history-requires-code-and-name.md) | 专业级历史匹配要求代码与名称同时一致 |
| [0022](docs/adr/0022-percentile-boundary-half-credit.md) | 概率使用原始百分位，临界年份按半权重 |
| [0023](docs/adr/0023-invite-only-account-authentication.md) | 受控邀请注册、用户名密码与无联系方式认证 |
| [0024](docs/adr/0024-authenticated-report-and-saved-wishlist-retention.md) | 已保存志愿表长期保留，报告仅账号本人登录访问 |
| [0025](docs/adr/0025-remove-account-data-export.md) | 移除面试范围外、低价值的账户原始数据导出 |
| [0026](docs/adr/0026-remove-user-visible-login-device-management.md) | 移除用户可见登录设备管理，保留底层会话撤销机制 |
| [0027](docs/adr/0027-single-year-history-cannot-be-stable.md) | 已被 ADR-0028 取代：单年历史不得强制降档 |
| [0028](docs/adr/0028-probability-tier-is-separate-from-history-depth.md) | 概率分档与历史覆盖深度分离，单年强命中仍可为稳档并提示样本有限 |
| [0029](docs/adr/0029-major-renumbering-uses-exact-name.md) | 专业跨年重新编号时按映射组内精确名称匹配历史 |
| [0030](docs/adr/0030-wishlist-group-selection-and-draft-risk.md) | 志愿草稿支持专业组移出、重新加入与重排，并重算草稿风险摘要 |
