# Run Lifecycle Runtime Contract

| 项 | 内容 |
| --- | --- |
| 版本 | v1.0 |
| 状态 | 待确认；确认后作为 M2 实现基线 |
| 上游 | [PRD v1.3 §5.2、§6.2、§8.2、§13](../prd.md)、[GLOSSARY.md](../../GLOSSARY.md) |
| 关联 | [ADR-0002](../adr/0002-langgraph-official-postgres-saver.md)、[Profile Contract](./profile-contract.md) |

## 1. Module

`RunLifecycle` 是一个深模块：对外只暴露五个操作，内部隐藏状态机、租约、幂等、事件序号、中断消费、检查点关联和发布条件。

worker 的正式发布使用内部命令 `PublishFormalResult`。它不是新增客户端入口：
调用方只提交绑定当前 run 的 `EvidencePackage`，`RunLifecycle` 通过唯一
`VerifyPackage` seam 取得报告后决定发布或失败，不能自行声明报告已通过。发布前还必须
复核本 run 的不可变资格/配置 artifact；调用方提交的资格摘要不能作为通过依据。

```text
SubmitRun(command)  -> RunReceipt
ResumeRun(command)  -> RunReceipt
WatchRun(query)     -> stream<RunEvent>
GetRun(query)       -> RunView
CancelRun(command)  -> RunReceipt
```

这个 seam 是 caller 和测试共同使用的接口。Gateway、worker 和测试不得绕过它直接修改 run 状态。

LangGraph checkpointer 是内部 adapter：

- `langgraph-checkpoint-postgres` 保存图进度；
- `plan_checkpoint` 只保存业务审计快照；
- 外部调用方永远不接触框架 checkpoint ID。

## 2. 身份与实体

```text
Principal
  principal_id: trusted identity

Session
  session_id: UUID
  principal_id
  created_at

Run
  run_id: UUID
  session_id
  principal_id
  parent_result_version: string | null
  task_kind: GENERATE_PLAN | POLICY_QA | EXPLAIN_RESULT | REVISE_PLAN
  state
  stage
  terminal_reason: string | null
  state_version: int64
  working_profile_version: string | null
  confirmed_profile_version: string | null
  release_id: string
  lease_owner: string | null
  lease_epoch: int64
  lease_expires_at: RFC3339 UTC | null
  active_execution_remaining_ms: int64
  cancel_requested_at: RFC3339 UTC | null
  result_version: string | null
  result_kind: ALGORITHM_PREVIEW | V1_DEMO | V1_FORMAL | null
  created_at / updated_at
```

约束：

- `principal_id` 只来自可信认证上下文，不接受请求正文自称。
- `session_id` 是容器，不是恢复幂等键。
- 一个 session 可有多个 run；修订和新生成都创建新 run。
- `state_version` 每次对外可见状态变化递增，用于乐观并发控制。
- `lease_epoch` 每次 worker 取得执行权递增；旧 epoch 不得提交状态、事件或结果。
- `release_id` 在 `SubmitRun` 时绑定当前有效 release；绑定后不可变。范围变化必须创建新 run。
- `result_version` 属于 run，发布后不可变。
- LLM 预算作用域固定为 `principal_id + session_id`；创建新 run、修订或重试都不重置额度。

## 3. 状态机

公开状态：

```text
QUEUED | RUNNING | WAITING_HITL | EXPIRED | SUCCEEDED | FAILED | CANCELLED
```

`EXPIRED / SUCCEEDED / FAILED / CANCELLED` 是终态，不可恢复。

| 当前状态 | 事件 | 新状态 | 原子副作用 |
| --- | --- | --- | --- |
| 无 | `SubmitRun` | `QUEUED` | 创建 session（如需）、run、receipt、初始事件 |
| `QUEUED` | worker claim | `RUNNING` | 写 lease_owner、递增 lease_epoch、写开始事件 |
| `RUNNING` | 创建人工中断 | `WAITING_HITL` | 先保存 checkpoint，再创建 interrupt 和 interrupt 事件 |
| `WAITING_HITL` | 合法 `ResumeRun` | `QUEUED` | 消费 interrupt、保存回答命令、写恢复事件 |
| `WAITING_HITL` | 30 分钟到期 | `EXPIRED` | 关闭 interrupt，写终态事件 |
| `RUNNING` | 验证并发布成功 | `SUCCEEDED` | 保存不可变结果、验证摘要和 result 事件 |
| `RUNNING` | 不可恢复失败 | `FAILED` | 写 terminal_reason 和 error 事件 |
| `QUEUED / WAITING_HITL` | 合法取消 | `CANCELLED` | 关闭 interrupt，写终态事件 |
| `RUNNING` | 取消请求 | 保持 `RUNNING` | 写 `cancel_requested_at`；worker 在安全检查点转 CANCELLED |

并发规则：

- 所有状态提交使用 `WHERE run_id = ? AND state_version = expected`。
- 发布与取消竞争时，只有一个条件提交成功。
- 若发布先提交，后到取消返回 `RUN_ALREADY_TERMINAL`；若取消先提交，worker 不得发布结果。
- RUNNING 期间租约过期时，新 worker 可递增 `lease_epoch` 接管，旧 worker 的后续写必须失败。

## 4. 运行时间

- `active_execution_remaining_ms` 初始为 600000，即主动执行 10 分钟。
- 只有 RUNNING 且由当前 lease epoch 执行的耗时计入此额度。
- WAITING_HITL 不消耗主动执行额度。
- 单次模型调用不超过 30000ms，也不得超过剩余执行额度。
- 单次人工中断等待 30 分钟；每个 run 同时最多一个 `OPEN` interrupt。
- 达到主动执行上限转 `FAILED / EXECUTION_TIMEOUT`。

## 5. 接口契约

### 5.1 SubmitRun

```text
SubmitRunCommand
  principal_id: from trusted context
  session_id: UUID | null
  task_kind
  input: TextInput | StructuredProfileInput | ResultReferenceInput
  idempotency_key: string

RunReceipt
  accepted: bool
  reason: string | null
  run_id
  session_id
  state
  stage
  state_version
  first_event_seq
  replayed: bool
```

规则：

- `accepted=true` 只表示持久受理，不表示图已运行或方案已完成。
- 未传 session 时创建新 session。
- 传入 session 时必须校验 principal 所有权。
- `GENERATE_PLAN / REVISE_PLAN` 最终必须产生已确认 Profile；`POLICY_QA` 只能要求问题所需范围；`EXPLAIN_RESULT` 必须绑定父 result。

### 5.2 ResumeRun

```text
ResumeRunCommand
  principal_id: from trusted context
  run_id
  interrupt_id
  expected_state_version
  profile_version: required for ConfirmProfile
  answer:
    AnswerFields | ConfirmProfile | RejectOrEditProfile | DeclineFields
  idempotency_key
```

规则：

- interrupt 必须属于该 run，状态必须为 `OPEN` 且未过期。
- `expected_state_version` 必须匹配。
- 同一 interrupt 只能被一个命令消费。
- 命令成功后 run 转为 `QUEUED`，由 worker 在 checkpoint 上应用一次回答。
- Resume 不等待图执行完成。

### 5.3 WatchRun

```text
WatchRunQuery
  principal_id
  run_id
  after_event_seq: int64
```

事件：

```text
RunEvent
  run_id
  event_seq: int64
  type: STATE_CHANGED | PROGRESS | INTERRUPT | RESULT | ERROR
  stage
  state_version
  occurred_at
  payload_version
  payload
```

规则：

- `event_seq` 在 run 内严格递增且无空洞。
- 先持久化事件，再通知订阅者。
- SSE heartbeat 不是业务事件，不占 `event_seq`。
- 业务事件保留 7 天；游标早于窗口返回 `EVENT_CURSOR_EXPIRED`，客户端应调用 `GetRun`。
- WatchRun 不推进图。

### 5.4 GetRun

```text
RunView
  run_id / session_id / task_kind
  state / stage / terminal_reason
  state_version
  release_id
  confirmed_profile_version
  active_execution_remaining_ms
  cancel_requested_at: RFC3339 UTC | null
  current_interrupt: InterruptView | null
  result: { result_version, result_kind } | null
  created_at / updated_at
  expires_at
```

`GetRun` 必须执行 principal 所有权校验。未完成 run 的结果引用为 null。
`state`、`state_version` 与 `current_interrupt` 必须来自同一数据库快照；不得返回
旧 run 状态配新 interrupt 的混合视图。状态与 interrupt 组合不一致时，调用方无法
安全构造 `ResumeRun`。

### 5.5 CancelRun

```text
CancelRunCommand
  principal_id
  run_id
  idempotency_key
```

- QUEUED / WAITING_HITL 直接转 CANCELLED。
- RUNNING 写 cancel 标志；当前 worker 在安全点停止并将 run 转 CANCELLED。
- 终态 run 返回 `accepted=false`、`reason=RUN_ALREADY_TERMINAL`，不改变终态。

## 6. Interrupt

```text
Interrupt
  interrupt_id: UUID
  run_id
  kind: INTAKE_ASK | PROFILE_CONFIRM | BACKFILL_ASK
  state: OPEN | CONSUMED | EXPIRED | CANCELLED
  question_key: stable key
  payload_version
  payload
  answer_schema_version
  created_at
  expires_at
  consumed_by_command_id
```

- 人类展示文案和字段 key 分离。
- `ConfirmProfile` 必须绑定 `profile_version`。
- 服务端不得根据自由文本猜测用户是在确认还是改分数字段。
- interrupt 必须“先有 durable checkpoint，后对外可见”。

## 7. Idempotency 与 Receipt

幂等作用域：

```text
principal_id + operation + target_run_id(or session_id) + idempotency_key
```

保存内容：

```text
request_hash
response_receipt
created_at
expires_at
```

规则：

- 同 key、同 request_hash：返回原 receipt，`replayed=true`；不重复执行。
- 同 key、不同 request_hash：返回 `IDEMPOTENCY_CONFLICT`。
- 不同 key、同一已消费 interrupt：返回 `INTERRUPT_CONSUMED`。
- 命令回执保留 7 天。

## 8. Release 与 Profile 绑定

- Submit 时可以尚不知道省年科批，但必须绑定当前有效、不可变的 `release_id`；没有可用 release 时返回 `RELEASE_UNAVAILABLE`。
- 画像解析完成后，校验其省年科批是否被已绑定 release 支持；不支持时失败，不能换成另一个 release。
- 同一 run 内不得切换 release；release 更新或画像范围变化必须创建新 run。
- 每次 Resume 和发布前都校验 release 未撤销。
- 撤销时恢复命令返回 `RELEASE_REVOKED`；已运行 worker 不得继续发布。
- 算法节点只接受 `CONFIRMED` 的 `confirmed_profile_version`。
- 正式报告可注入 `role=report` 的受控叙事 adapter；其 prompt/schema 必须由
  LLMGateway 按当前 release 注册。叙事不得写候选、数值、顺序、资格或 EvidenceRef；
  越界输出 fail closed，超时/限流/不可用使用明确标注的确定性模板降级。

## 9. 持久化与原子性

至少需要以下逻辑记录：

| 记录 | 职责 |
| --- | --- |
| `run` | 生命周期、stage、版本、release、租约、结果引用 |
| `interrupt` | 当前和历史上的人工中断 |
| `command_receipt` | 写命令幂等回执 |
| `run_event` | 持久事件与单调 `event_seq` |
| `profile_revision` | 不可变画像版本 |
| `run_formal_gate_artifact` | EvaluateEligibility 结果、可信 release 配置、Verifier 版本与适用日期快照，insert-only |
| `result_version` | 已通过门禁的不可变结果引用 |
| `run_evidence_package` | `V1_FORMAL` 已剥离证据包正文与输入/发布摘要，insert-only |
| `run_verification_report` | `V1_FORMAL` 完整 `VerificationReport` 与摘要，insert-only |
| `run_result_state_event` | 发布结果后的 `ACTIVE / STALE` 独立追加状态 |
| `data_retention_policy` | 版本化 7/30/180 天保留策略与 DELETE/DEIDENTIFY 动作 |
| `data_retention_sweep` / `data_retention_ledger` | 保留清理的非敏感审计、逐资源结果与行数 |

事务要求：

- 状态、事件和 outbox 在同一提交中写入，或通过可恢复事务 outbox 等价实现。
- 创建 interrupt 前，LangGraph checkpoint 必须已经持久化；若跨存储不能共事务，必须有幂等物化和恢复对账。
- Resume receipt、interrupt 消费和 QUEUED 状态必须原子提交。
- 结果发布必须同时校验 lease epoch、state_version、confirmed profile 和 release 有效性。
- 正式发布必须从 `run_formal_gate_artifact` 复核资格摘要、release manifest 配置、
  Verifier 版本和 `applicable_as_of`；缺失、摘要不一致或 artifact hash 冲突均 fail closed。
- `applicable_as_of` 固定取持久化 `run.created_at` 的日期，不取 worker 当前时钟；
  同一 run 在不同日期重建 artifact 时摘要必须稳定。
- `V1_FORMAL` 必须在同一事务写入结果、证据包、验证报告、`run.state=SUCCEEDED`、
  `result_version`、ACTIVE 状态和 RESULT 事件；数据库触发器拒绝重写正文。
- GET、Watch 与 RESULT 事件必须返回同一完整 `verification_report`，并原样返回
  `disclaimer / risk_notice / no_admission_guarantee`。
- 正式结果响应还以 additively added `report` 返回版本化、已通过门禁的确定性报告章节；
  报告中的候选身份、数值、单位和样本年份必须与已固化证据包一致。
- 同一 `result_version` 只能映射一份规范化输入摘要；重试重放原结果与事件，摘要冲突
  返回 `RESULT_VERSION_CONFLICT`。发布与取消竞争只有一个终态提交成功。
- Redis 仅作通知或可重建投影，不是权威状态。
- 保留清理只允许在显式 `app.retention_cleanup=on` 的事务中删除正式结果；普通业务
  写入仍不可改写。30 天后 run/session 保留去标识化 stub，不返回原用户私有数据。

## 10. 稳定错误码

| reason | 场景 |
| --- | --- |
| `RUN_NOT_FOUND` | run 不存在或不可见 |
| `RUN_FORBIDDEN` | principal 不拥有 session/run |
| `RUN_STATE_CONFLICT` | expected_state_version 不匹配 |
| `RUN_ALREADY_TERMINAL` | 对终态执行不允许的命令 |
| `INTERRUPT_NOT_FOUND` | interrupt 不属于该 run |
| `INTERRUPT_CONSUMED` | interrupt 已被其他命令消费 |
| `INTERRUPT_EXPIRED` | interrupt 已过期 |
| `IDEMPOTENCY_CONFLICT` | 同幂等键对应不同请求 |
| `RELEASE_REVOKED` | 绑定 release 已撤销 |
| `RELEASE_UNAVAILABLE` | Submit 时没有可绑定的有效 release |
| `RELEASE_MISMATCH` | 正式证据包与 run 绑定的 release 不一致 |
| `FORMAL_GATE_ARTIFACT_MISSING` | 本 run 缺少权威资格/配置 artifact |
| `FORMAL_GATE_ARTIFACT_MISMATCH` | 权威 artifact hash 不一致 |
| `FORMAL_GATE_ARTIFACT_CONFLICT` | 同一 run 试图写入不同 artifact |
| `QUALIFICATION_ARTIFACT_MISMATCH` | 调用方资格摘要与权威资格产物不一致 |
| `APPLICABLE_DATE_MISMATCH` | package 与权威 artifact 的适用日期不一致 |
| `PROFILE_VERSION_MISMATCH` | 正式证据包与 run 的确认 Profile 不一致 |
| `RESULT_VERSION_CONFLICT` | `result_version` 已绑定不同输入摘要或其他 run |
| `DELETION_IN_PROGRESS` | principal 删除处理中，禁止私有读取与新命令 |
| `PRINCIPAL_DELETED` | principal 已完成删除，禁止私有读取与新命令 |
| `PROFILE_NOT_CONFIRMED` | 数据依赖节点尝试读取未确认画像 |
| `QUEUE_OVERLOADED` | 无可用执行容量 |
| `EVENT_CURSOR_EXPIRED` | Watch 游标超出保留窗口 |
| `RESTORE_IN_PROGRESS` | 备份恢复尚未完成删除 ledger/tombstone 重放，业务读取保持关闭 |
| `DEPENDENCY_UNAVAILABLE` | 下游不可用且无合法回放 |
| `CONSENT_REQUIRED` | 使用敏感资格字段但缺少当前有效同意 |
| `CONSENT_REVOKED` | 当前 policy 下的敏感数据同意已撤销 |
| `SENSITIVE_DATA_NOT_ALLOWED` | 敏感原文试图进入不允许的数据边界 |

## 11. Terminal Reason

`FAILED` 和 `EXPIRED` 必须携带稳定原因：

- `INCOMPLETE_PROFILE`
- `UNSUPPORTED_SCOPE`
- `NO_RELIABLE_DATA`
- `VALIDATION_FAILED`
- `BUDGET_EXHAUSTED`
- `EXECUTION_TIMEOUT`
- `HITL_TIMEOUT`
- `RELEASE_REVOKED`
- `DEPENDENCY_UNAVAILABLE`
- `CONSENT_REQUIRED`
- `CONSENT_REVOKED`
- `SENSITIVE_DATA_NOT_ALLOWED`
- `INTERNAL_ERROR`

`CANCELLED` 固定原因 `CANCELLED_BY_USER`。原因写入 `terminal_reason`，不得只存在于日志。

## 12. 验收测试

| ID | 场景 | 期望 |
| --- | --- | --- |
| RL-01 | 两次 Submit 使用同 key 和同内容 | 返回同一 receipt，`replayed=true` |
| RL-02 | 同 key 不同 user_text | `IDEMPOTENCY_CONFLICT` |
| RL-03 | 双击同一确认 | 只消费一次 interrupt，第二次返回原 receipt |
| RL-04 | 两标签页提交不同回答 | 一个成功，另一个 `INTERRUPT_CONSUMED` |
| RL-05 | interrupt 可见前进程崩溃 | 不出现对外 OPEN 但无 checkpoint 的 interrupt |
| RL-06 | 回答受理后 worker 崩溃 | 接管 worker 只应用一次回答 |
| RL-07 | 旧 lease epoch 迟到提交 | 提交被拒绝，不覆盖新 worker |
| RL-08 | gateway 重启后重新订阅 | run 继续执行，可从 event_seq 补发 |
| RL-09 | 同一 run 并发发布与取消 | 仅一个终态提交成功 |
| RL-10 | 事件游标超过 7 天 | `EVENT_CURSOR_EXPIRED`，GetRun 仍可读 |
| RL-11 | Resume 时 release 已撤销 | `RELEASE_REVOKED`，不推进 |
| RL-12 | 主动执行累计超 10 分钟 | `FAILED / EXECUTION_TIMEOUT`，人工等待不计入 |
| RL-13 | 未确认画像传给算法 | `PROFILE_NOT_CONFIRMED` |
| RL-14 | 非所有者查询、订阅、恢复、取消 | 全部拒绝且不泄露画像 |
| RL-15 | 同一 session 连续创建多个修订 run | 预算跨 run 累计；新 run 或重试不重置额度 |
| RL-16 | 无可用 release 时 SubmitRun | `RELEASE_UNAVAILABLE`，不创建 run |
| RL-17 | 运行期间发布新 release | 当前 run 继续使用原 release，不混读 |

## 13. 非目标

- 不在本规格定义业务节点内部的算法；
- 不在本规格定义 LLM 预算预占和供应商重试；
- 不在本规格定义 SSE HTTP 细节、网关鉴权协议和前端重连实现；
- 不把 LangGraph checkpoint 编号暴露给客户端。
