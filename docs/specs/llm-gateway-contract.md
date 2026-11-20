# LLM Gateway Runtime Contract

| 项 | 内容 |
| --- | --- |
| 版本 | v1.0 |
| 状态 | 已确认；作为 M5 LLM 治理实现基线 |
| 上游 | [PRD v1.3 FR-20/FR-21/FR-22](../prd.md)、[ADR-0003](../adr/0003-deepseek-early-llm-experimental.md)、[ADR-0004](../adr/0004-immutable-content-hash-release-id.md) |
| 关联 | [Run Lifecycle Contract](./run-lifecycle.md)、[Evidence Contract](./evidence-contract.md) |

## 1. 目标与边界

本规格定义模型调用、角色路由、缓存、预算预占、结算、重试账本、流式事件与敏感数据边界。

LLM Gateway 是模型供应商的唯一适配层。调用方不得持有供应商 key，不得指定任意模型，不得根据供应商返回值自行决定预算是否允许。

不在本规格内：

- 画像解析、方案生成、报告生成等业务逻辑；
- 本地 embedding / rerank；
- 评测指标和 golden set；
- 供应商模型能力评测；
- 前端 SSE 协议。

## 2. 核心不变量

1. **模型由服务端路由。** 调用方只传 `role`，不能传供应商 key、任意 base URL 或任意模型名。
2. **预算硬上限先于供应商调用。** 未成功原子预占时不得发起请求。
3. **预算跨 run 累计。** 作用域固定为 `principal_id + session_id`，创建新 run、修订或重试不重置。
4. **逻辑调用与供应商尝试分离。** `logical_call_id` 对应用户语义上的一次模型调用；`attempt_id` 对应每次供应商请求。
5. **未知费用不能立即释放。** 超时、断流或供应商未明确计费时，预占保持待对账状态。
6. **缓存必须版本化。** 命中条件包含 prompt、schema、路由、模型、release 和敏感数据策略。
7. **敏感字段默认不进入云模型。** 健康等敏感信息必须先本地脱敏或转换成规则结论。
8. **结构化输出必须由服务端校验。** schema 校验失败不能作为成功结果返回。
9. **流结束必须有账本。** 正常完成、供应商错误和客户端断流都要记录可对账状态。
10. **只记录必要数据。** 不把完整敏感 prompt、模型 key 或未脱敏响应写入日志。
11. **system prompt 只来自注册模板。** 调用方 messages 中不得夹带 system 角色；
    prompt 模板按 release + key + version 解析。
12. **结构化授权只来自注册 schema。** 旧 `json_schema` 字段保留 wire 兼容，但不能
    授权、覆盖或改变已注册 response schema。

## 3. 预算模型

### 3.1 作用域

```text
BudgetScope
  budget_scope_id: opaque string
  principal_id: trusted identity
  session_id: UUID
  currency: USD_MICRO
  token_limit: 50000
  cost_limit_micro_usd: 150000      # $0.15
  provider_tokens_settled: int64
  provider_tokens_reserved: int64
  cost_settled_micro_usd: int64
  cost_reserved_micro_usd: int64
  budget_status: ACTIVE | BUDGET_OVERRUN
  overrun_at: timestamp | null
```

- `budget_scope_id` 由服务端根据 principal 与 session 生成，不接受客户端覆盖。
- 预算按 provider 实际 token 计；缓存命中不消耗 provider token，但受网关限流保护。
- 成本统一用 `USD_MICRO` 存储，`1 USD = 1,000,000`。
- `(provider_tokens_settled + provider_tokens_reserved)` 与
  `(cost_settled_micro_usd + cost_reserved_micro_usd)` 必须同时不超过各自上限。
- 并发预占通过带 `budget_status = 'ACTIVE'` 与双上限条件的单条
  `UPDATE ... RETURNING/affected rows` 完成；禁止先查余额再无条件更新。
- `BUDGET_OVERRUN` 是作用域级持久封锁状态。已有预占的在途调用可以完成结算或对账，
  但新 logical call 不得创建 provider attempt 或访问供应商。

### 3.2 价格表

```text
PriceTable
  price_table_version: string
  effective_at: RFC3339 UTC
  currency_unit: USD_MICRO
  exchange_rate_version: string | null
  models:
    model_id
    input_micro_usd_per_million_tokens
    output_micro_usd_per_million_tokens
```

- 价格必须来自已发布的 `price_table_version`。
- 供应商以人民币报价时，必须记录汇率版本和换算后的 USD_MICRO。
- 缺少当前价格时返回 `PRICE_UNAVAILABLE`，不得按旧价格猜测。
- 价格更新不得改变已经结算的调用账本。

### 3.3 预占公式

```text
input_token_bound = utf8_byte_count(canonical_request_payload)
output_token_bound = max_output_tokens

attempt_cost_bound =
  max over primary and fallback models of:
    ceil(input_token_bound * input_rate / 1_000_000)
    + ceil(output_token_bound * output_rate / 1_000_000)

logical_reservation =
  attempt_cost_bound * max_attempts
```

- `max_attempts` 默认 2，包含备用模型和 SDK 内部自动重试。
- UTF-8 字节数是 V1 的保守 token 上界；tokenizer 版本升级时可以降低过度预占，但不能削弱硬上限。
- 预占前还检查 `(input_token_bound + max_output_tokens) * max_attempts` 是否会使 session token 超过 50000。
- 预算不足返回 `BUDGET_EXCEEDED`，不调用供应商。
- 创建 `IN_PROGRESS` logical call、首个 `IN_PROGRESS` provider attempt 与预算预占
  在同一个 PostgreSQL 事务内提交；只有提交成功后才会退出事务并调用 provider。

### 3.4 结算公式

```text
actual_cost =
  ceil(actual_input_tokens * input_rate / 1_000_000)
  + ceil(actual_output_tokens * output_rate / 1_000_000)
```

- 成功返回 usage 时按实际值结算。
- 成功后释放该逻辑调用未使用的预占。
- 供应商明确返回“请求未受理且未计费”时，标记 attempt `FAILED_NO_CHARGE`、
  logical call `FAILED`，并在同一事务释放全部 token/cost 预留。
- 超时/断流且 usage 未知时，对应 attempt 保持 `PENDING_RECONCILIATION`，预占不释放。
- 实际费用或实际 provider tokens 超过预占时，尝试进入
  `PENDING_RECONCILIATION / BUDGET_OVERRUN`，作用域锁存
  `budget_status = 'BUDGET_OVERRUN'`；既有预留保持待人工对账，后续调用在
  provider 零调用下返回 `BUDGET_OVERRUN`，不得静默扩大上限。

## 4. 路由与能力

```text
RoleRoute
  role: profile | strategy | critic | report | verify | policy_qa
  primary_model_id
  fallback_model_ids: list<string>
  route_version: string
  max_output_tokens
  timeout_ms
  requires_response_schema: bool
  price_table_version
  allowed_data_classification:
    PUBLIC | INTERNAL
```

规则：

- 模型白名单、顺序和 token 上限只来自服务端配置/release；
- 调用方不得覆盖；
- 结构化角色必须使用支持 JSON Schema 的模型；
- 备用模型只有在主模型发生可重试故障时使用；
- 每次 attempt 记录实际模型；
- 切换备用模型属于同一 `logical_call_id` 的新 `attempt_id`；
- M5a 核心硬上限为每个 logical call 最多两个 provider attempt；route
  `max_attempts` 只能配置 1 或 2，数据库同时约束 attempt 序号不超过 2；
- 模型价格表中不存在的模型不得启用。

## 5. 请求与响应

### 5.1 ChatRequest

```text
ChatRequest
  trace_id
  principal_id: from trusted context
  session_id
  run_id
  role
  release_id
  prompt_template_key
  prompt_template_version
  prompt_variables: map<string, string>
  messages: list<Message>
  json_schema: legacy wire compatibility only
  response_schema_key: string | null
  response_schema_version: string | null
  temperature: double
  max_output_tokens: int32
  timeout_ms: int32
  data_classification: PUBLIC | INTERNAL | SENSITIVE
  logical_call_id: stable string
```

约束：

- 新调用必须引用 release 内 `ACTIVE` 的 prompt template key/version，并可提交模板
  变量；未注册、已撤销或版本不存在返回 `PROMPT_TEMPLATE_NOT_ALLOWED`。
- `system` message 必须由注册模板生成；调用方 messages 中出现 system 角色返回
  `PROMPT_TEMPLATE_NOT_ALLOWED`。非 system messages 只能在服务端模板之后追加。
- `SENSITIVE` 数据不得发送到云供应商，返回 `SENSITIVE_DATA_NOT_ALLOWED`。
- `max_output_tokens` 不能超过角色路由上限。
- `timeout_ms` 不能超过当前 run 剩余执行时间。
- `release_id` 必须有效且用于缓存与模型配置解析。
- `requires_response_schema=true` 或调用方提交 schema 引用时，必须使用 release
  内 `ACTIVE` 的 response schema key/version。原始 `json_schema` 不能代替注册引用。

### 5.2 ChatResponse

```text
ChatResponse
  content: string
  structured_output: canonical object | null
  model_id
  route_version
  cache_hit: bool
  finish_reason: STOP | LENGTH | CONTENT_FILTER | ERROR
  usage:
    input_tokens
    output_tokens
  cost:
    currency: USD_MICRO
    actual_micro_usd
    settlement_status
  attempts: list<AttemptSummary>
  ledger_entry_id
  prompt_template_key / prompt_template_version
  response_schema_key / response_schema_version
```

`AttemptSummary`：

```text
attempt_id
model_id
provider_status
latency_ms
input_tokens / output_tokens | null
provider_request_id | null
```

`structured_output` 必须通过 response schema 校验；校验失败返回 `SCHEMA_VALIDATION_FAILED`，不得作为成功内容。

### 5.3 Prompt template 与 response schema 注册

`migrations/32_llm_prompt_schema_registry.sql` 建立两张 release-bound 注册表：

- `llm_prompt_template`：`release_id + prompt_template_key + prompt_template_version`
  唯一，保存不可变 `system_template / user_template / template_sha256 / status`；
- `llm_response_schema`：`release_id + response_schema_key + response_schema_version`
  唯一，保存不可变 JSON Schema、内容摘要和 `status`；
- `llm_role_route.requires_response_schema` 明确该角色是否必须结构化输出；
- `llm_logical_call` 冻结本次调用实际使用的 prompt/schema key、version 和摘要。

模板变量使用 `{{variable_name}}`，变量值作为数据替换；缺失变量或格式无效时不预占
预算、不访问 provider。解析后的 system/user messages，连同 prompt/schema 的
key、version 和摘要进入 request hash。模板内容只能在发布新 version 时变化；撤销
只允许 `ACTIVE -> REVOKED`，不能恢复或原地改写。

结构化调用要求供应商返回 JSON object。网关在 provider 返回后、成功响应和结算前用
同一注册 schema 校验：

- 通过时 `structured_output` 返回规范化 object，并返回 prompt/schema/route/model/
  price 身份；
- 不通过时返回 `SCHEMA_VALIDATION_FAILED`，logical call 记为 `FAILED`，不返回
  structured output；
- schema 失败仍按供应商真实 usage 原子结算并释放差额；`llm_call_log` 只记录失败
  投影与稳定 reason，不作为预算权威。

## 6. 缓存

缓存键的 canonical 输入：

```text
prompt_template_key + prompt_template_version
+ canonical_redacted_messages
+ response_schema_key + response_schema_version
+ role + route_version + model_id
+ temperature + max_output_tokens
+ release_id
+ cache_schema_version
```

规则：

- 仅 `temperature=0` 且 `data_classification != SENSITIVE` 的调用可缓存；
- 缓存值保存最终内容、结构化结果、模型、route、schema 和 usage 类型；
- 缓存命中不创建 provider attempt，provider token 和实际费用为 0；
- 缓存命中仍记录 logical call、命中来源和本地延迟；
- 模型、schema、prompt、路由或 release 变化自然产生新键；
- 不缓存供应商原始错误或未脱敏响应。

## 7. 幂等与重试

### 7.1 逻辑调用幂等

作用域：

```text
principal_id + session_id + logical_call_id
```

- 同 `logical_call_id`、同 request hash：返回已存在的结果或在途状态。
- 同 `logical_call_id`、不同 request hash：`IDEMPOTENCY_CONFLICT`。
- 已完成调用再次请求：返回同一最终响应，不重复产生供应商费用。
- 已知终态失败再次请求：返回同一稳定原因且不新增 attempt；`PARTIAL_UNKNOWN`
  返回待对账原因，只有 `RESERVED / IN_PROGRESS` 返回 `CALL_IN_PROGRESS`。
- 进行中的调用返回 `CALL_IN_PROGRESS`，不并发重复发起。

### 7.2 供应商重试

| 错误 | 重试 |
| --- | --- |
| 连接失败、429、5xx、明确未受理 | 可进入下一 attempt，总 attempt 不超过 2 |
| 参数错误、schema 不支持、认证失败 | 不重试 |
| 超时且可能已计费 | attempt 标记待对账，不立即释放预占 |
| 客户端断流 | 标记 `STREAM_INTERRUPTED`，对账前保持预占 |

格式错误导致的语义修复必须新建 `logical_call_id` 或进入业务层重写预算，不能伪装成网络重试。

- Provider adapter 必须关闭供应商 SDK 内部自动重试；网关的一次 `Provider.Chat`
  对应一次供应商请求，因此 SDK 重试不能绕过 attempt 上限。
- Adapter 只有在已确认“未受理且未计费”时才返回 typed `ProviderFailure`；
  `Retryable=true` 才允许按 route 使用下一模型。参数错误、认证失败和 schema
  不支持返回不可重试的稳定原因；任何未分类连接异常、超时或断流仍按未知计费处理，
  不由网关猜测为免费。

## 8. 流式接口

```text
ChatStreamEvent
  sequence: int64
  type: START | DELTA | USAGE | DONE | ERROR
  logical_call_id
  attempt_id
  model_id
  delta_text: string | null
  finish_reason: string | null
  usage: Usage | null
  error_code: string | null
```

规则：

- `START` 在预算预占成功后发送；
- token delta 只做展示，业务方不得把未完成内容作为最终结果；
- `DONE` 必须包含 usage、ledger id 和 schema 校验结果；
- `ERROR` 必须包含稳定错误码；
- 客户端断开时网关停止发送，但不能假设供应商取消或免计费；
- 流式缓存命中以后可以单个 `DELTA + DONE` 回放；
- 事件序号由网关为本次流生成，不等同于 run 的 `event_seq`。

## 9. 调用账本

### 9.1 LogicalCall

```text
logical_call
  logical_call_id
  principal_id
  session_id
  run_id
  role
  release_id
  prompt_template_key/version
  response_schema_key/version
  request_hash
  route_version
  cache_hit
  status: RESERVED | IN_PROGRESS | SUCCEEDED | FAILED | PARTIAL_UNKNOWN
  reservation_micro_usd
  created_at / updated_at
```

### 9.2 ProviderAttempt

```text
provider_attempt
  attempt_id
  logical_call_id
  model_id
  provider
  request_started_at / finished_at
  input_tokens / output_tokens
  cost_micro_usd
  status:
    RESERVED |
    SUCCEEDED |
    FAILED_NO_CHARGE |
    PENDING_RECONCILIATION
  provider_request_id
  error_code
```

- 账本是预算结算的唯一来源；
- `llm_call_log` 只能作为查询投影，不得以事后累加代替原子预占；
- `llm_call_log` 缺失、延迟修复或投影值与权威账本不一致时，不得改变
  `llm_budget_scope / llm_logical_call / llm_provider_attempt` 的预算裁决；
- 未知计费记录必须能按 `logical_call_id + attempt_id` 对账；
- 任何预算覆盖不足必须报告，不能删除历史记录修正。

## 10. 数据与日志

- 日志记录 trace、principal/session/run 身份、role、模型、usage、成本、延迟和错误码；
- 默认不记录完整 messages、模型原始响应和敏感字段；
- 如需调试 prompt，只记录模板 key/version、脱敏后的 hash 和样本 ID；
- `SENSITIVE` 数据在到达网关前必须已转换为脱敏规则结论；
- 模型 key 只在 gateway 进程中通过 secrets 注入；
- 供应商 URL 和模型 id 不能被请求覆盖。

## 11. 稳定错误

| reason | 行为 |
| --- | --- |
| `BUDGET_EXCEEDED` | 不发起请求 |
| `PRICE_UNAVAILABLE` | 不发起请求 |
| `MODEL_NOT_ALLOWED` | 拒绝路由 |
| `PROMPT_TEMPLATE_NOT_ALLOWED` | 拒绝未注册 system prompt |
| `RESPONSE_SCHEMA_NOT_ALLOWED` | 拒绝未注册/撤销 schema 或 raw schema 绕过 |
| `SENSITIVE_DATA_NOT_ALLOWED` | 拒绝云模型调用 |
| `IDEMPOTENCY_CONFLICT` | 同 key 不同请求 |
| `CALL_IN_PROGRESS` | 同逻辑调用正在执行 |
| `SCHEMA_VALIDATION_FAILED` | 不返回成功结构化结果，按真实 usage 结算 |
| `PROVIDER_TIMEOUT` | 记录 attempt 与待对账状态 |
| `PROVIDER_RATE_LIMITED` | 可重试或切换备用模型 |
| `PROVIDER_UNAVAILABLE` | 可重试或失败 |
| `STREAM_INTERRUPTED` | 保持预占待对账 |
| `BUDGET_OVERRUN` | 停止后续调用并进入人工对账 |

## 12. 验收测试

| ID | 场景 | 期望 |
| --- | --- | --- |
| LG-01 | 并发调用同时接近预算上限 | 原子预占，总量不超上限 |
| LG-02 | 同一 session 创建多个 run | 预算跨 run 累计 |
| LG-03 | 新 session 使用同一 principal | 新预算作用域，不串账 |
| LG-04 | 预算不足 | `BUDGET_EXCEEDED`，provider 零调用 |
| LG-05 | 同缓存键重复调用 | 命中缓存，provider attempt 为 0 |
| LG-06 | release 改变 | 缓存键变化，不命中旧结果 |
| LG-07 | 同 logical_call_id 同请求 | 返回原结果，不重复计费 |
| LG-08 | 同 logical_call_id 异请求 | `IDEMPOTENCY_CONFLICT` |
| LG-09 | 首次 attempt 超时且计费未知 | 保持待对账，不释放预占 |
| LG-10 | 主模型 503 后备用模型成功 | attempt 数、模型和费用分别入账 |
| LG-11 | 结构化输出不符合 schema | `SCHEMA_VALIDATION_FAILED`，不返回成功结果 |
| LG-12 | 请求包含 SENSITIVE 数据 | 拒绝云调用 |
| LG-13 | 客户端断开流 | 记录 `STREAM_INTERRUPTED`，可对账 |
| LG-14 | release 撤销 | 拒绝新模型调用 |
| LG-15 | 实际费用超过预占 | `BUDGET_OVERRUN`，停止后续调用并告警 |
| LG-16 | 调用方自带 system message | `PROMPT_TEMPLATE_NOT_ALLOWED`，provider 零调用 |
| LG-17 | prompt 未注册、撤销或版本不存在 | `PROMPT_TEMPLATE_NOT_ALLOWED`，provider 零调用 |
| LG-18 | raw `json_schema` 未引用注册 schema | `RESPONSE_SCHEMA_NOT_ALLOWED`，provider 零调用 |
| LG-19 | 合法结构化 object | 返回 canonical `structured_output` 与全部治理身份 |
| LG-20 | 输出不符合 schema | `SCHEMA_VALIDATION_FAILED`，无成功结果但结算真实 usage |
| LG-21 | prompt/schema version 变化 | request hash 与逻辑调用身份变化，不复用旧语义 |
