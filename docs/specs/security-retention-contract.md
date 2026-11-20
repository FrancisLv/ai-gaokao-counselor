# Security and Retention Runtime Contract

| 项 | 内容 |
| --- | --- |
| 版本 | v1.0 |
| 状态 | 待确认；确认后作为 M2/M5 安全实现基线 |
| 上游 | [PRD v1.3 §4.2、§7.4、T16/T18](../prd.md)、[Profile Contract](./profile-contract.md)、[LLM Gateway Contract](./llm-gateway-contract.md) |
| 关联 | [Run Lifecycle Contract](./run-lifecycle.md)、[Evidence Contract](./evidence-contract.md) |

## 1. 目标与边界

本规格定义身份、授权、敏感数据、用户同意、日志脱敏、删除、备份恢复、保留期限和发布前安全检查。

本规格提供技术控制，不宣称自动满足任何地区的法律、教育监管或未成年人保护要求。正式公开部署前仍需法律与安全评审。

## 2. 数据分类

| class | 内容 | 默认去向 |
| --- | --- | --- |
| `PUBLIC_DATA` | 公开招生章程、政策、计划、分数 | 可长期保存，可进入 RAG |
| `PRINCIPAL_DATA` | 匿名 principal、session、run 归属 | PostgreSQL，受限访问 |
| `INQUIRY_DATA` | 省份、分数、选科、偏好、成绩 | PostgreSQL，日志脱敏 |
| `SENSITIVE_ELIGIBILITY_DATA` | 色觉、视力、健康限制、性别、户籍/学籍等条件事实 | 最小化、加密、默认不进入云模型 |
| `OPERATIONAL_DATA` | 事件、检查点、结果、证据包、调用账本 | 受保留策略控制 |
| `SECRET` | 模型 key、数据库凭据、签名 key | secrets 系统，不进入数据库明文/日志 |

用户文本可能意外包含手机号、身份证号等直接标识。网关必须在持久化前做检测与脱敏；无法安全分离的输入应拒绝进入云模型链路。

## 3. Principal 与授权

### 3.1 Principal

```text
Principal
  principal_id: trusted server-side identity
  kind: AUTHENTICATED_USER | CONTROLLED_ANONYMOUS
  status: ACTIVE | DELETION_PENDING | DELETED
```

- `principal_id` 来自认证或服务端签名上下文；
- 请求正文中的 `principal_id`、`user_id` 一律不信任；
- 匿名 principal 使用短期受控会话凭证；
- principal 不可用后，所有私有查询返回 `RUN_FORBIDDEN` 或 `PRINCIPAL_DELETED`。

### 3.2 授权矩阵

| 操作 | 用户 | 运营/支持 | 内部服务 |
| --- | --- | --- | --- |
| 创建自己的 session/run | 允许 | 不可代建，除非明确支持流程 | 允许受控调用 |
| 读取自己的 run/result | 允许 | 需管理员角色与审计 | 允许内部调用 |
| 恢复自己的 interrupt | 允许 | 不可代替用户回答 | 不允许伪造用户回答 |
| 读取他人敏感画像 | 禁止 | 仅在授权审计下最小读取 | 不通过 Trace 绕过 |
| 删除自己的数据 | 允许 | 按请求处理 | 执行删除任务 |
| 修改公共 release/规则 | 禁止 | 仅发布管理员 | ETL 发布流程 |

`trace_id`、`session_id`、`run_id` 都不是授权凭证。

## 4. 同意与敏感数据

### 4.1 ConsentRecord

```text
ConsentRecord
  consent_id
  principal_id
  purpose: PLAN_GENERATION | SENSITIVE_ELIGIBILITY_EVALUATION
  data_classes: list<string>
  policy_version
  granted_at
  revoked_at: optional
  guardian_ack_recorded: bool
```

- 采集 `SENSITIVE_ELIGIBILITY_DATA` 前必须有对应 consent；
- 同意只覆盖列出的目的和数据类别；
- 同意撤销后，新的使用和模型调用立即停止；
- 用户明确标识为未成年人时，公网环境必须记录监护人确认或限制相关采集；
- consent 不得作为用户画像字段发送给模型。

### 4.2 敏感数据进入 LLM

- 健康、色觉、视力等原始字段不进入云模型；
- 规则层只向报告模型提供脱敏后的 `PASS/FAIL/UNKNOWN` 和限制原因；
- `ChatRequest.data_class` 使用 `PUBLIC_DATA / PRINCIPAL_DATA / INQUIRY_DATA /
  SENSITIVE_ELIGIBILITY_DATA / OPERATIONAL_DATA / SECRET` 六类枚举；未知枚举值
  fail closed；
- 每个 `llm_role_route.allowed_data_classes` 明确声明可进入云模型的安全子集；
  `SENSITIVE_ELIGIBILITY_DATA` 与 `SECRET` 永远不能出现在 route 允许集合，
  数据库约束与服务端检查双重拒绝；
- 旧 string 字段的 `PUBLIC / INTERNAL / SENSITIVE` 只作显式兼容：分别映射为
  `PUBLIC_DATA`、`PRINCIPAL_DATA`、`SENSITIVE_ELIGIBILITY_DATA`。`INTERNAL`
  不会扩大为公开数据；`SENSITIVE` 始终返回 `SENSITIVE_DATA_NOT_ALLOWED`；
- 网关在持久化、request hash 和 provider 调用前检测原始消息、模板变量及模板
  渲染后的完整消息，覆盖中国大陆手机号、18 位身份证号、邮箱、常见 key/token
  形态及健康/色觉/视力/性别/户籍/学籍原始事实。当前不尝试对自然语言做不完全
  可靠的自动改写，命中即拒绝；
- 只包含字段名与 `PASS/FAIL/UNKNOWN` 的脱敏规则结论可以参与生成；原始敏感
  描述与规则结论同时出现仍按原文拒绝；
- provider 输出返回前执行同一直接标识与敏感事实检查；结构化输出通过 schema
  后还会递归检查解码后的键和值。不安全模型内容不写入 response、
  `response_payload`、投影或错误文本；若已产生费用，按真实 usage 结算后返回
  `SENSITIVE_DATA_NOT_ALLOWED`；
- 共享缓存不得保存敏感 prompt 或敏感模型响应；
- trace、错误消息和业务事件不得包含健康明细。

## 5. 存储与日志

- 服务间传输必须使用 TLS；本地开发可例外但必须显式标记；
- PostgreSQL 和对象存储使用磁盘加密或等价基础设施控制；
- 敏感字段使用列级加密或字段级 envelope encryption；
- 日志只记录 trace、结构化错误码、版本、耗时和非敏感计数；
- 不记录完整 prompt、原始 provider 响应、未通过敏感守卫的模型内容、手机号、
  身份证号或健康原文；
- 错误响应不得回显 DSN、key、原始 SQL 或内部堆栈；
- 浏览器 SSE 不在 URL 放长期 token；
- 模型 key 只存在于 LLM Gateway secrets 环境。

## 6. 数据保留

| 数据 | 默认保留 | 删除方式 |
| --- | --- | --- |
| 公共文档/原文 | 长期 | 只在版权/安全/纠错需要时撤销 release |
| Profile draft | 终态后 30 天 | 删除或去标识化 |
| Session / run / interrupt | 终态后 30 天 | 关联删除 |
| `run_event` | 终态后 7 天 | 删除 |
| 成功 run 检查点 | 30 天 | 删除 |
| FAILED/EXPIRED 检查点 | 7 天 | 删除 |
| 命令幂等回执 | 7 天 | 删除 |
| 最终 result + evidence package | 30 天 | 删除 |
| LLM provider attempt 账本 | 180 天 | 去标识化，不保存 prompt |
| 安全/删除审计 tombstone | 180 天 | 不保存敏感字段 |
| PostgreSQL 备份 | 7 天 | 过期删除 |
| Redis RDB | 2 天 | 过期删除 |

“归档”只表示降低访问频率，不延长保留期，也不绕过删除。

## 7. 删除流程

### 7.1 DeletePrincipalData

```text
DeletePrincipalDataCommand
  principal_id: trusted context
  request_id: idempotency key
  confirmation_token
  reason: optional
```

`confirmation_token` 必须由服务端在完成重新认证后签发，绑定 principal 和删除请求，10 分钟后失效且只能消费一次。

网关在持久化前至少执行以下确定性脱敏：

- 中国大陆 18 位身份证号；
- 中国大陆 11 位手机号；
- 明确标记为 `SECRET` 的 key/token 形态。

脱敏不是对任意自然语言隐私识别的完整保证；无法安全分类的输入不得进入云模型或完整日志。

流程：

1. 鉴权并验证确认；
2. 写删除请求和 tombstone；
3. 立即撤销该 principal 的 session/run/result 访问；
4. 删除或去标识化 Profile、run、interrupt、event、checkpoint、result、evidence、缓存；
5. 调用账本去除 principal/session 关联，仅保留计费所需的去标识化记录；
6. 公共文档和公开统计保留；
7. 返回 `DELETION_ACCEPTED` 或 `DELETION_COMPLETED`。

### 7.2 幂等与失败

- 同一 `request_id` 返回同一 receipt；
- 部分失败时 principal 保持 `DELETION_PENDING`，继续重试；
- 未完成删除不得重新开放数据访问；
- 删除只影响用户数据，不删除公共资料；
- 删除任务必须记录已完成资源和失败资源，不记录被删内容。

## 8. 备份与恢复

- 备份恢复后必须先应用删除 tombstone 和删除请求 ledger，再开放业务；
- 恢复环境不得为了排查而复制敏感原文字段；
- 测试必须覆盖“备份中存在已删除画像，恢复后仍不可访问”；
- Redis 是可重建投影，恢复后不得重新引入已撤回的敏感结果；
- 备份过期的对象存储副本由清理任务删除。

运行实现使用全局恢复闸门，在恢复开始后立即把所有私有 RunLifecycle 读取标为
`RESTORE_IN_PROGRESS`。恢复 adapter 先重置可重建投影，再按 tombstone 与删除
ledger 重放 principal 清理；任一重放失败都保持闸门关闭，重试成功后同时写入最小
恢复审计后才开放。恢复审计只保存 restore id、principal 不可逆引用、删除 request
id、结果和稳定错误码，不保存 prompt、健康原文或被删内容。

## 9. Prompt 注入与输出安全

- RAG 文档、网页和模型输入中的指令都按不可信数据处理；
- 文档不能授予工具、改变系统提示、修改预算或跳过门禁；
- 工具调用采用白名单，未注册工具不可调用；
- SQL 只走参数化查询；
- 输出必须保留免责声明和风险提示；
- 禁止“保证录取”“一定上岸”等承诺；
- 资格 UNKNOWN、证据不足和保底不足不得被文案掩盖。

## 10. 安全审计

审计记录仅包含：

- 操作类型；
- principal 的去标识化引用；
- 资源类型和不可逆 ID；
- 时间、结果和错误码；
- 管理员角色与操作目的。

不包含用户原文、健康详情、模型 key 或完整 prompt。

## 11. 稳定错误

| reason | 行为 |
| --- | --- |
| `PRINCIPAL_REQUIRED` | 缺少可信身份 |
| `RUN_FORBIDDEN` | 非所有者访问 |
| `PRINCIPAL_DELETED` | 已删除主体访问 |
| `CONSENT_REQUIRED` | 敏感采集/使用无同意 |
| `CONSENT_REVOKED` | 同意撤销后继续使用 |
| `SENSITIVE_DATA_NOT_ALLOWED` | 敏感原文进入云模型 |
| `DELETION_IN_PROGRESS` | 删除处理中禁止访问 |
| `DELETION_FAILED_RETRYABLE` | 删除部分失败，保持 pending |
| `DELETION_CONFIRMATION_INVALID` | 删除确认缺失、过期、已消费或参数不匹配 |
| `RESTORE_IN_PROGRESS` | 备份恢复尚未完成删除 ledger/tombstone 重放 |
| `RETENTION_POLICY_INVALID` | 没有匹配保留策略 |
| `SECRET_EXPOSURE_BLOCKED` | 日志/响应检测到敏感凭据 |

## 12. 验收测试

| ID | 场景 | 期望 |
| --- | --- | --- |
| SR-01 | 用户 A 读取用户 B 的 run | `RUN_FORBIDDEN` |
| SR-02 | 正文伪造 principal_id | 服务端忽略，仍然拒绝越权 |
| SR-03 | 健康字段进入云模型请求 | `SENSITIVE_DATA_NOT_ALLOWED` |
| SR-04 | 健康字段进入日志/trace | 测试检出即失败 |
| SR-05 | 用户输入手机号/身份证号 | 持久化前脱敏或拒绝 |
| SR-06 | 未同意采集敏感资格字段 | `CONSENT_REQUIRED` |
| SR-07 | 同意撤销后发起新 run | `CONSENT_REVOKED` |
| SR-08 | 删除请求重复提交 | 返回同一 receipt |
| SR-09 | 删除处理中访问 run | `DELETION_IN_PROGRESS` |
| SR-10 | 删除后检查 profile/run/result/cache | 均不可访问，公共文档保留 |
| SR-11 | 备份恢复包含已删除数据 | 删除 ledger 重放后才开放 |
| SR-12 | RAG 文档包含恶意指令 | 不改变系统规则和工具白名单 |
| SR-13 | 报告承诺录取 | 发布门禁阻断 |
| SR-14 | 错误响应包含 DSN/key | 安全检查失败 |
| SR-15 | 到期数据超过保留策略 | 定时任务删除/去标识化并可审计 |

## 13. 非目标

- 不替代正式法律意见；
- 不定义公网身份供应商；
- 不定义运营后台 UI；
- 不把匿名 cookie 当作唯一合规保障；
- 不承诺所有用户输入都能自动识别全部直接标识。
