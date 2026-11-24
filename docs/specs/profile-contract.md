# Profile Runtime Contract

| 项 | 内容 |
| --- | --- |
| 版本 | v1.0 |
| 状态 | 待确认；确认后作为 M1/M2 实现基线 |
| 上游 | [PRD v1.3 §4、§6.1、§13](../prd.md)、[GLOSSARY.md](../../GLOSSARY.md) |
| 关联 | [Run Lifecycle Contract](./run-lifecycle.md)、[algo.proto](../../api/algo/v1/algo.proto) |

## 1. 目标与边界

本规格定义一个考生画像如何被创建、补全、校验、确认、版本化和映射为算法输入。

它保证：

- LLM 不能决定画像是否完整；
- 用户修改分数等字段后，旧画像及下游产物不会被悄悄复用；
- `UNKNOWN / DECLINED` 不会被当成满足条件；
- Profile 到 `EstimateProbRequest` 的字段转换只有一个确定入口。

不在本规格内：

- 认证和用户数据删除；
- 候选级资格规则；
- LLM prompt 与追问文案；
- run 生命周期和 interrupt 状态。

## 2. 核心不变量

1. **Profile revision 不可变。** 每次字段变化创建新 revision，已确认 revision 不原地修改。
2. **服务端生成版本。** `profile_version` 和 `revision_no` 由服务端生成，客户端不能指定。
3. **确认绑定具体版本。** `PROFILE_CONFIRM` 只对指定 `profile_version` 生效。
4. **下游只消费确认版本。** `EstimateProb` 及后续规则、证据和报告不得读取 `DRAFT`。
5. **全局必需字段不可用 UNKNOWN / DECLINED 代替。** 无法补齐时结束为 `INCOMPLETE_PROFILE`，不能进入个性化推荐。
6. **条件字段只在规则需要时阻断。** 未被当前候选规则要求的缺失条件字段不产生额外追问。
7. **算法推导值不覆盖用户事实。** `rank`、`percentile`、`batch_control_line` 只能以 `DERIVED` 状态写入。
8. **字段名唯一。** V1 使用 `application_year`、`category`、`score`；禁止继续使用 `track`、`score_or_rank`。

## 3. Runtime 数据结构

```text
ProfileRevision
  profile_version: opaque string        # UUID，服务端生成
  session_id: UUID
  revision_no: int64                    # session 内单调递增
  based_on: string | null               # 上一 profile_version
  lifecycle: DRAFT | CONFIRMED | SUPERSEDED
  fields: map<field_key, ProfileField>
  created_at: RFC3339 UTC
  confirmed_at: RFC3339 UTC | null

ProfileField<T>
  state: PROVIDED | UNKNOWN | DECLINED | DERIVED | DEFAULT
  source: USER_TEXT | USER_FORM | ALGORITHM | SYSTEM_DEFAULT
  value: T | null
```

约束：

- `ProfileField.value != null` 当且仅当 `state` 为 `PROVIDED / DERIVED / DEFAULT`。
- 字段 key 不存在表示服务端从未采集该字段；`UNKNOWN` 和 `DECLINED` 是用户明确表达。
- revision 的 `fields`、`based_on`、`session_id` 和 `revision_no` 创建后不可修改。
- `CONFIRMED` revision 只能变为 `SUPERSEDED`；内容仍保持不变。

## 4. 字段目录

### 4.1 全局必需

| field_key | 类型 | 规则 |
| --- | --- | --- |
| `province` | string | V1 仅允许 `湖南` |
| `application_year` | int32 | 必须由 release 清单声明为支持 |
| `category` | string | `物理类` 或 `历史类` |
| `batch` | string | V1 仅允许 `本科批`；缺失时写 `DEFAULT`，确认后生效 |
| `score` | int32 | `0 <= score <= 750`；V1 只接受分数，不接受 rank-only |
| `subjects` | list<string> | 恰好 3 门且互不重复；首选必须与 `category` 一致，再选恰好 2 门 |

选科合法值：

- 首选：`物理`、`历史`；
- 再选：`化学`、`生物`、`政治`、`地理`。

例如：

- `物理类 + ["物理","化学","生物"]` 合法；
- `历史类 + ["历史","政治","地理"]` 合法；
- 重复科目、只有 2 门、首选与科类不一致均返回 `PROFILE_INVALID_VALUE`。

### 4.2 可推导字段

| field_key | 类型 | 产生方式 |
| --- | --- | --- |
| `rank` | int32 | `RankConvert` 或算法内部换算后写回 |
| `percentile` | double | `rank / total_count` |
| `batch_control_line` | int32 | 当前 release 对应一分一段表 |

这些字段只能由 `ALGORITHM` 写入，且不得作为用户必须回答的问题。

### 4.3 条件事实

条件事实使用稳定 field key，后续规则只能引用这里声明或由新版本 spec 扩展的 key。

| field_key | 类型 | 说明 |
| --- | --- | --- |
| `physical_exam.color_vision` | `NORMAL / COLOR_WEAK / COLOR_BLIND` | 色觉结论 |
| `single_subject_scores.<subject>` | int32 | 单科成绩；范围由规则 spec 校验 |
| `foreign_language` | string | 外语语种 |
| `gender` | `MALE / FEMALE` | 用户可拒绝，拒绝值记录为 `DECLINED` |
| `graduate_status` | `FRESH / PREVIOUS` | 应届或往届 |
| `household_province` | string | 户籍省份，用于专项计划资格 |
| `school_registration_province` | string | 学籍省份，用于专项计划资格 |

条件字段规则：

- 未被任何当前候选规则要求时可以缺失；
- 规则要求但字段缺失、为 `UNKNOWN` 或 `DECLINED` 时，该规则结果为 `UNKNOWN`；
- 条件字段缺失不得被解释为“无要求”或“满足要求”。

### 4.4 可选偏好

| field_key | 类型 | 缺省行为 |
| --- | --- | --- |
| `preferences.risk_appetite` | `conservative / balanced / aggressive` | 不阻断；算法使用自身默认值，并标记来源为系统默认 |
| `preferences.preferred_cities` | list<string> | 空列表 |
| `preferences.major_interests` | list<string> | 受控本科专业名称或用户明确确认的相关专业名称；空列表 |
| `preferences.required_major_names` | list<string> | 仅由“只看 / 仅限 / 必须”产生；按当前计划专业名称精确包含 |
| `preferences.priority_dims` | subset of `tier / major / location / strength` | 空列表 |
| `preferences.preferred_tiers` | subset of `985 / 211 / 双一流` | 空列表 |
| `preferences.max_tuition_cny_per_year` | decimal >= 0 | 空；**当前 algo proto 尚无此字段，V1 不得声称已执行学费过滤** |

可选偏好缺失不改变“画像是否可用”，只影响确定性默认值或后续排序。不得再使用无法量化的“低置信”概念替代字段状态。

确定性默认值：

- `risk_appetite=balanced`；
- `preferred_cities=[]`；
- `major_interests=[]`；
- `required_major_names=[]`；
- `priority_dims=[]`，由 algo 配置的 `DefaultWeights` 决定效用权重；
- `preferred_tiers=[]`；
- `max_tuition_cny_per_year=null`。

## 5. 确定性校验

```text
Validate(profile_version, release_scope) -> ProfileValidationReport
```

`ProfileValidationReport`：

```text
readiness: READY_FOR_CONFIRMATION | NEEDS_INPUT | UNSUPPORTED | CONFIRMED
findings:
  - code
  - field_keys
  - blocking: bool
    message_key: stable i18n key
```

校验顺序固定：

1. 校验 revision 存在且属于当前 session。
2. 校验全局字段状态和值。
3. 校验 province / year / category / batch 是否被 release 支持。
4. 校验选科组合。
5. 校验可选偏好值。
6. 返回排序稳定的 findings。

稳定错误码：

| code | blocking | 含义 |
| --- | --- | --- |
| `PROFILE_REQUIRED_MISSING` | 是 | 全局必需字段不存在或状态为 UNKNOWN/DECLINED |
| `PROFILE_INVALID_VALUE` | 是 | 类型、范围或枚举非法 |
| `PROFILE_UNSUPPORTED_SCOPE` | 是 | 省 / 年 / 科 / 批不在当前 release 支持范围 |
| `PROFILE_UNSUPPORTED_INPUT` | 是 | 仅提供位次或其它 V1 未支持输入 |
| `PROFILE_OPTIONAL_IGNORED` | 否 | 偏好已采集但当前算法不消费 |
| `PROFILE_CONDITIONAL_NOT_READY` | 条件性 | 条件字段缺失或不确定，仅供规则层使用 |

`READY_FOR_CONFIRMATION` 只说明画像可供用户确认，不代表候选资格已通过。

## 6. 修改与确认

### 6.1 修改

- 修改只能作用于 `DRAFT` revision；
- 每次修改产生新 `profile_version` 和递增的 `revision_no`；
- 已确认版本收到修改请求时，必须由新 run 创建基于旧版本的新 draft；
- 修改请求携带作为修改基础的 `base_profile_version`；只有当前 session 的最新 `DRAFT` 能作为 base，否则返回 `PROFILE_VERSION_CONFLICT`。

### 6.2 确认

- 只有 `Validate` 返回 `READY_FOR_CONFIRMATION` 的 draft 能确认；
- 确认记录包含 `profile_version`、`confirmed_at`、`principal_id`；
- 确认后 `confirmed_profile_version` 才能传给算法；
- 确认期间生成新版本时，旧确认命令返回 `PROFILE_VERSION_CONFLICT`；
- 画像任一字段变化都会使旧候选、规则、证据、优化和报告失效；V1 不在同一 run 内复用。

## 7. Module Interface

Profile 是一个深模块。调用方只通过以下接口使用它：

```text
CreateDraft(session_id, base_profile_version?) -> ProfileRevision
ApplyPatch(session_id, base_profile_version, patch, idempotency_key)
    -> ProfileRevision
Validate(profile_version, release_scope) -> ProfileValidationReport
Confirm(profile_version, actor) -> ConfirmedProfile
ToAlgoInput(confirmed_profile_version) -> EstimateProbInput
```

隐藏在本模块内部：

- revision 编号和版本冲突；
- 字段 merge 与 source 优先级；
- 选科规范化；
- release 支持范围校验；
- 修改后的 draft 复制；
- 错误排序和 Message key。

`Confirm` 和 `ApplyPatch` 是唯一外部写操作；读取其他模块时只能使用不可变 revision。

## 8. 算法映射

`ToAlgoInput` 是 Profile 到现有 `EstimateProbRequest` 的唯一适配器。

| Profile | algo |
| --- | --- |
| `province` | `province` |
| `application_year` | `year` 的十进制字符串 |
| `category` | `category` |
| `batch` | `batch` |
| `score` | `score` |
| `subjects` | `subjects` |
| `preferences.risk_appetite` | `profile.risk_appetite` |
| `preferences.priority_dims` | `profile.priority_dims` |
| `preferences.major_interests` | `profile.interests` |
| `preferences.required_major_names` | `profile.required_major_names` |
| `preferences.preferred_cities` | `profile.preferred_cities` |
| `preferences.preferred_tiers` | `profile.preferred_tiers` |
| `preferences.max_tuition_cny_per_year` | 暂无映射 |

映射失败必须返回稳定错误，不允许产生零值请求。

## 9. 验收测试

| ID | 场景 | 期望 |
| --- | --- | --- |
| PC-01 | 完整湖南物理类画像 | `READY_FOR_CONFIRMATION`，映射字段一致 |
| PC-02 | 缺 `application_year` | `PROFILE_REQUIRED_MISSING` |
| PC-03 | 只给位次 | `PROFILE_UNSUPPORTED_INPUT`，不反推分数 |
| PC-04 | 选科只有两门或科类不一致 | `PROFILE_INVALID_VALUE` |
| PC-05 | 未给 batch | 生成 `DEFAULT=本科批`，确认后可用 |
| PC-06 | 非本科批 | `PROFILE_UNSUPPORTED_SCOPE` |
| PC-07 | release 不覆盖请求年份 | `PROFILE_UNSUPPORTED_SCOPE`，不回退 |
| PC-08 | 未确认直接调用算法映射 | `PROFILE_NOT_CONFIRMED` |
| PC-09 | 两个请求基于同一版本修改 | 一个成功，另一个 `PROFILE_VERSION_CONFLICT` |
| PC-10 | 已确认后修改分数 | 产生新 draft，旧确认版本保持不可变 |
| PC-11 | 条件字段 UNKNOWN | 画像本身可继续，规则层不得按 PASS 处理 |
| PC-12 | 学费预算存在 | 返回值保留该字段，但标记 `PROFILE_OPTIONAL_IGNORED` |
| PC-13 | “我想学物理学” | `major_interests=["物理学"]`，不推断 `category` 或相关专业 |
| PC-14 | “我只想看物理学” | `required_major_names=["物理学"]`，只匹配当前计划中名称精确命中的专业 |
