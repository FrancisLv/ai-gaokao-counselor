# GLOSSARY

本仓库的**单一上下文术语表**（single-context，约定见 `docs/agents/domain.md`）。输出中涉及领域概念时（issue 标题、重构提案、假设、测试名），用此处定义的术语，不要漂移到「避免」列点名的同义词。`docs/prd.md §11` 术语表是本表的子集，**以本表为准**。

术语缺口是信号：要么你在发明项目不用的语言（请重考虑），要么是真实缺口（记下来交给 `/domain-modeling`）。

## 高考志愿领域术语

| 术语 | 定义 | 避免 / 备注 |
| --- | --- | --- |
| 冲 / 稳 / 保 | 按录取概率分层的志愿策略：冲刺 / 稳妥 / 保底 | — |
| 院校专业组 | 湖南省新高考平行志愿的投档单位（一组选科要求相同的专业），候选生成的基本单位 | 不要退化为「院校 + 专业」二元 |
| 选科组合（3+1+2） | 首选科目（物理 / 历史）+ 再选 2 门 | — |
| 科类（category） | 物理类 / 历史类（新高考 3+1+2）；位次与概率计算的必需过滤条件 | **避免 track**（track 是废弃表 admission_score 的旧字段名，活跃表统一用 category） |
| 批次（batch） | 招生录取批次；V1 仅支持本科批，缺失时使用本科批默认值并在画像确认卡展示 | 其他批次不属于 V1 支持范围 |
| 一分一段表 | 省考试院发布的分数 → 累计位次映射表（`score_rank_map`） | — |
| 位次（rank） | 考生在全省同科类的排名 | — |
| 百分位（percentile） | `rank / total_count` | — |
| 批次控制线（control_score） | 各批次录取最低控制分数 | — |
| 加权命中率法（weighted_hit） | 现行录取概率估计方法（详见 `docs/algorithms.md`） | **避免「分位数法 / 核密度」**（已废弃设计，非 V1 承诺） |
| 滑档率（slip_rate） | 平行志愿下全部未投档的解析式估算 Π(1−pᵢ) | 是近似估计，**非录取保证** |
| 期望满意度（expected_satisfaction） | 志愿表效用分均值 | — |
| 适用域（applicability） | 文档或规则适用的省份、科类、批次和生效区间 | 不是排序因素，必须在计算相关度前过滤 |
| 生效区间（valid interval） | 文档或规则开始、结束有效的日期范围；结束日期为空表示持续有效 | 年度章程不得只靠“当前年份”推断 |

## 系统 / 身份模型术语

| 术语 | 定义 | 避免 / 备注 |
| --- | --- | --- |
| principal（principal_id） | 经可信认证产生的用户或受控匿名主体；决定 session/run 的访问归属 | **避免用请求正文自称身份** |
| session（session_id） | 会话容器，可包含多个 run；不是恢复或幂等单位 | **避免以 session 代替 run** |
| run / interrupt | run = 一次任务执行（每次生成 / 修订新建）；interrupt = run 内一次人工中断。恢复与幂等以 run/interrupt 为单位 | **避免以 session 为恢复单位** |
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

## 已拍板决策索引（详见 `docs/adr/`）

| ADR | 决策 |
| --- | --- |
| [0001](docs/adr/0001-defer-zhparser-keep-trgm.md) | 维持 pg_trgm，推迟 zhparser |
| [0002](docs/adr/0002-langgraph-official-postgres-saver.md) | 官方 langgraph-checkpoint-postgres |
| [0003](docs/adr/0003-deepseek-early-llm-experimental.md) | 早期 DeepSeek 实验链路，M5 收口网关 |
| [0004](docs/adr/0004-immutable-content-hash-release-id.md) | 内容哈希不可变 release_id |
| [0005](docs/adr/0005-golden-set-human-labeled.md) | golden set 由 domain owner 定标 |
| [0006](docs/adr/0006-consolidate-policy-tables-drop-charter.md) | 收口 policy_*，废弃 charter_* |
