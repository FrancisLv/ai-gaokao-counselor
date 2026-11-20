# Evaluation and SLO Runtime Contract

| 项 | 内容 |
| --- | --- |
| 版本 | v1.0 |
| 状态 | 已确认；作为 M4/M5 评测与发布门禁基线 |
| 上游 | [PRD v1.3 §3、§7、§9、§12、§13](../prd.md)、[ADR-0005](../adr/0005-golden-set-human-labeled.md) |
| 关联 | [Evidence Contract](./evidence-contract.md)、[LLM Gateway Contract](./llm-gateway-contract.md)、[Run Lifecycle Contract](./run-lifecycle.md) |

## 1. 目标与边界

本规格定义 golden set、指标公式、基线、CI 门禁、性能统计口径和 `EvalRun` 记录。

它回答两个问题：

1. 系统是否比上一版更差；
2. 系统是否达到可发布的最低质量与安全要求。

不在本规格内：

- 具体模型和检索算法实现；
- 生产监控面板 UI；
- 线上流量计费和供应商结算；
- 法律合规认证。

## 2. 核心不变量

1. **评测集不可变。** 每次修改 golden set 生成新 `dataset_version` 和新内容 hash。
2. **评测输入必须固定。** 记录 git SHA、release、算法配置、模型路由、价格表和 verifier 版本。
3. **安全指标是绝对门禁。** 不允许用 recall 提升抵消越权、非法候选或引用错误。
4. **质量指标采用绝对阈值 + 相对基线。** 首次发布建立基线，后续不得显著退化。
5. **CI 不依赖付费模型。** 常规 CI 使用冻结替身和脚本化输出；真实模型评测单独运行并记录。
6. **报告只统计最终通过门禁的结果。** 被剥离的草稿内容不计作最终事实覆盖。
7. **测试必须从外部 seam 观察。** 指标基于接口输出、持久事件和证据包，不读取模块私有实现。
8. **P99 有统计条件。** 样本量、环境、硬件和模型配置必须随结果记录。

## 3. 评测产物

### 3.1 EvalDataset

```text
EvalDataset
  dataset_version: string
  dataset_sha256: string
  retrieval_cases_file: qa.jsonl
  e2e_cases_file: cases.jsonl
  manifest_file: manifest.json
  retrieval_cases_sha256: string
  e2e_cases_sha256: string
  owner: domain owner
  created_at
  immutable: true
```

首批版本：

- 检索集：30 条；
- 端到端集：10 条；
- 后续扩展目标：检索集 100–200 条。

修改任一条用例都必须创建新 dataset version，不能在原版本上覆盖。

### 3.2 RetrievalCase

```text
RetrievalCase
  case_id
  query
  scope: province / application_year / category / batch
  expected_chunk_ids: list<string>
  expected_doc_ids: list<string>
  must_exclude_sources: list<doc_id or school_id>
  must_exclude_chunk_ids: optional list<chunk_id>
  tags: policy / charter / physical_exam / no_evidence / scope_conflict
  criticality: CRITICAL | NORMAL
  evaluation_status: ACTIVE | PENDING_RELEASE_BINDING | PENDING_RELEASE_REBUILD | PENDING_SEMANTIC_RELEVANCE
```

首批 30 条必须覆盖：

- 院校章程条款；
- 全国或省级长期政策；
- 学校名称 + 专业政策的组合查询；
- 应返回 `NO_EVIDENCE` 的查询；
- 应排除跨省、跨年、跨科类证据的干扰用例。

### 3.3 EndToEndCase

```text
EndToEndCase
  case_id
  input_profile
  scripted_model_responses
  expected:
    result_kind
    terminal_status
    excluded_candidate_keys
    required_claim_types
    forbidden_text_patterns
    max_provider_cost_micro_usd
  tags
  criticality
```

首批 10 条至少覆盖：

- 正常冲稳保方案；
- 缺全局必需字段；
- 选科规则拒绝；
- 条件字段 UNKNOWN；
- 零召回/无可靠数据；
- 报告数字或引用被篡改；
- 画像确认后修改分数；
- 预算耗尽；
- worker 恢复；
- 取消与发布竞争。

### 3.4 EvalRun

```text
EvalRun
  eval_run_id
  dataset_version / dataset_sha256
  git_sha
  release_id
  algorithm_config_version
  model_route_version
  price_table_version
  verifier_version
  environment
  hardware_profile
  started_at / finished_at
  metrics
  gate_status: PASS | FAIL
  failed_case_ids
  baseline_eval_run_id
```

同一 `eval_run_id` 的指标和原始结果不可变。
PASS EvalRun 必须持久化 retrieval 逐例结果、E2E 最终 Claims/候选结果和 100+
run samples；门禁从原始记录重算汇总指标，不能只信任提交的 metrics JSON。

运行时把完成计算的 EvalRun 写入 insert-only `eval_run_record`；记录保存
`eval_run_id`、`result_sha256`、dataset/release/code 身份和完整 payload，并由数据库
触发器拒绝 UPDATE/DELETE。发布门禁只从该权威记录读取结果，不接受调用方临时构造的
`gate_status=PASS`。

## 4. 检索指标

设 `Q` 为 scope 有效的检索用例集合。

### 4.1 Recall@k

```text
recall@k =
  count(case where at least one expected_evidence_id appears in top-k)
  / count(eligible cases)
```

- 主门禁使用 `k=5`；
- `k=20` 作为诊断指标；
- 只有标记为 `NO_EVIDENCE` 的用例不进入 recall 分母；
- 返回错误 scope 的结果不计为命中。

若有效检索用例数为 0，评测无效并失败，不能产生 100% 或 N/A 通过。

### 4.2 MRR

```text
MRR = mean(1 / rank_of_first_expected_evidence)
```

- 未命中记为 0；
- 同分排序必须与检索契约一致；
- 多证据用例取第一次命中的排名。

### 4.3 Scope Leak Rate

```text
scope_leak_rate =
  count(cases with at least one out-of-scope evidence)
  / count(cases with at least one returned evidence)
```

必须为 `0`，不是可容忍的观测项。

### 4.4 Evidence Precision@5

```text
evidence_precision@5 =
  count(top-5 evidence items marked acceptable)
  / count(returned top-5 evidence items)
```

若没有返回证据，则该用例 precision 记 `0`。`irrelevant_evidence_rate =
1 - evidence_precision@5`。

`must_exclude_chunk_ids` 命中必须逐例失败，不能用 recall 平均值掩盖。正式质量门槛
必须使用 owner `SIGNED_OFF` 的数据集校准；草稿/fixture 只报告指标和 diff。

非 `ACTIVE` case 不调用检索器，不进入 recall/MRR/precision 分母；baseline 必须报告
`skipped_case_count` 和按状态分组的 `skipped_case_reasons`，不能静默省略。

## 5. 生成与事实指标

事实口径采用 [Evidence Contract](./evidence-contract.md) 的 `ClaimVerification`。

```text
factual_claims = claims with claim_type in DOCUMENT_FACT / STRUCTURED_FACT / DERIVED_FACT

citation_coverage =
  count(factual claims with at least one EvidenceRef)
  / count(factual claims)

citation_accuracy =
  count(VERIFIED factual claims)
  / count(factual claims with at least one EvidenceRef)

contradiction_rate =
  count(CONTRADICTED factual claims)
  / count(factual claims)

unverifiable_rate =
  count(UNVERIFIABLE factual claims)
  / count(factual claims)

hallucination_rate =
  count(CONTRADICTED or UNVERIFIABLE factual claims)
  / count(factual claims)
```

发布门禁：

- 最终报告 `citation_coverage = 100%`；
- 最终报告 `citation_accuracy = 100%`；
- `contradiction_rate = 0`；
- `CRITICAL` claim 的 `unverifiable_rate = 0`；
- 非关键无法验证内容必须被剥离并记录。

若最终结果没有任何事实陈述，上述覆盖率指标定义为 0 并使评测失败；“无事实可检查”不能视为 100% 通过。

## 6. 规则与算法指标

### 6.1 非法候选透出率

```text
illegal_candidate_leak_rate =
  count(FAIL candidates present in formal output)
  / count(injected or golden-case FAIL candidates)
```

对已声明 `RuleCoverage=FULL` 的规则必须为 `0`。UNKNOWN 候选进入正式输出同样算门禁失败。

### 6.2 数值复现率

```text
reproducibility_rate =
  count(cases with normalized output exactly equal to rerun output)
  / count(reproducible cases)
```

必须为 `100%`。比较范围：

- 候选组键和顺序；
- 概率、效用、最终排名；
- AuditBasis 和样本年份；
- 规则状态与原因；
- 证据引用和验证状态。

## 7. 性能与成本

### 7.1 延迟口径

- `active_execution_latency`：run 处于 RUNNING 的累计时间，不含 QUEUED 和 WAITING_HITL；
- `wall_clock_latency`：Submit 受理到终态的时间，仅作诊断；
- `time_to_first_event`：Submit 受理到首个持久事件；
- `P50/P99` 使用最近一次固定环境的评测样本计算。

P99 输入要求：

- 至少 100 个有效样本；
- 记录硬件、数据库数据量、模型路由、缓存状态和并发度；
- 不允许把 WAITING_HITL 混进主动执行延迟。

### 7.2 SLO 门禁

| 指标 | 门禁 |
| --- | --- |
| 主动执行硬超时 | 所有 run ≤ 600000ms |
| P99 回归 | 相对基线不得增加超过 10% |
| 单 session token | 所有用例 ≤ 50000 |
| 单 session 成本 | 所有用例 ≤ 150000 USD_MICRO |
| 预算违规次数 | 0 |
| 首事件延迟 | 记录但作为 L2 体验指标，不作为安全门禁 |

首次评测没有基线时，先建立基线；建立基线不等于自动通过发布，安全指标仍必须达标。

### 7.3 实现口径

发布门禁从 RunLifecycle 与 LLMGateway 的权威记录构造 `RunSloSample`，不接受调用方
直接提交一组“看起来合格”的预算数字：

- active execution 样本由 `600000 - active_execution_remaining_ms` 得到；
- provider token 与成本取对应 `principal + session` 的 LLMGateway budget scope
  已结算值；
- `budget_status=BUDGET_OVERRUN` 计为预算违规；
- 同一 run 下存在 `PENDING_RECONCILIATION` attempt 时，预算证据不完整，发布门禁失败；
- 100 个有效样本、固定环境、硬件、数据库数据量、模型路由、缓存状态和并发度必须
  同时存在；任一身份缺失或与 EvalRun 不一致都失败。
- `RunSloSample` 必须携带 run 的 `release_id`；全部样本必须与 EvalDataset 的
  `release_id` 一致，不得把不同 release 的运行耗时混入同一 P99。
- baseline 必须保存与当前 RunSloSample 相同的 `performance_context`；环境、硬件、
  数据库数据量、模型路由、缓存状态或并发度任一缺失/不一致时，禁止 P99 相对比较。

P99 使用 nearest-rank 计算。active execution 任一 run 超过 600000ms 直接失败；P99
相对同 dataset baseline 增加超过 10% 同样失败。

### 7.4 M5b 聚合发布结论

M5b 发布结论是质量/SLO/预算与安全证据的唯一聚合结果，至少包含六类 gate：

```text
QUALITY_SLO_BUDGET | CRITICAL_FACTS | CONSENT | DELETION | RETENTION | RESTORE
```

- `QUALITY_SLO_BUDGET` 消费签字 EvalDataset 上的检索、报告事实、可复现性、P99、
  token、成本、预算违规与待对账结论；
- `CRITICAL_FACTS` 只检查最终保留 Claims 与最终报告 case gate，不因预算或 P99
  失败而误报事实错误；
- `CONSENT`、`DELETION`、`RETENTION`、`RESTORE` 每类必须恰好有一条已通过证据，
  缺失、重复或未知 gate 均 fail closed；
- 任一绝对安全或关键事实失败都使发布结论为 `FAIL`，不能用更高 recall、更低成本
  或更快 P99 抵消；
- 聚合结果保存 release、dataset version/hash、git SHA、每项 gate 的
  actual/threshold/baseline、failed metrics/case IDs、失败资源类型和稳定
  artifact hash；输入证据重排不得改变 artifact identity。

## 8. 基线比较

```text
baseline_eval_run_id
```

规则：

- baseline 必须使用同一 dataset version；
- 更换模型、路由、算法或 release 时必须记录；
- 质量指标允许设置“不显著下降”阈值，当前默认 2 个百分点；
- 安全指标不允许设置容差；
- P99 默认相对阈值 10%；
- 任何阈值变更必须更新本规格版本或新增 ADR。

如果 dataset version 改变，旧基线与新数据不可直接比较，必须重新建立基线并保留旧结果。

## 9. CI 与人工评测

### 9.1 CI

每次 PR 必须运行：

- schema/契约测试；
- 纯函数和状态机测试；
- 使用冻结替身的检索和端到端评测；
- 安全、权限和删除测试；
- 预算账本和幂等测试；
- golden set 门禁。

CI 不调用真实付费模型，不下载外部数据。

聚合发布门禁通过 `make m5b-release-gate` 执行。该目标只接受持久化
`eval_run_id` 与当前发布候选 git SHA，从 PostgreSQL 加载 EvalRun 和原始
retrieval/E2E/run samples 后重算汇总指标。四类安全测试在独立的
`SECURITY_TEST_DSN` 中由服务端 runner 实际执行，结果先写入 insert-only
attestation，再按 artifact identity 加载；不得从调用方 JSON 或内存 PASS 放行。
任何身份不一致、缺失证据或非 PASS EvalRun 均以非零状态退出。

### 9.2 人工/定时评测

真实模型、真实 provider 和性能压测按固定 environment 运行：

- 记录 provider/model/price table 版本；
- 结果写入 EvalRun；
- 不把一次热库测试当作 P99；
- 真实 provider 失败不等于 golden set 事实通过。

## 10. 指标配置

```text
EvalThresholds
  dataset_version
  recall_at_5_min: 0.80
  mrr_min: 0.65
  quality_regression_max_points: 0.02
  p99_regression_max_ratio: 0.10
  scope_leak_rate_max: 0
  contradiction_rate_max: 0
  critical_unverifiable_rate_max: 0
  citation_coverage_min: 1
  citation_accuracy_min: 1
  hallucination_rate_max: 0
  illegal_candidate_leak_rate_max: 0
  run_reproducibility_rate_min: 1
  run_sample_count_min: 100
  run_active_execution_max_ms: 600000
  session_token_count_max: 50000
  session_cost_micro_usd_max: 150000
  budget_violations_max: 0
```

阈值调整必须留下决策记录，不能为了提高通过率而静默降低。

## 11. 失败报告

失败时必须输出：

```text
gate_status: FAIL
failed_metrics
failed_case_ids
actual / threshold / baseline
git_sha / release_id / versions
first_diff
```

CI 失败不能只输出“评测不通过”。

## 12. 可观测指标

统一采用以下指标名称：

- `retrieval_recall_at_5`
- `retrieval_recall_at_20`
- `retrieval_mrr`
- `retrieval_scope_leak_rate`
- `claim_citation_coverage`
- `claim_citation_accuracy`
- `claim_contradiction_rate`
- `claim_unverifiable_rate`
- `illegal_candidate_leak_rate`
- `run_reproducibility_rate`
- `run_active_execution_p50_ms`
- `run_active_execution_p99_ms`
- `run_first_event_p99_ms`
- `session_cost_micro_usd`
- `session_token_count`
- `budget_violation_count`

面板不得展示完整用户画像或敏感事实。

## 13. 验收测试

| ID | 场景 | 期望 |
| --- | --- | --- |
| ES-01 | 同一 dataset 重跑 | dataset hash 和结果身份一致 |
| ES-02 | 修改 golden set 一条 | 自动产生新 dataset version |
| ES-03 | 跨省证据进入 top-k | `scope_leak_rate > 0`，门禁失败 |
| ES-04 | 关键词命中但不满足预期证据 | recall 不得误计为命中 |
| ES-05 | 报告有事实无证据 | citation coverage 失败 |
| ES-06 | 报告数字被篡改 | contradiction rate > 0，门禁失败 |
| ES-07 | 一个 UNKNOWN 候选进入正式输出 | illegal leak，门禁失败 |
| ES-08 | 同输入二次算法重跑 | reproducibility 100% |
| ES-09 | 并发 100 请求 | P99、预算和错误率均记录 |
| ES-10 | 超过 session 预算 | budget violations > 0，门禁失败 |
| ES-11 | 质量下降但安全通过 | 按质量回归阈值单独判定 |
| ES-12 | 安全通过但质量退化 | 仍可能因回归阻断 |
| ES-13 | 真实 provider 评测失败 | EvalRun 记录失败，不回退成通过 |
| ES-14 | 基线 dataset 改变 | 必须新建 baseline，禁止跨版本比较 |
| ES-15 | P99 少于 100 个有效样本 | `run_sample_count` 失败，禁止 PASS |
| ES-16 | 预算账本存在待对账 attempt | `budget_reconciliation_pending` 失败 |
| ES-17 | 一个可复现 case 二次结果不同 | `run_reproducibility_rate` 失败 |
| ES-18 | 不允许的候选进入正式结果 | `illegal_candidate_leak_rate` 失败 |

## 14. 非目标

- 不做模型训练；
- 不保证统计显著性；
- 不用 golden set 替代真实用户研究；
- 不把 P99 目标写成未经测量的生产承诺；
- 不用一个平均分替代逐项安全门禁。
