# Gap Analysis：PRD 需求 × 代码现状

| 项 | 值 |
| --- | --- |
| 文档版本 | v1.0 |
| 目的 | 把 `prd.md` 的 24 条 FR 逐条对照**真实代码**，给「现状 / 证据文件 / 剩余工作」，并合并 `agent-architecture-review.md` 的 19 项问题，作为拆票与 Codex 上下文的事实底座 |
| 基准 | 现状以**仓库代码实测**为准；目标态以 `prd.md` **v1.3** 为准（已修正矛盾与发布范围、为目标态唯一权威）；决策理由以 `docs/adr/` 为准；`agent-architecture-review.md` 已降级为历史存档 |
| 方法 | 通读 gateway / orchestrator / algo / llmgw / rag / migrations 源码，grep 调用点与表名，不轻信任何登记文本（含审查报告本身） |
| 生成时间 | 2026-11-18 |

> 本文档只做**只读分析**。原 6 项开放决策已于 2026-11-18 全部拍板，集中在 §6，正式记录见 `docs/adr/0001`~`0006`。

---

## 0. 图例

| 标记 | 含义 |
| --- | --- |
| ✅ 已实现 | 代码已落地且可独立验证（多为 svc-algo 与离线 RAG） |
| 🟡 部分 | 有骨架 / 离线可用 / 契约已定，但未接线或未覆盖全部口径 |
| ⛔ 缺失 | 仅有 TODO 桩或完全没有 |
| 🔵 设计目标 | PRD 已按审查回填为需求，代码尚未实现（默认态，不得对外声称为已具备能力） |

四服务一句话快照：

- **svc-algo（Go）**：既有四 RPC 全部实现（`RankConvert / EstimateProb / CheckConstraints / OptimizeWishlist`）；`EvaluateEligibility` 已具备 release-bound 规则、生命周期、覆盖声明、受限 AST、条件字段 typed facts，以及专业五态与组级 PASS_PARTIAL 汇总。薄 service + biz usecase 分层，仍是当前最成体系的已实现能力。
- **svc-orchestrator（Python/LangGraph）**：🟡 RunLifecycle 已提供持久 run/interrupt/租约与受控算法预览，`RetrievePolicy` 与在线检索节点已接线；自然语言画像、完整 POLICY_QA 子图、Strategy/Critic/report 等业务节点仍待实现，未调用 llmgw。
- **svc-gateway（Go）**：⛔ 三个入口方法（`CreatePlan / ConfirmProfile / BackfillProfile`）全部 `return errors.New("TODO")`。
- **svc-llm-gateway（Go）**：🟡 M5a 治理 seam 已具备可信 principal、session/run/release 校验、封闭 M5a 角色与消息协议白名单、release-bound route/模型白名单/价格、注册 prompt template 与 response schema、结构化输出校验、六类数据分类、原子 token/cost 硬预算、显式可信 usage、主备 attempt、未知费用对账、release-bound 结果缓存及类型化 `Chat/StreamChat`；普通与流式调用共享同一治理用例，`START` 发送失败按明确未计费收口，超限先于缓存，流内序号、稳定 ERROR、缓存回放与断流 `STREAM_INTERRUPTED` 已有真实 PG 测试。强结构化查询走独立 `internal/llmgw/data/queries` + sqlc 包。生产 provider 仍 fail closed，尚未接线真实付费模型、账单 adapter 或 orchestrator 入口。

---

## 1. FR × 现状矩阵

> 「证据」列给出实测文件与行号；「关联」列指向审查报告问题编号（见 §2）。

### 1.1 交互与任务底座（FR-01 ~ FR-07）

| FR | 现状 | 证据（文件:行） | 剩余工作 | 关联 | 里程碑 |
| --- | --- | --- | --- | --- | --- |
| FR-01 对话式画像采集（P0） | 🟡 | `graph/state.py:83` 确定性闸门 `check_completeness` 已实现；`graph/nodes/intake.py:12` 节点为 TODO 桩（无 LLM 解析、无 interrupt 追问） | 接 llmgw `role=profile` 解析 user_text→profile；缺项生成追问并 `interrupt()`；字段口径改 `category`（非 track）、主输入仅分数；TypedDict 换运行时校验（Pydantic） | P1-02, P1-14, §9.2 | M2/M3 |
| FR-02 画像确认 HITL（P0） | 🟡 | `migrations/09_profile_run_lifecycle.sql`、`app/run_lifecycle` 已实现不可变画像 revision、确认卡 interrupt 与 ResumeRun；旧 gateway `ConfirmProfile` 仍未接新命令 | 接 gateway 适配器与前端画像卡；旧 `ResumeGraph` 收口到 RunLifecycle | P1-03, P1-13, §9.1 | M2 |
| FR-03 条件必需按需补问（P0） | 🟡 有冲突 | `graph/nodes/check_conditional.py:15` 现实现为「问所有条件字段」，`:13` TODO 注明应由候选硬要求驱动 | 改为候选驱动（候选无体检限制则不问体检）；接 backfill interrupt；UNKNOWN 不放行 | P1-02 | M2 |
| FR-04 多轮修正与协商探索（P1） | ⛔ | 无对应节点；`build.py:38` 标注 strategy/critic/... 全 TODO | REVISE_PLAN 新建 run；画像修正后派生数据失效一致性 | P1-13 | M6 |
| FR-05 SSE 流式输出（P0） | ✅ RunLifecycle 侧 | `WatchRun` 提供持久 `event_seq`、typed progress/interrupt/result/error、游标补发和断线重订；gateway 已接 RunLifecycle 命令 | HTTP SSE 具体响应适配与前端重连仍属后续接线 | P1-08 | M2 已接 |
| FR-06 任务状态机与超时（P0） | ✅ RunLifecycle 侧 | `app/run_lifecycle` 实现 QUEUED / RUNNING / WAITING_HITL / 终态、interrupt、租约 epoch、执行/HITL 时限与取消竞态；gateway 旧入口转同一命令 | 生产容量与外部队列扩展不在 M2 最小底座 | P1-01(生命周期), P1-10, P2-01, §9.1 | M2 已接 |
| FR-07 幂等与断点续跑（P0） | ✅ RunLifecycle 侧 | 官方 `langgraph-checkpoint-postgres` 承载画像→算法预览业务图与内部执行闸门；receipt/interrupt 原子恢复，租约 fencing 防旧 worker 写入 | 更大吞吐调度后续演进 | P1-09, P1-10, §6.3 | M2 已接 |

### 1.2 检索与知识（FR-08 ~ FR-11）

| FR | 现状 | 证据（文件:行） | 剩余工作 | 关联 | 里程碑 |
| --- | --- | --- | --- | --- | --- |
| FR-08 结构化/非结构化分流检索（P0） | 🟡 在线 keyword / vector / hybrid + 可选 rerank，POLICY_QA 浏览器证据查看已接 | 唯一主 seam `rag/policy_retrieval.py::RetrievePolicy` 已实现 release 可用性校验、release/候选共享只读快照、`UNSUPPORTED_SCOPE`/`NO_EVIDENCE`、省份/科类/批次/学校/文档类型/有效区间 SQL 共同硬过滤、pg_trgm 关键词路、pgvector 余弦向量路、`RRF(k=60)` 与稳定证据投影；学校筛选保留 `school_id=NULL` 的全国/省级政策，校级文档缺学校身份或全部候选不可投影时统一返回 `NO_EVIDENCE`；hybrid 对单路技术故障按同范围另一路降级并给出稳定 `degraded_reason`，两路均不可用返回 `RETRIEVAL_UNAVAILABLE`；向量查询以有界 `ann_candidates` 直接触发 `idx_chunk_embedding` HNSW 距离访问，并用 `strict_order` 迭代扫描防止域外高相似度候选耗尽；release 可在 RRF 池内启用版本化 rerank；`RunLifecycle` 接受 typed POLICY_QA 并发布 `POLICY_EVIDENCE`，gateway `/v1/policy-queries` 与前端证据卡展示 source/hash/locator/release；验收手册见 `docs/m4a-frontend-acceptance.md` | 最终政策解释、golden set 真实签字基线 | P1-04, P1-13, §9.2 | M4 进行中 |
| FR-09 非结构化知识问答（P1） | ⛔ | `build.py:46` `check_conditional → END`，无 POLICY_QA 子图 | 子图路由（POLICY_QA/GENERATE_PLAN/EXPLAIN_RESULT/REVISE_PLAN）；依赖 FR-08 在线化 | P2-02 | M6 |
| FR-10 离线数据采集与索引管线（P0） | 🟡 | `rag/etl.py`(376 行) + `scripts/ingest_charters.py` 存在；`docs/charters/2025/` 有 5700+ 院校 txt/json 原料；`migrations/15_policy_retrieval_release_scope.sql` 已补齐 policy 文档类型、发布者、学校、抓取时间、原件来源、文档/chunk hash、字符定位、embedding/index 身份和 release→文档/chunk set 映射，`migrations/16_policy_vector_release_identity.sql` 已补 release 级索引身份，`migrations/17_policy_rerank_release_identity.sql` 已补 release 级 rerank 启用状态/模型/配置/超时身份，并标注 `charter_*` 废弃 | ETL 尚未写新 metadata/release/index/rerank 配置，已有 policy 数据需离线修复后才会进入在线结果；四层质检与正式 release 发布流程仍待接入 | P2-03 | M4 进行中 |
| FR-11 ETL 四层质检防线（P0） | 🟡 | `scripts/qa_check.py` 存在 | 按 FR-11 四层（结构/语义/交叉/抽样）逐层核实；`etl_run_id` 回溯链 | P1-01 | M4 |

### 1.3 算法与方案（FR-12 ~ FR-17）

| FR | 现状 | 证据（文件:行） | 剩余工作 | 关联 | 里程碑 |
| --- | --- | --- | --- | --- | --- |
| FR-12 冲/稳/保方案生成（P0） | 🟡 确定性正式报告已接线 | `internal/algo/service/algo.go:54` `EstimateProb` 从分数生成整池；RunLifecycle 在确认画像后按 RankConvert → EstimateProb → EvaluateEligibility → OptimizeWishlist 执行，只有 PASS/PASS_PARTIAL 组进入优化。M5b-01 将候选、概率、位次、样本年份、资格状态和必需说明构造成最终 Claims/EvidencePackage，经 `VerifyPackage` 后自动发布 `V1_FORMAL` | LLM 叙事、版本化 golden set、Eval/SLO 与删除安全门禁仍由 M5b 后续 tickets 完成；Strategy 仍不可替换候选身份或概率 | P1-14 | M3/M4b/M5b-01 已接 |
| FR-13 硬约束校验（P0） | ✅ 结构化资格闭环 | `EvaluateEligibility` 已提供 release-bound 规则快照、FULL/PARTIAL/NONE 覆盖、证据引用和专业五态；迁移 21 建立 `rule_version / rule_coverage`。迁移 22 增加 DRAFT→REVIEWED→PUBLISHED→REVOKED、内容不可变、状态审计、发布覆盖/重叠冲突门禁和撤销失效；迁移 23 增加 AST v1、Kleene 三值逻辑与资源上限；迁移 24 增加 typed facts 字段目录；迁移 25 增加规范 scope、来源完整性与覆盖冻结；迁移 26 将字段目录冻结为 release-scoped 快照。规则进入 REVIEWED/PUBLISHED 前执行与运行时相同的 AST、allowlist、literal 类型和来源证据静态校验。`EstimateProb` 输出报考年计划成员标记，RunLifecycle 不再无条件把所有历史专业视为当年计划。组级已按 PASS / PASS_PARTIAL / FAIL / UNKNOWN 汇总；所有声明的 PARTIAL/NONE 覆盖都会阻止 V1_FORMAL，PARTIAL/UNKNOWN/MISSING/BLOCKED 明确门禁。旧 `CheckConstraints` 保留兼容，正式路径不再使用 | 后续仍需真实规则内容发布和报告 Verifier；这些不计入本能力伪造通过 | P1-01, P1-15 | M4b 已接 |
| FR-14 位次换算（P0） | ✅ 正向 | `algo.go:36` `RankConvert` score→rank；RunLifecycle 传入 run 绑定 `release_id` 并校验 reply rank 与 EstimateProb `AuditBasis` 一致 | 仅正向（符合 R7，rank-only 不反推）；**`RankConvertReply` 无控制线**，而 `score_rank_map.control_score` 在库（`00_schema.sql:223`）——可补透出 | §2(控制线未透出) | M3 已接 |
| FR-15 录取概率估计（P0） | ✅ 正式事实闭环 | `algo.proto:84` `ProbEstimate` method=`weighted_hit`、clamp `[0.01,0.99]`；RunLifecycle 校验概率范围、候选身份、重复/漂移与执行时限，并把最终概率、单位和样本年份以 `DERIVED` EvidenceRef 送入正式报告门禁 | 置信区间仍为 P2；概率与滑档率仍只能作为近似估计，不能写成录取保证 | P1-05, P1-15 | M3/M5b-01 已接 |
| FR-16 方案自我挑战 Critic（P1） | 🟡 有偏差 | `build.py:17-19` `MAX_ASK_ROUNDS=6` ✅、`MAX_VERIFY_RETRIES=2` ✅、`MAX_CRITIC_LOOPS=2` 已与 PRD v1.3 对齐；Critic 节点仍未实现 | 实现已优化方案上的结构化审查、无进展检测与 P1 发布门禁；硬错误不得由 Critic 豁免 | P1-02, P1-03 | M2/M6 |
| FR-17 志愿表优化（P0） | 🟡 确定性正式报告已接线 | `algo.go:86` `OptimizeWishlist`；RunLifecycle 只把 `EvaluateEligibility` 的 PASS/PASS_PARTIAL 候选组送入优化，并把最终顺序、候选身份和概率写入确定性报告与 `V1_FORMAL` 结果 | LLM 叙事与 Eval/SLO 门禁仍由 M5b 后续 tickets 完成；(P2)投档模拟未做 | P1-14 | M3/M4b/M5b-01 已接 |

### 1.4 生成、校验与治理（FR-18 ~ FR-24）

| FR | 现状 | 证据（文件:行） | 剩余工作 | 关联 | 里程碑 |
| --- | --- | --- | --- | --- | --- |
| FR-18 报告生成（P0） | 🟡 受控报告闭环 | M5b-01 在确认画像后由 worker 自动生成版本化 `m5b-report-v1` 报告：位次、候选院校专业组、专业、概率、最终排名、资格状态、样本年份、风险提示与免责声明由确定性模板渲染；M5b-02 接入 `role=report` 的 LLMGateway 叙事 adapter，只允许注册 schema 的概述与逐候选说明，拒绝数值、候选/证据身份、模板控制符和录取承诺；非关键未知叙事按 `stripped_claim_ids` 剥离，模型不可用时明确标记确定性降级，文本重写上限 2 次；报告经 RESULT/GetRun 返回 | 正式 release 仍需发布对应 report route、prompt template、response schema 与价格配置；面向用户的报告编辑器不在范围 | P1-05 | M5b-01/M5b-02 已接 |
| FR-19 事实校验 Verifier（P0） | 🟡 报告与正式发布闭环已接 | `services/orchestrator/evidence/verifier.py` 保留唯一 `VerifyPackage` seam；内部 `claims / formal_gate / normalization / models` 分离三类 Claim、正式门禁、规范值与运行时模型。M5b-01 从最终报告事实构造 `DERIVED` 证据与 release-bound 必需说明，`RunLifecycle.PublishFormalResult` 绑定 `run_formal_gate_artifact` 后原子发布不可变结果、已剥离证据包、完整报告与 ACTIVE 状态；GET/Watch 返回同一 `verification_report`、必需说明和报告正文；发布前复核主动执行额度。真实 PG 覆盖 PASS、PARTIAL、BLOCKED、伪造资格、重试冲突、release 撤销、发布/取消竞态和执行超时 | LLM 驱动的受控叙事、非关键内容剥离策略细化由 M5b-02 完成；真实文档/结构化证据仍取决于检索与规则发布前置 | P1-05 | M4/M5b-01 |
| FR-20 LLM 治理与成本闸门（P0） | ✅ M5a 治理 seam | `api/llmgw/v1/llmgw.proto` 保留 `ChatDelta.content=1/done=2`，新增 `event=3` 与 typed `ChatStreamEvent`；类型化字段覆盖流内 sequence、START/DELTA/USAGE/DONE/ERROR、logical/attempt/model、usage/cost、finish reason、schema 校验状态、structured output、ledger、route/price/release 身份和安全错误 metadata。`internal/llmgw/orchestration.go::executeGovernedCall` 是 `Chat/StreamChat` 共用的唯一治理用例；普通调用与 DONE 的最终内容、usage、cost、attempt 归属和稳定错误语义一致。usage 只有显式 `TRUSTED` 才结算；M5a 角色与调用方消息 role 均为封闭白名单，敏感扫描覆盖 role、模板渲染与 JSON 转义。`START` 在预占后发送失败时原子转换 `FAILED_NO_CHARGE`、释放预算/缓存并追加投影；生产 fail-closed provider 明确未受理；预算超限先于缓存并阻止新 logical call。`internal/llmgw/data/queries` → `internal/llmgw/dbgen` 独立 sqlc 包承载强结构化读写，pgx 只保留事务编排。`internal/llmgw/stream.go` 只在预算预占成功发送 START，缓存/幂等完成态按 DELTA/DONE 回放且不新增 attempt，预占前拒绝只发送 ERROR。客户端在可能受理后断开时，`markProviderUnknownWithReason` 使用独立持久化上下文记录 `STREAM_INTERRUPTED / PENDING_RECONCILIATION / PARTIAL_UNKNOWN` 并保留预占。`migrations/29` 至 `35` 提供全部权威账本与缓存对象。真实 PG 生成客户端测试覆盖正常/错误流、usage 状态、角色/JSON 转义、START 失败、生产 fail-closed、超限缓存、结构化 DONE、旧字段 wire 兼容和普通/流式最终语义等价；完整 LLM 集成回归与 `00/07→35` / `28→29…→35` 迁移通过 | 真实 provider/账单 adapter、orchestrator 入口接线与真实付费模型质量/费用核验；流式 DELTA 仍未实时转发供应商 token | P1-11, P1-12 | M5a seam 完成，生产接线待后续 |
| FR-21 限流与并发保护（P0） | ⛔ | `internal/gateway/service.go:16` 注释「限流器」为 TODO(M2) | 令牌桶限流 + 并发闸门（超限排队非 500）；公网发布前置 | P1-08 | M2/M4 |
| FR-22 降级与故障处理（P0） | 🟡 consent、报告降级、删除与保留已接 | M5b-05 新增 principal + purpose + data class + policy version 绑定的 `consent_record`，敏感字段缺失/撤销/不匹配时 fail closed，业务事件脱敏。M5b-06 以幂等 tombstone/ledger 即时封锁删除主体并清理私有载荷。M5b-07 新增版本化 7/30/180 天保留策略：终态结果/Profile/run 元数据与检查点按 30 天，事件/回执/失败检查点按 7 天，provider 账本与安全审计按 180 天去标识化；策略缺失 fail closed，部分失败可重试并审计。M5b-02 对报告模型超时、限流、schema 缺失和不可用使用明确标注的确定性降级；事实越界立即阻断 | 备份恢复 tombstone 重放与 Redis 投影验证由 M5b-08 完成 | P1-03, P1-04, P1-06 | M5b-02/05/06/07 |
| FR-23 评测与发布门禁（P0） | 🟡 联合 EvalDataset、SLO/预算门禁已接，待签字与真实基线 | `data/golden/evaluation-hunan-2025-v0.2.0-draft/` 以不可变 `qa.jsonl + cases.jsonl + manifest.json` 固定 31 条检索 case 与 10 条端到端 case，并显式声明每条 case 的 driver、条件字段与取消竞争终局。联合 loader 校验双文件 hash、组合 dataset hash、三类 count、`case_count`、immutable 和必备场景，`PENDING_SIGNOFF` 只能生成诊断结果。`eval.run_lifecycle_runner` 从 `SubmitRun → ResumeRun → GetRun` 执行全部 10 条 case；`eval.e2e_eval` 只统计最终保留 Claims 的 citation/accuracy/contradiction/critical unverifiable/hallucination/illegal candidate 指标。M5b-04 `eval.release_gate` 合并签字数据、case gate、2 个百分点质量回归、100+ 样本 active-execution P99、600000ms 硬超时、10% P99 回归、session token/成本、预算违规与待对账状态，输出 failed_metrics/actual/threshold/baseline；`eval.run_telemetry` 直接从 run 与 LLMGateway budget scope/attempt 账本采集预算样本。检索指标继续复用 `RetrievePolicy` evaluator | Francis 对联合数据集签字并运行真实检索 baseline；真实 provider/性能环境尚未产出签字 EvalRun | P2-04 | M5b-03/M5b-04 已接，待签字 |
| FR-24 可观测（P1） | 🟡 | `common.v1.Trace` 贯穿各 proto 请求；`llm_call_log` 有 trace_id/latency/cost | OTel trace_id 贯通 Go↔Python；面板指标（QPS/P50P99/节点耗时/错误率/缓存命中/循环分布/幻觉率） | P1-08 | M6 |

---

## 2. 审查 19 项问题 → 回填状态

> 「PRD 回填」= 该问题结论是否已写进 PRD 正文（v1.1 逐条回填、v1.2 吸收审查全文、v1.3 修正矛盾与发布范围）；「代码收口」= 是否已在代码层解决。**全部 19 项代码层均未收口**（除 P1-14 算法接口已按新口径实现）。
>
> 编号兼容：本节沿用历史审查编号 `P1-xx / P2-xx`；PRD v1.3 为避免与功能优先级混淆，对外写作 `REVIEW-P1-xx / REVIEW-P2-xx`。

| 编号 | 问题 | 关联 FR | PRD 回填 | 代码收口 |
| --- | --- | --- | --- | --- |
| P1-01 | 硬规则只有消费端，无生产发布链路 | FR-13/FR-10/FR-11 | ✅ R6 | ⛔ |
| P1-02 | 必需字段与「到上限就低置信继续」冲突 | FR-01/FR-03/FR-16 | ✅ R7 | ⛔ `build.py:26` 仍静默放行 |
| P1-03 | 降级/循环上限可能绕过正确性门禁 | FR-16/FR-19/FR-22 | ✅ | ⛔ |
| P1-04 | 检索不足时放宽硬过滤会改变问题本身 | FR-08/FR-22 | ✅ | ⛔ |
| P1-05 | 「每条结论溯源原文」无法验证派生数值 | FR-19 | ✅ R8(三类证据) | ⛔ |
| P1-06 | 隐私目标与健康画像/永久保存冲突 | FR-22/§7.4 | ✅ R9 | ⛔ |
| P1-07 | 图控制权在 gateway 与 LangGraph 间分裂 | §8/FR-09 | ✅ R13 | 🟡 gateway 旧入口已转同一 RunLifecycle；策略子图仍属 M6 |
| P1-08 | SSE 连接承担了任务生命周期 | FR-05/FR-06 | ✅ R3 | ✅ RunLifecycle 事件游标与持久状态不依赖订阅连接 |
| P1-09 | 有检查点≠有并发安全执行权 | FR-07 | ✅(租约/fencing) | ✅ worker 租约 + epoch fencing 已接 |
| P1-10 | 按 session 去重吞掉合法第二次回答 | FR-06/FR-07 | ✅ R3(run/interrupt) | ⛔ |
| P1-11 | 缓存 data_version 非跨服务一致快照 | FR-20 | ✅ R10(release_id) | ✅ route/price、logical call 与结果缓存均绑定不可变 `release_id`；缓存键覆盖治理维度，release 撤销会失效并清除结果 |
| P1-12 | 硬预算缺原子预占及重试账本 | FR-20 | ✅ R4 | ✅ principal + session 跨 run 原子预占/结算、真实 PG 并发、同 ID 仲裁、两次 attempt 上限、主备模型、逐 attempt 成本、未知费用对账和流式断流待对账均已覆盖 |
| P1-13 | 画像修正后回边与派生数据失效不一致 | FR-04/FR-13 | ✅(REVISE_PLAN 新建 run) | ⛔ |
| P1-14 | 原文算法流程与当前接口无法接合 | FR-12/FR-14/FR-17 | ✅ R1/R7 | 🟡 RunLifecycle 已接四 RPC 并产出 ALGORITHM_PREVIEW；正式全链路仍待 M4/M5 |
| P1-15 | 历史专业组数据不能直接作当年资格 | FR-13/FR-15 | ✅(标设计目标) | ⛔ 数据边界 |
| P2-01 | HITL 过期和恢复规则不自洽 | FR-06 | ✅ R3 | ⛔ |
| P2-02 | 政策问答/解释/修改缺独立路由 | FR-09 | ✅ R14 | ⛔ |
| P2-03 | 概念 schema/脚本/契约草案混作实施规格 | 全局 | ✅(本文档即回应) | 🟡 本 gap-analysis 收口中 |
| P2-04 | 技术选择与性能成本表述过度确定 | 全局/FR-23 | ✅(降级表述) | ⛔ |

---

## 3. 已实现、可直接复用的地基

拆票时应把这些当**既有能力**，不重复造：

1. **svc-algo 四 RPC**（`internal/algo/service/algo.go` + `biz/{rank,prob,constraint,optimize}`）：分数→位次、候选池生成+概率、选科硬约束、志愿表优化+滑档率。契约见 `api/algo/v1/algo.proto`，类型化 message（非 opaque JSON）。
2. **离线混合检索器** `rag/retrieve.py::HybridRetriever`：向量+trgm 双路 RRF，SQL 参数化，确定性 tie-break。
3. **确定性完整性闸门** `graph/state.py::check_completeness`：不依赖 LLM 判断「够没够」。
4. **LangGraph 图骨架** `graph/build.py`：entry→intake→profile_confirm→retrieve→check_conditional，条件边 + 循环护栏常量已就位。
5. **数据库 schema** `migrations/00_schema.sql`：13 张表（university/major/enrollment_plan/major_admission/school_admission/score_rank_map/charter_doc/charter_chunk/chat_session/plan_checkpoint/llm_call_log/eval_run/data_version），pgvector + pg_trgm 扩展、HNSW + GIN 索引齐备。
6. **数据资产** `docs/charters/2025/`：5700+ 院校章程 txt/json 原料。
7. **proto 单一事实来源** `api/{algo,gateway,llmgw,orchestrator,common}/v1`：Go/Python 双端生成（`make api`）。

---

## 4. 阻塞级缺口（按严重度）

### 4.1 阻塞第一行代码（schema/契约与 PRD 模型不匹配）

| # | 缺口 | 证据 | 影响 |
| --- | --- | --- | --- |
| B1 | **会话模型 vs run/interrupt 模型** | `run / interrupt / profile_revision / receipt / event / result` 迁移与 RunLifecycle 已落地 | 已收口 |
| B2 | **缓存键 data_version（可变）vs release_id（不可变）** | `release`、`release_component` 与公开数据结构化表 `release_id` 已落地，algo 查询实际按 release 过滤 | 已收口；后续 ETL 需显式生成 release |
| B3 | **LangGraph saver 未定** | 官方 `langgraph-checkpoint-postgres` 已接，业务图包含画像、内部执行闸门和算法预览节点 | 已收口 |
| B4 | **profile_version 字段缺失** | `chat_session` 无该列 | 画像修正→派生失效一致性（P1-13）无锚点 |
| B5 | **表名 charter_* / policy_* 双轨** | `00_schema.sql:272` charter_chunk vs `07_policy_chunk_trgm.sql:30` policy_chunk；`retrieve.py` 查 policy_chunk | fresh 环境检索可能查空表；须收口为一套 |

### 4.2 搭骨架即撞上（口径/护栏偏差）

| # | 缺口 | 证据 | 影响 |
| --- | --- | --- | --- |
| C1 | 字段口径 `track` vs `category` | `state.py:12` GLOBAL_REQUIRED 用 track；proto/DB 用 category | 画像→algo 入参对不上（§9.2） |
| C2 | `score_or_rank` 接受 rank-only | `state.py:12,90`；但 FR-14/proto 仅支持 score→rank | 只给位次的请求会被闸门放行却无接口处理（P1-14/R7） |
| C3 | Critic 上限已对齐 | `build.py:18` `MAX_CRITIC_LOOPS=2`；PRD v1.3 §5.2 / FR-16 / §7.2 均为 2 | 规格口径已统一，不再是阻塞项 |
| C4 | route_after_intake 到上限静默放行 | `build.py:26-27` | P1-02 冲突源：带缺失继续却标低置信，绕过门禁 |
| C5 | 画像无运行时校验 | `state.py:29` TypedDict（仅类型提示） | 非法画像不会被拦截（P2-03） |

### 4.3 写代码前要定（接口/字段补齐）

- D1 `RankConvertReply` 补控制线透出（DB 有 `control_score`，proto 无字段）。
- D2 `UserProfile`（`algo.proto:35`）仅含偏好维度，不含 province/year/score/health——orchestrator 构造入参时须明确「完整画像」与「algo UserProfile」的映射边界。
- D3 所有跨服务 `*_json` / payload 的 JSON schema 约定（命名风格、struct 映射）——proto 已类型化的部分除外。
- D4 基准年策略：请求传考试年（如 2026）而数据止于 2025 时的取数口径（窗口=[year-W, year-1]）。

---

## 5. 建议施工顺序（对齐审查 §10.1 A→F / PRD §9 M1~M6）

> 算法闭环前置（四 RPC 已实现），先形成受控最小闭环再加智能增强。

| 阶段 | 里程碑 | 首批可开票（依赖 §6 决策已定） | 阻塞前置 |
| --- | --- | --- | --- |
| A 规格对齐 | M1 | 已建立 Profile / Run / Retrieval / Rules / Evidence / LLM Gateway / Eval-SLO / Security-Retention 八份 runtime 契约（后两份待确认）；下一步制定 proto/DB 变更计划并登记 issue | 无 |
| B 任务底座 | M2 | schema 迁移（B1/B2/B4）；选定并接入 LangGraph saver（B3）；gateway 三入口 + SSE 事件分型；run 状态机 + 幂等恢复 | 决策 2（saver）、决策 4（release_id 形态） |
| C 算法闭环 | M3 | orchestrator 接 algo 四 RPC（FR-12/14/15/17）；修 C1~C5 口径/护栏；受控 ALGORITHM_PREVIEW | 决策 1（分词，影响取数）、B5 表名收口 |
| D 生产资格与证据 | M4 | ETL 四层质检 + 双路检索在线化 + rerank；当年规则发布链路（FR-13）；三类证据校验（FR-19）；golden set + CI 门禁（FR-23） | 决策 1（分词）、决策 5（golden set 标注归属） |
| E 模型治理与表达 | M5 | llmgw 路由/缓存/原子预占结算（FR-20）；报告生成 + 失败模板（FR-18）；隐私脱敏（FR-22） | 决策 3（LLM 路径/模型/key） |
| F 智能增强 | M6 | Strategy/Critic/政策问答/解释·修订子图（FR-04/09/16）；全链路 OTel（FR-24）；压测 | E 完成 |

**当前可启动**：确认五份 runtime 契约后，建立 proto/DB 变更草案，并把本文档 §4 的 B/C/D 缺口逐条登记为 GitHub Issue（用 `docs/agents/issue-tracker.md` 的 native blocking 依赖链接编码 A→F 顺序）。

---

## 6. 已拍板决策（2026-11-18，Francis 选定）

> 以下 6 项原为「开放决策」，现已全部拍板，作为 M1~M6 施工前提；后续改动须回到本表更新。
> 正式 ADR 记录见 `docs/adr/0001`~`0006`（与本表决策 1~6 一一对应）。

| # | 决策 | ✅ 决议 | 阻塞阶段 | 出处 |
| --- | --- | --- | --- | --- |
| 1 | 中文全文分词选型 | **维持 pg_trgm + 向量主导，推迟 zhparser**；M4 建 golden set 量化出关键词路是瓶颈后再评估（与「RAG 分阶段演进策略」阶段三推迟一致）。否决：现在上 zhparser / pg_bigm（需自定义 PG 镜像 + 全量重建 tsv，且无 golden set 无法量化收益） | C/D | PRD 风险#2、审查 §9.2、`retrieve.py:6` |
| 2 | LangGraph saver 选型 | **官方 `langgraph-checkpoint-postgres`**（自带 interrupt/resume + pending writes + 并发语义，覆盖 FR-07 与 P1-09 执行权）；`plan_checkpoint` 降级为业务审计快照。否决：改造自定义表（审查 §6.3-4 点名风险高） | B | 审查 §6.3-4、`build.py:48` |
| 3 | 早期 LLM 模型 | **DeepSeek（deepseek-v4-pro）**；早期 Python 直连 = 实验链路，M5 收口 llmgw 网关；key 只放 `.env` 不入 Git；模型 id 接线时对齐 DeepSeek API 文档。M2 任务底座用测试替身即可，不需真模型 | E（M3 起） | PRD 风险#3、§8、`internal/llmgw` |
| 4 | release_id 形态 | **内容哈希不可变 `release` 表**（release_id = 各数据源版本清单 SHA256 前缀）；run 创建时绑定，缓存键 = `prompt_hash + release_id + model`；各读取端按 release_id 选数据、禁原地覆盖；`data_version` 保留作迁移过渡。根治 P1-11 | B/D | PRD R10、审查 P1-11、`00_schema.sql:409` |
| 5 | golden set 标注归属 | **Francis 最终定标、Agent 出草稿 + 跑评测 + 出 diff 报告**；首批检索集 30 条（真实章程「问题→应命中 chunk/院校」）+ 端到端集 10 条（覆盖冲稳保 / 硬约束剔除 / 无可靠数据三类场景），后续迭代扩充 | D | PRD FR-23、`data/golden/README.md` |
| 6 | charter_* / policy_* 表名收口 | **收口到 `policy_*`（活表），废弃 `charter_*`（孤儿空表）**；零数据迁移——下个迁移 drop/标注 `charter_doc/charter_chunk`，`00_schema` 加注释指向 `policy_*`。否决：迁回 charter_*（需重建 1024 维向量库，无收益） | C/D | `00_schema.sql:272` vs `07_policy_chunk_trgm.sql:30` |

---

## 7. 证据索引（实测文件:行）

- `internal/gateway/service.go:16,24,30,36` — 三入口 TODO + 限流器 TODO
- `internal/llmgw/{rpc,orchestration,attempt_execution,validation,reservation,settlement,output_rejection,replay}.go` — 类型化 Chat/Stream 的 RPC 映射、治理编排、身份/路由校验、预占、attempt 执行、结算、输出拒绝、幂等重放与投影
- `internal/llmgw/data/queries` → `internal/llmgw/dbgen` — LLMGateway 独立 sqlc 查询与生成类型
- `internal/llmgw/classification.go` / `sensitive_data.go` — 六类分类与旧别名映射、route 准入、直接标识和敏感原文检测
- `internal/llmgw/schema.go` — 注册 JSON Schema 编译与结构化输出规范化校验
- `internal/llmgw/reconciliation.go` — 未知计费内部对账、幂等状态转换与超预留封锁
- `internal/llmgw/cache.go` — release-bound 缓存键、PostgreSQL miss claim、命中 logical call 与结果回填
- `internal/llmgw/*_integration_test.go` — 按 stream、contract、budget、reconciliation、migration、cache、idempotency、prompt/schema、sensitive data 分组的生成 client + gRPC + fake provider + 真实 PostgreSQL 外部行为测试
- `migrations/29_llm_gateway_chat.sql` 至 `35_llm_result_cache.sql` — 价格/预算/调用账本、超限封锁、attempt 上限、不可变 prompt/schema 注册、追加式对账审计、route 分类与结果缓存约束
- `internal/algo/service/algo.go:36,54,76,86` — 四 RPC 实现
- `api/algo/v1/algo.proto:35,84,117,140,146,153,159,178,193` — UserProfile/ProbEstimate/OptimizationReport/RejectedCandidate/RankConvert/EstimateProb/OptimizeWishlist 契约
- `services/orchestrator/app/server.py` — RunLifecycle 传输适配与 run-bound 检索构图入口
- `services/orchestrator/graph/build.py` — 护栏常量 / 条件边 / run-bound 检索节点注入
- `services/orchestrator/graph/state.py:12,29,66,83,90` — GLOBAL_REQUIRED / TypedDict / degraded / check_completeness / score_or_rank
- `services/orchestrator/graph/nodes/retrieve.py` — 仅经 `RetrievePolicy` 的在线检索节点；其余图业务节点仍待实现
- `services/orchestrator/rag/retrieve.py:6,24,160,201` — trgm 替代 tsvector / meta 过滤字段 / policy_chunk 查询
- `services/orchestrator/rag/policy_retrieval.py` — release-bound `RetrievePolicy` 唯一主 seam、硬过滤与文档证据投影
- `services/orchestrator/evidence/verifier.py` — 唯一 `VerifyPackage` seam、包级顺序与 `PASS/BLOCKED/PARTIAL` 状态机
- `services/orchestrator/app/run_lifecycle/report.py` — 确定性正式报告、最终 Claims、必需说明和同 release 证据包
- `services/orchestrator/app/data_lifecycle/deletion.py` — 幂等 `DeletePrincipalData`、即时访问封锁、最小 tombstone、资源 ledger 与私有数据清理/去标识化
- `services/orchestrator/app/data_lifecycle/restore.py` — 备份恢复全局闸门、可重建投影重置、tombstone/ledger 幂等重放与失败关闭
- `migrations/39_backup_restore_deletion_replay.sql` — 恢复闸门与不含敏感内容的删除重放审计
- `services/orchestrator/tests/test_data_deletion.py` — 删除成功/重放/部分失败重试、正式结果清理、公开 release 保留、私有载荷与敏感 reason 清理测试
- `migrations/36_principal_data_deletion.sql` — principal 删除状态、请求 receipt、资源 ledger、tombstone，以及仅限删除事务的不可变结果受控删除通道
- `services/orchestrator/app/data_lifecycle/retention.py` — 版本化 7/30/180 天保留、逐资源失败隔离、幂等重试与账本/审计去标识化
- `services/orchestrator/tests/test_data_retention.py` — 保留窗口、GetRun 不可见、公开 release 保留、策略缺失、部分失败、重复 sweep、provider 账本与安全审计去标识化测试
- `migrations/38_data_retention.sql` — 保留策略/sweep/ledger 与仅限保留事务的正式结果受控清理通道
- `services/orchestrator/evidence/claims.py` — 三类 Claim 内部校验适配器
- `services/orchestrator/evidence/formal_gate.py` — 资格、版本与必需说明的 `formal-gate-v1` 策略
- `services/orchestrator/evidence/normalization.py` / `canonical.py` — 数值、语义、hash 与样本年份规范值
- `services/orchestrator/tests/test_evidence_verification.py` — 三类证据、状态机、资格/版本/必需说明与录取承诺门禁行为测试
- `services/orchestrator/tests/test_policy_retrieval.py` — release/scope/适用域/hash/locator/NO_EVIDENCE、关键词与 pgvector、索引身份故障和确定性外部行为测试
- `data/golden/retrieval-hunan-2025-v0.2.0-draft/` — 30 条检索 Agent 草稿、可验证 interference 元数据、版本 manifest 与 case/dataset hash
- `services/orchestrator/eval/retrieval_eval.py` — golden set loader、`RetrievePolicy` 评测 runner、recall/MRR/scope leak/first diff/baseline
- `services/orchestrator/eval/eval_dataset.py` — 联合 `qa.jsonl + cases.jsonl + manifest.json` 不可变 loader、组合 hash、count/场景校验
- `services/orchestrator/eval/e2e_eval.py` — 最终 Claims 报告指标、pending-signoff 发布阻断、联合 EvalRun 身份与 first diff
- `services/orchestrator/eval/release_gate.py` — 签字数据、质量回归、P99、预算、可复现性与非法候选发布门禁及机器可读失败明细
- `services/orchestrator/eval/run_telemetry.py` — 从 run active execution 与 LLMGateway budget scope/attempt 权威账本采集 SLO 样本
- `services/orchestrator/eval/run_lifecycle_runner.py` — 从 RunLifecycle 主 seam 执行脚本化端到端 case
- `data/golden/evaluation-hunan-2025-v0.2.0-draft/` — 31 条检索 case + 10 条带显式 driver 的端到端 case 不可变草稿
- `services/orchestrator/tests/test_retrieval_eval.py` — hash/schema、指标分母、安全泄漏与 baseline 复现测试
- `scripts/run_retrieval_eval.py` — 实际 `RetrievePolicy` 与确定性 fixture 的 baseline 入口
- `migrations/00_schema.sql:223,272,306,314,333,353,385,409` — control_score / charter_chunk / chat_session / status / plan_checkpoint / llm_call_log / eval_run / data_version
- `migrations/07_policy_chunk_trgm.sql:19,30` — policy_doc / policy_chunk
- `migrations/15_policy_retrieval_release_scope.sql` — policy 适用域、`policy_chunk_set`、`release_policy_doc` 与检索索引
- `migrations/16_policy_vector_release_identity.sql` — release 级 policy embedding 模型、向量维度与 index version
- `docs/prd.md` **v1.3**（24 条 FR + §5.2 端到端 15 步 + §8 服务/契约/表映射/证据/旁路 + §9 里程碑 + §10 风险 + §12 验收总则 + §13 20 验收场景）
- `docs/agent-architecture-review.md`（**历史存档**，结论已吸收进 PRD v1.3；P1-01~P1-15 + P2-01~P2-04 + §6.3 + §9 + §10.1）
