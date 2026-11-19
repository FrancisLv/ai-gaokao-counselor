# Eligibility Rules Runtime Contract

| 项 | 内容 |
| --- | --- |
| 版本 | v1.0 |
| 状态 | 已确认；作为 M4 资格规则实现基线 |
| 上游 | [PRD v1.3 FR-03/FR-11/FR-13](../prd.md)、[Profile Contract](./profile-contract.md) |
| 关联 | [Retrieval Contract](./retrieval-contract.md)、[Evidence Contract](./evidence-contract.md)、[algo.proto](../../api/algo/v1/algo.proto) |

## 1. 目标与边界

本规格定义“候选是否可报”的确定性判定，以及规则如何从条款变成可发布、可审计、可回滚的规则版本。

本模块不生成候选，不计算概率，不把自然语言条款直接当规则条件，也不允许 LLM 决定 PASS。

候选身份来自当年的招生计划。历史录取数据只用于概率估计。

## 2. 核心不变量

1. **资格是候选级事实。** 结果必须绑定具体 `release_id`、院校专业组和可用的专业范围。
2. **三态结果。** 每个规则和每个候选只能得到 `PASS / FAIL / UNKNOWN`。
3. **未知不等于通过。** 缺用户字段、缺规则覆盖、规则冲突、表达式错误都产生 `UNKNOWN`。
4. **只有结构化规则能裁决。** RAG 文本只能作为规则来源和证据，不能直接执行句式相似度判断。
5. **规则必须审核并发布。** LLM 生成的规则最多是 `DRAFT`，不能进入在线 release。
6. **默认拒绝。** 无适用规则、规则无法解析、字段不合法时，不做宽松解释。
7. **不得用历史数据替代当年资格。** 历史专业组、历史选科文本和历史专业存在性都不构成当年报考资格。
8. **组内专业可能不同。** 组级汇总必须保留每个专业的状态，不能把“有一个专业可报”包装成“全组可报”。
9. **评估确定性。** 同输入、同 release、同规则集得到相同五态和相同顺序。

## 3. 规则模型

### 3.1 RuleVersion

```text
RuleVersion
  rule_id: stable string
  version: int32
  rule_key: string
  rule_type:
    SUBJECT_REQUIREMENT |
    SINGLE_SUBJECT_MIN |
    PHYSICAL_RESTRICTION |
    GENDER_RESTRICTION |
    BATCH_RESTRICTION |
    TUITION_LIMIT |
    OTHER_HARD
  scope:
    province
    application_year
    category
    batch
    university_id
    major_group
    major_code | null
  required_profile_fields: list<field_key>
  when: RuleExpression
  expression_version: int32
  outcome_when_true: FAIL
  reason_code: stable string
  source_evidence: list<EvidenceRef>
  review_status: DRAFT | REVIEWED | PUBLISHED | REVOKED
  valid_from / valid_to
  release_id
```

规则不得使用任意脚本、SQL 片段、网络地址或模型提示词作为条件。

### 3.2 受限表达式

允许的表达式：

```text
AND(left, right)
OR(left, right)
NOT(expr)
EQ(field, value)
NEQ(field, value)
IN(field, values)
GTE(field, value)
LTE(field, value)
PRESENT(field)
CONTAINS_ALL(field, values)
CONTAINS_ANY(field, values)
SUBJECT_MATCH(subject_req, profile.subjects)
```

表达式规则：

- `field` 必须属于 `required_profile_fields`；
- 表达式使用 `expression_version=1` 的 typed AST；未声明版本的规则不得进入在线评估；
- `AND / OR` 使用 left/right，`NOT` 使用 child；原子节点按字段类型接受标量或列表字面量；
- `required_profile_fields` 必须与 AST 实际引用字段集合完全一致，重复、遗漏或多余均拒绝发布；
- 字段值缺失 -> `UNKNOWN`；
- 值类型不匹配 -> `UNKNOWN`，不是 `FAIL`；
- `PRESENT` 对缺失、`UNKNOWN`、`DECLINED` 或类型错误均不得返回 MATCH；
- `AND` / `OR` / `NOT` 使用 Kleene 三值逻辑：确定的 NO_MATCH 或 MATCH 不被无关 INDETERMINATE 分支污染；
- 结构化字段必须绑定 `structured evidence`；
- 表达式执行器只能读取 allowlist 字段。
- 资源上限固定为深度 16、节点数 64、单个列表字面量 32 项、单个字符串 256 字节、表达式 JSON 32 KiB；
- JSON 中出现未允许的节点键、operator、字段、错误 arity 或超限内容时拒绝发布；运行时残留异常返回稳定 BLOCKED / `RULE_EXPRESSION_INVALID`，未知字段返回 `RULE_UNKNOWN_FIELD`。

### 3.3 规则集与覆盖声明

```text
RuleSetRelease
  ruleset_version: string
  release_id: string
  scope: EligibilityScope
  rules: list<RuleVersion>
  coverage: list<RuleCoverage>

RuleCoverage
  rule_type
  scope
  status: FULL | PARTIAL | NONE
  covered_data_sources: list<string>
  notes: string
```

正式推荐的规则集必须明确声明覆盖状态：

- `FULL`：该 `rule_type` 在该 scope 下所有需要检查的候选都已覆盖；
- `PARTIAL`：只覆盖部分候选/来源，未覆盖项为 `UNKNOWN`；
- `NONE`：没有可用规则，不能推断“没有要求”。

V1 当前仅有选科的可用规则时，其他硬规则的类型必须声明为 `NONE`。

## 4. 资格判定

```text
EligibilityScope
  province: string
  application_year: int32
  category: string
  batch: string
  university_id: int64 | null
  major_group: string | null
  major_code: string | null
```

### 4.1 候选与专业

```text
EligibilityRequest
  principal_id: trusted identity
  release_id: string
  confirmed_profile_version: string
  scope: EligibilityScope
  candidate_groups: list<CandidateGroup>
```

```text
CandidateGroup
  university_id: int64
  university_code: string
  university_name: string
  major_group: string
  majors: list<CandidateMajor>

CandidateMajor
  major_code: string
  major_name: string
  subject_req: string | null
  tuition_cny_per_year: decimal | null
```

每个 `CandidateGroup` 可包含多个 `major_code`。

单个专业的状态：

- `PASS`：所有适用规则均为 PASS，且相关覆盖为 FULL；
- `FAIL`：至少一条适用的 `DENY` 规则为 TRUE；
- `UNKNOWN`：任一适用规则无法求值，或覆盖不是 FULL；
- `NOT_APPLICABLE`：该专业在当前 scope 下不存在/不招生；
- `MISSING_REQUIRED_FACT`：缺少规则需要的用户字段，最终汇总为 UNKNOWN。

组级汇总：

```text
group_status:
  PASS             # 所有声明参与展示的专业均为 PASS
  PASS_PARTIAL     # 至少 1 个 PASS，且存在 FAIL/NOT_APPLICABLE，但无 UNKNOWN
  FAIL             # 没有任何 PASS
  UNKNOWN          # 任一相关专业 UNKNOWN
```

正式报告可以展示 `PASS_PARTIAL`，但必须列出受限专业；不得声称全组专业均可报。`UNKNOWN` 候选不得进入 `V1_FORMAL` 最终方案。

```text
MajorEligibility
  major_code
  status: PASS | FAIL | UNKNOWN | NOT_APPLICABLE | MISSING_REQUIRED_FACT
  failure_rule_keys: list<string>
  unknown_reasons: list<string>
  evidence_refs: list<EvidenceRef>

GroupEligibility
  group_key
  group_status: PASS | PASS_PARTIAL | FAIL | UNKNOWN
  majors: list<MajorEligibility>
  evidence_refs: list<EvidenceRef>
```

### 4.2 评估顺序

1. 校验 release、scope、Profile 已确认。
2. 只选择 `PUBLISHED` 且适用域匹配的规则。
3. 对每条规则检查用户字段 presence 和类型。
4. 在受限表达式上求值，得到 `MATCH / NO_MATCH / INDETERMINATE`。
5. 同一 `rule_key` 出现多个生效版本：标记 `RULE_CONFLICT`。
6. 汇总专业状态，再汇总组状态。
7. 生成 `EligibilityDecision`、失败原因和证据引用。
8. 将结果稳定排序：group key、major code、rule key。

`PASS` 产生条件：

- 当前 scope 没有适用的拒绝规则；且
- 该 scope 所需覆盖声明全部为 FULL；且
- 没有 INDETERMINATE 规则。

缺少覆盖时，结果必须是 `UNKNOWN / RULE_COVERAGE_MISSING`，不得省略该候选后假装成功。

## 5. Module Interface

```text
EvaluateEligibility(request: EligibilityRequest) -> EligibilityResult
```

```text
EligibilityResult
  status: COMPLETE | PARTIAL | BLOCKED
  groups: list<GroupEligibility>
  coverage: list<RuleCoverage>
  evidence_bundle_id: string
  diagnostics:
    evaluated_rule_count
    conflict_count
    unknown_count
```

字段语义：

- `COMPLETE`：所有候选都有确定性判定，且没有 UNKNOWN；
- `PARTIAL`：部分候选有判定，但仍存在 UNKNOWN 或覆盖不足；
- `BLOCKED`：不能形成合法候选集，例如 release 撤销或规则集冲突。

这是规则层的唯一 seam。调用方不能自行解释规则文本，也不能根据 RAG 是否召回改写结果。

内部隐藏：

- 规则选择与版本解析；
- 表达式求值；
- 缺字段判定；
- 同组多专业汇总；
- 冲突检测；
- 证据包生成；
- 规则执行顺序与错误归一化。

## 6. 发布与回滚

规则状态机：

```text
DRAFT -> REVIEWED -> PUBLISHED -> REVOKED
```

- LLM 只可产出 DRAFT；
- REVIEWED 必须有人类 domain owner；
- PUBLISHED 才能进入在线 release；
- REVOKED 规则不得参与新 run；
- 规则修改必须创建新 version，不能原地覆盖；
- 发布时同 `rule_key` 只能有一个适用 PUBLISHED 版本；
- release 撤销时，相关 run 的 Resume 和正式发布阻断。

## 7. 与现有算法的关系

- `EstimateProb` 继续生成候选池和概率；
- 当前 `CheckConstraints` 的选科逻辑只覆盖 `SUBJECT_REQUIREMENT`；
- 单科、体检、性别、批次、学费必须等待规则集和可用输入字段发布，不能只用 proto 注释宣称已实现；
- 扩展到新规则类型时，优先扩展 `EvaluateEligibility` 契约，不在每个调用方重写 if/else；
- 规则结果不得修改候选概率或历史数据。

## 8. 稳定错误与未知原因

| reason | 含义 |
| --- | --- |
| `RULE_COVERAGE_MISSING` | 该 scope/rule type 未完整覆盖 |
| `RULE_EXPRESSION_INVALID` | 规则表达式无法解析 |
| `RULE_CONFLICT` | 同一规则键存在冲突版本 |
| `RULE_UNKNOWN_FIELD` | 规则引用不在 allowlist 的字段 |
| `PROFILE_FACT_MISSING` | 用户字段缺失/UNKNOWN/DECLINED |
| `RULE_SOURCE_MISSING` | 规则没有可追溯证据 |
| `CANDIDATE_NOT_IN_PLAN` | 候选不在当年计划中 |
| `BATCH_UNSUPPORTED` | 批次不在 V1 支持范围 |
| `PROFILE_UNSUPPORTED_SCOPE` | 请求省年科批不在绑定 release 支持范围 |

## 9. 验收测试

| ID | 场景 | 期望 |
| --- | --- | --- |
| ER-01 | 用户选科满足规则 | 专业 PASS；证据含规则和原始条款 |
| ER-02 | 用户选科不满足 | 专业 FAIL，reason 可审计 |
| ER-03 | 规则需要单科成绩，用户 UNKNOWN | 专业 UNKNOWN，不进入正式方案 |
| ER-04 | 规则表达式含未授权字段 | `RULE_EXPRESSION_INVALID` |
| ER-05 | 规则覆盖声明 PARTIAL | 未被覆盖的候选 UNKNOWN |
| ER-06 | 组内 2 个专业 PASS，1 个受限 | `PASS_PARTIAL`，报告列出受限专业 |
| ER-07 | 组内存在 UNKNOWN | 组 UNKNOWN |
| ER-08 | 同一 rule_key 两个生效版本 | `RULE_CONFLICT` |
| ER-09 | 历史专业组不在当年计划 | `CANDIDATE_NOT_IN_PLAN`，不能作为 PASS |
| ER-10 | LLM 生成的规则未审核 | 不参与在线结果 |
| ER-11 | 规则对应 release 撤销 | 评估阻断，输出 `RELEASE_REVOKED` |
| ER-12 | 同输入重复评估 | 五态、原因和顺序完全一致 |

## 10. 非目标

- 不生成候选；
- 不计算概率或效用；
- 不把政策问答回答当成资格判定；
- 不在本规格实现具体规则表 schema；
- 不实现规则审核 UI。
