# Proto and Database Change Plan

| 项 | 内容 |
| --- | --- |
| 版本 | v1.0 |
| 状态 | 待执行 |
| 目标 | 把 PRD v1.3 与八份 runtime 契约落成可递进的 proto、数据库迁移和 issue 图 |
| 上游 | [PRD v1.3](../prd.md)、[可执行规格](./README.md) |
| 约束 | 只做加法迁移；不重放或改写历史初始化 SQL；不跨服务 import 内部实现 |

## 1. 总原则

1. 历史 `migrations/00_*` 至 `07_*` 保持不动。
2. 新变更从 `08_*` 开始，按依赖顺序新增升级迁移。
3. 新数据库对象先上线并 backfill，再删除旧列/旧表；V1 不直接 drop 仍被代码读取的表。
4. proto 是 Go/Python 跨服务唯一 wire 契约；不得用 `*_json` 承载跨服务业务数据。
5. 身份、release、版本和幂等 ID 都由服务端生成。
6. LangGraph checkpoint 表由官方 saver 的固定迁移创建；业务 `plan_checkpoint` 只保留审计用途。
7. 所有 public 数据集都通过 `release_id` 选择；不允许在线 run 混读不同版本。
8. 单元测试通过模块 interface；跨服务集成测试通过生成的 proto client。

## 2. 迁移顺序

| migration | 内容 | 依赖 |
| --- | --- | --- |
| `08_release_and_identity.sql` | release 清单、数据 release 维度、principal 基座 | 00–07 已完成 |
| `09_profile_run_lifecycle.sql` | profile revision、run、interrupt、event、receipt、result、artifact | 08 |
| `10_policy_rules_evidence.sql` | policy 适用域、release 映射、规则、三类证据 | 08、09 |
| `11_llm_gateway.sql` | 价格表、预算作用域、逻辑调用、provider attempt、缓存 | 08、09 |
| `12_eval_retention_security.sql` | EvalDataset/EvalRun、consent、删除、保留与安全审计 | 08–11 |

官方 `langgraph-checkpoint-postgres` 的 saver 表必须使用固定版本迁移，不手写同名表。

## 3. Release 与公共数据版本

### 3.1 新表

```text
release
  release_id TEXT PK                 # SHA256 前缀
  status STAGING|PUBLISHED|REVOKED
  manifest JSONB
  manifest_sha256 TEXT
  created_at
  published_at
  revoked_at

release_component
  release_id
  component_type
  dataset_key
  component_version
  content_sha256
  PRIMARY KEY(release_id, component_type, dataset_key)
```

`release_component` 至少覆盖：

- enrollment plan；
- major admission；
- school admission；
- score rank map；
- university/master data；
- policy documents/chunks；
- rules；
- embedding/index；
- model route/prompt/schema；
- price table。

### 3.2 结构化数据表

向以下表增加 `release_id TEXT NOT NULL REFERENCES release(release_id)`：

- `university`
- `major`
- `enrollment_plan`
- `major_admission`
- `school_admission`
- `score_rank_map`

唯一约束必须包含 `release_id`；已有的 province 缺失问题一并收口：

- `university(release_id, school_id)`
- `major(release_id, university_id, major_group, major_code, major_name)`
- `enrollment_plan(release_id, province, university_code, major_group, major_code, category, batch, year)`
- `major_admission(release_id, province, university_code, major_group, major_code, category, batch, year)`
- `school_admission(release_id, province, university_code, major_group, category, batch, year)`
- `score_rank_map(release_id, province, year, category, batch, score)`

迁移步骤：

1. 创建 `rel_legacy_v1`；
2. 先加 nullable `release_id`；
3. backfill 现有全部行；
4. 设置 `NOT NULL`；
5. 删除旧唯一约束并创建新约束；
6. 为 `release_id + province + year + category + batch` 增加复合索引。

数据导入 SQL 的后续版本必须显式写 `release_id`；历史 SQL 作为首次装载保留。

### 3.3 文档数据版本

为避免重复存储昂贵向量，`policy_doc` / `policy_chunk` 采用稳定内容身份 + release 映射：

```text
release_policy_doc
  release_id
  doc_id
  chunk_set_version
  PRIMARY KEY(release_id, doc_id)

release_policy_index
  release_id PK
  embedding_model
  embedding_dim
  index_version
```

chunk 通过 `doc_id + chunk_set_version` 归属 release；`release_policy_index` 声明整个 release 唯一的模型、维度和索引版本，映射内所有 `policy_chunk_set` 与 chunk 身份必须一致。同一文档内容未变化时可复用；重新切分或更换 embedding 必须生成新 `chunk_set_version`/chunk 身份。

## 4. Profile 与 Run 持久化

### 4.1 Profile

```text
profile_revision
  profile_version UUID PK
  session_id UUID
  revision_no BIGINT
  based_on UUID NULL
  lifecycle DRAFT|CONFIRMED|SUPERSEDED
  fields JSONB
  created_at
  confirmed_at NULL
  UNIQUE(session_id, revision_no)

profile_confirmation
  confirmation_id UUID PK
  profile_version UUID
  confirmed_by
  confirmed_at
  consent_id NULL
```

要求：

- revision 内容不可变；
- `EXTRA_PROFILE` 等旧 DTO 不再写回 `chat_session.profile`；
- `chat_session` 保留为 session 容器，逐步去业务状态。

### 4.2 Run

```text
run
  run_id UUID PK
  session_id UUID
  principal_id TEXT
  parent_result_version UUID NULL
  task_kind
  state
  stage
  terminal_reason NULL
  state_version BIGINT
  release_id TEXT NOT NULL
  working_profile_version UUID NULL
  confirmed_profile_version UUID NULL
  result_version UUID NULL
  result_kind NULL
  lease_owner NULL
  lease_epoch BIGINT
  lease_expires_at NULL
  active_execution_remaining_ms BIGINT
  cancel_requested_at NULL
  created_at / updated_at
```

辅助表：

- `run_interrupt`
- `run_command_receipt`
- `run_event`，`UNIQUE(run_id, event_seq)`
- `run_outbox`
- `run_artifact`
- `result_version`

`run_event.payload` 在 DB 中可存版本化 JSON，但跨服务 wire 必须使用 proto `oneof`。

### 4.3 Session 与 checkpoint

- `chat_session` 增加 `principal_id`，现有 `status/current_node` 不再作为 run 状态权威；
- `plan_checkpoint` 改名语义为审计快照或新增 `audit_checkpoint`，不承担恢复；
- 恢复使用官方 LangGraph saver；
- 新增租约/fencing 必需索引。

## 5. Policy、规则与证据

### 5.1 Policy 元数据

`policy_doc` 增加：

- `document_type`
- `issuer_type`
- `issuer_id`
- `school_id`
- `fetched_at`
- `content_sha256`
- `raw_storage_uri`

新增：

```text
policy_applicability
  id
  doc_id
  regions TEXT[]
  categories TEXT[]
  batches TEXT[]
  valid_from DATE
  valid_to DATE NULL
```

`policy_chunk` 增加：

- `text_sha256`
- `char_start/char_end NULL`
- `embedding_model`
- `embedding_dim`
- `index_version`

`charter_doc/charter_chunk` 由新迁移标注废弃；V1 不继续写入。数据迁移完成和代码完全切换后，再由单独迁移 drop。

### 5.2 规则

```text
rule_version
  rule_id UUID
  version INT
  rule_key
  rule_type
  scope JSONB
  required_profile_fields JSONB
  expression JSONB
  outcome_when_true
  reason_code
  review_status
  valid_from/valid_to
  release_id
  evidence_bundle_id
  PRIMARY KEY(rule_id, version)

rule_coverage
  release_id
  rule_type
  scope JSONB
  status FULL|PARTIAL|NONE
  covered_data_sources JSONB
```

必须约束同一 release、同一 `rule_key` 只有一个适用 PUBLISHED 版本。

### 5.3 证据

```text
evidence_ref
  evidence_id
  evidence_type
  release_id
  content_sha256
  payload JSONB
  created_at

evidence_package
  evidence_package_id
  result_version
  verifier_version
  status

claim
  claim_id
  evidence_package_id
  claim_type
  criticality
  normalized JSONB
  rendered_text

claim_evidence
  claim_id
  evidence_id

claim_verification
  claim_id
  status
  reason
  checked_evidence_ids
  deterministic
```

`result_version` 必须引用不可变 `evidence_package`。

## 6. LLM Gateway 持久化

新表：

- `llm_price_table`
- `llm_price_entry`
- `llm_budget_scope`
- `llm_logical_call`
- `llm_provider_attempt`
- `llm_cache_entry`

关键约束：

- budget scope 唯一键为 `(principal_id, session_id)`；
- `logical_call_id` 幂等作用域唯一；
- attempt 可按 logical call 唯一追踪；
- price table 有版本和生效时间；
- 预占/结算使用单事务条件更新；
- `llm_call_log` 保留为只读投影，不再承担权威结算。

## 7. Eval、同意、删除与保留

新表：

- `eval_dataset`
- `eval_run_v2` 或对现有 `eval_run` 的兼容扩展
- `eval_case_result`
- `consent_record`
- `deletion_request`
- `deletion_item`
- `deletion_tombstone`
- `security_audit`
- `retention_job`

`eval_run` 增加：

- `release_id`
- `dataset_version`
- `algorithm_config_version`
- `model_route_version`
- `price_table_version`
- `verifier_version`
- `gate_status`

旧 `data_version` 进入只读兼容期。

## 8. Proto 变更矩阵

| proto | 变更 |
| --- | --- |
| `common/v1` | 新增稳定错误、Trace 边界、共享 EvidenceRef/Release 引用；禁止身份放正文 |
| `gateway/v1` | 新增 Submit/Resume/Watch/Get/Cancel/Delete 接口；旧 plans 路由保留适配器 |
| `orchestrator/v1` | 用类型化 Run/Event/Interrupt 替换 `payload_json`；新增 Get/Cancel/Watch |
| `algo/v1` | 所有请求增加 `release_id`；新增 current-year plan 字段、PASS/FAIL/UNKNOWN、扩展资格输入和证据引用 |
| `llmgw/v1` | 扩展 logical call、预算、模板/schema、usage、cost、attempt、流式事件 |
| `conf/conf.proto` | 增加 role 路由、模型白名单、限流、保留策略和服务间认证配置 |

### 8.1 身份传播

- 浏览器身份在 gateway 终止；
- gateway → orchestrator 使用服务认证 + gRPC metadata 传播可信 `principal_id`、request id、trace；
- 不把 `principal_id` 放在客户端可填的 proto body；
- Python/Go 内部 client 统一封装 metadata。

### 8.2 生成与兼容

- 新字段使用新编号；
- 旧 RPC 不删除，先加新 RPC 和 adapter；
- `make api` 生成 Go/Python；
- 契约 CI 检查旧字段编号和 wire compatibility；
- `openapi.yaml` 重新生成并纳入检查。

## 9. Algo 契约演进

必须新增：

- 每个 RPC 的 `release_id`；
- `RankConvertReply.control_score`、`total_count`、`data_year`；
- Candidate 的 current-year plan/member identity、history comparability；
- `EvaluateEligibility` 或向后兼容的扩展 CheckConstraints；
- group/major 五态结果；
- `RULE_COVERAGE_MISSING`、`RULE_CONFLICT` 等稳定 reason；
- 与 EvidenceRef 的结构化引用。

在 proto 兼容期，旧四 RPC 保持可调用；新 eligibility 结果通过新 RPC 提供。

## 10. 测试与验收

每个迁移 issue 必须包含：

- upgrade from old schema；
- fresh schema from 00–N；
- rollback/deprecated-table behavior；
- backfill invariants；
- query plan/index verification；
- generated code diff；
- no historical migration edits。

每个 proto issue 必须包含：

- Go/Python generation；
- wire compatibility；
- required-field omission behavior；
- stable error reason；
- adapter compatibility。

## 11. GitHub Issue 图

临时键：

| key | 标题 | blocked by |
| --- | --- | --- |
| `PARENT` | M1 执行规格与 proto/DB 收口 | — |
| `DB-01` | release、数据版本与 principal 基座 | — |
| `PROTO-01` | common/run/event wire 契约 | — |
| `DB-02` | profile/run/interrupt/event/result 持久化 | DB-01 |
| `DB-03` | policy 适用域、规则与证据表 | DB-01 |
| `DB-04` | LLM、Eval、删除与保留表 | DB-01, DB-02, DB-03 |
| `PROTO-02` | gateway/orchestrator 运行 API | PROTO-01 |
| `PROTO-03` | algo eligibility/release/evidence 契约 | PROTO-01 |
| `PROTO-04` | llm-gateway 类型化调用与预算契约 | PROTO-01 |
| `TEST-01` | 契约、故障注入与迁移测试基座 | PROTO-01, DB-02 |
| `IMPL-01` | M2 RunLifecycle 可靠任务底座 | DB-01, DB-02, PROTO-02, TEST-01 |
| `IMPL-02` | M3 受控 ALGORITHM_PREVIEW 闭环 | IMPL-01, PROTO-03 |
| `IMPL-03` | M4 检索、规则与证据链 | DB-03, PROTO-03, TEST-01, IMPL-02 |
| `IMPL-04` | M5 LLM 治理、报告与评测门禁 | DB-04, PROTO-04, IMPL-01, IMPL-03 |

依赖图：

```text
DB-01 ─┬─> DB-02 ─┬─> TEST-01 ─┐
       │          │             ├─> IMPL-01 ─> IMPL-02 ─> IMPL-03 ─> IMPL-04
       │          └─> DB-04 ──┐ │                           │
       └─> DB-03 ──────────────┘ │                           │
                                 │                           │
PROTO-01 ─┬─> PROTO-02 ─────────┘                           │
          ├─> PROTO-03 ─────────────────────────────────────┘
          └─> PROTO-04 ────────────────────────────────────────> IMPL-04
```

## 12. 执行边界

本计划只定义变更和依赖，不代表以下内容已完成：

- 迁移文件；
- proto 修改；
- 生成代码；
- 表创建和 backfill；
- issue 之外的业务实现。

每个 issue 完成时必须附测试证据，并更新 [gap-analysis.md](../gap-analysis.md)。
