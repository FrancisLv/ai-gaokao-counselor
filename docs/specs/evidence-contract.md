# Evidence Runtime Contract

| 项 | 内容 |
| --- | --- |
| 版本 | v1.0 |
| 状态 | 已确认；作为 M4/M5 事实校验实现基线 |
| 上游 | [PRD v1.3 FR-18/FR-19](../prd.md)、[ADR-0004](../adr/0004-immutable-content-hash-release-id.md) |
| 关联 | [Retrieval Contract](./retrieval-contract.md)、[Eligibility Rules Contract](./eligibility-rules-contract.md)、[Run Lifecycle Contract](./run-lifecycle.md) |

## 1. 目标与边界

本规格定义报告中的事实如何绑定证据、如何校验、何时可以发布，以及文档、结构化和算法派生事实如何分别验证。

证据不是“有 URL”或“有引用标记”。证据必须能定位到指定 release 中的原件、结构化行或算法输入输出。

本规格不定义报告文案风格，不负责生成候选，也不允许 LLM 用解释替代确定性校验。

## 2. 核心不变量

1. 每条关键事实必须绑定至少一个 `EvidenceRef`。
2. 所有证据必须属于结果所用 `release_id`，不得跨 release 拼接。
3. 文档事实验证原文；结构化事实验证字段；派生事实重算或比对算法输出和输入快照。
4. 数字、单位、样本年份和顺序由确定性模板渲染，LLM 不得改写。
5. Verifier 的状态是 `VERIFIED / CONTRADICTED / UNVERIFIABLE`，不是“感觉正确”。
6. `CONTRADICTED` 不得自动修复文案；必须回退上游或阻断。
7. `UNVERIFIABLE` 的关键事实不能静默进入最终报告；可剥离非关键段落，关键事实则失败。
8. 引用不存在的学校、年份、条款或 release 是硬错误。
9. 证据必须可重复校验：同输入、同 release、同 verifier 版本得到同状态。

## 3. EvidenceRef

```text
EvidenceRef
  evidence_id: stable string
  evidence_type: DOCUMENT | STRUCTURED | DERIVED
  release_id: string
  content_sha256: string
  created_at: RFC3339 UTC
  producer_version: string
```

每个 `EvidenceRef` 必须能在对应类型的注册表中解析，否则视为 `DANGLING_EVIDENCE`。

## 4. 三类证据

### 4.1 DocumentEvidence

```text
DocumentEvidence
  doc_id: string
  chunk_id: string
  title: string
  document_type: string
  issuer: string
  source_uri: string
  fetched_at: RFC3339 UTC
  applicability:
    regions / categories / batches / valid_from / valid_to
  section_path: string
  excerpt_sha256: string
  char_start / char_end: optional locator
  excerpt: string
  release_id: string
```

校验：

- 原件和 chunk 存在且 hash 一致；
- 查询 scope 与文档适用域匹配；
- `section_path` 和 excerpt 能定位原文；
- 事实没有超出原文范围；
- 原文文本可以用于展示，但不能作为系统指令执行。

### 4.2 StructuredEvidence

```text
StructuredEvidence
  dataset: string
  table: string
  primary_key: string
  business_key:
    province / year / category / batch / university_code / major_group / major_code
  field_path: string
  value: canonical scalar/object
  unit: string | null
  source_year: int32 | null
  row_or_snapshot_sha256: string
  release_id: string
```

校验：

- 表、主键、业务键和字段存在；
- 值与同 release 的权威行/快照完全一致；
- 单位、精度和展示格式符合字段定义；
- 不把缺失值、NULL 或 0 当作“免费/无限制”；
- 结构化事实不得交给 RAG 反查。

### 4.3 DerivedEvidence

```text
DerivedEvidence
  operation_id: string
  algorithm: string
  algorithm_version: string
  config_sha256: string
  input_snapshot_sha256: string
  profile_version: string
  release_id: string
  sample_years: list<int32>
  output_field: string
  output_value: canonical scalar/object
  unit: string | null
  assumptions: list<string>
  warnings: list<string>
```

校验：

- 输入快照、Profile、release、算法和配置版本齐全；
- 在允许的 verifier 中确定性重算，或与已保存的不可变算法产物比对；
- 输出字段、单位、精度和样本年份一致；
- `slip_rate`、`expected_satisfaction` 等只能作为近似告警，不能改写成录取承诺。

## 5. Claim

```text
Claim
  claim_id: stable string
  claim_type: DOCUMENT_FACT | STRUCTURED_FACT | DERIVED_FACT
  criticality: CRITICAL | NORMAL
  normalized:
    subject: string
    predicate: string
    object: canonical scalar/object
    unit: string | null
  rendered_text: string
  evidence_refs: list<EvidenceRef>
```

关键事实定义为影响以下内容的陈述：

- 候选身份、专业组、专业代码；
- 招生资格与限制；
- 分数、位次、概率、计划数、学费；
- 样本年份、数据版本、风险告警；
- 免责声明和“不保证录取”。

文案可以变化，`normalized` 事实和数值不能变化。

## 6. Verification

```text
VerifyPackage(package: EvidencePackage) -> VerificationReport
```

`VerifyPackage` 是 Verifier 的唯一 seam。内部实现按 evidence type 分派，调用方不得根据文档 URL 或 LLM 置信度自行宣布通过。

```text
EvidencePackage
  release_id: string
  profile_version: string
  result_version: string
  claims: list<Claim>
  evidence_registry: list<EvidenceRef>
  verifier_version: string
```

```text
VerificationReport
  status: PASS | BLOCKED | PARTIAL
  claims: list<ClaimVerification>
  blocking_reasons: list<string>
  stripped_claim_ids: list<string>
  citation_coverage: number
```

```text
ClaimVerification
  claim_id
  criticality: CRITICAL | NORMAL
  status: VERIFIED | CONTRADICTED | UNVERIFIABLE
  reason: stable code
  checked_against: list<evidence_id>
  deterministic: bool
```

`citation_coverage` 按最终保留的事实性 Claim 计算。`BLOCKED` 报告没有可发布结果，
覆盖率为 `0.0`；没有保留事实性 Claim 时同样不能返回可发布报告。逐 Claim 状态、
阻断原因、检查证据和剥离 ID 均按稳定键排序。

### 6.1 文档事实

1. 检查证据存在、release 一致、适用域匹配；
2. 检查 excerpt/hash/section path；
3. 对结构化抽取的文档事实做确定性字段比对；
4. 对真正需要语义判断的文本，可调用经过版本化的 entailment verifier；
5. entailment 无法确定时只能返回 `UNVERIFIABLE`，不能升级为 `VERIFIED`。

### 6.2 结构化事实

按 `dataset + primary_key + field_path + release_id` 重新读取权威值，逐字段比较。任何类型、单位或值差异均为 `CONTRADICTED`。

### 6.3 派生事实

重算优先；如果不具备在线重算条件，则比较不可变算法产物、输入 hash、算法/配置版本、样本年份和输出。比较失败为 `CONTRADICTED`，缺任何身份字段为 `UNVERIFIABLE`。

## 7. 发布门禁

正式结果发布必须满足：

- 所有 `CRITICAL` claim 为 `VERIFIED`；
- 不存在 `CONTRADICTED` claim；
- 所有证据可解析且属于同一 `release_id`；
- 资格规则结果为 PASS 或明确允许的 PASS_PARTIAL；
- 结果引用的 Profile、release、算法配置和 verifier 版本一致；
- 免责声明和风险告警存在且未被改写。

非关键 `UNVERIFIABLE` claim 可由确定性模板剥离，只发布剩余已验证内容，并记录 `stripped_claim_ids`。关键 `UNVERIFIABLE` 不能通过剥离掩盖，必须阻断结果。

## 8. 证据包生命周期

- 证据包随 `result_version` 不可变；
- `result_version` 发布后只能被撤销/标记过时，不能重写；
- 报告、解释和导出都读取同一证据包；
- release 撤销后旧结果标记过时，不能作为新 run 的权威证据；
- 原始对象存储、数据库行和派生快照的 hash 必须长期可追溯；
- 删除用户数据时，公共文档证据可保留，用户画像和派生输入按保留策略处理。

## 9. 稳定错误

| reason | 含义 |
| --- | --- |
| `EVIDENCE_NOT_FOUND` | 引用不存在 |
| `EVIDENCE_SCOPE_MISMATCH` | 证据适用域不匹配 |
| `EVIDENCE_RELEASE_MISMATCH` | 跨 release 混合 |
| `EVIDENCE_HASH_MISMATCH` | 原文/行/快照 hash 不一致 |
| `EVIDENCE_LOCATOR_INVALID` | 原文位置无法定位 |
| `STRUCTURED_VALUE_MISMATCH` | 结构化字段不一致 |
| `DERIVED_RECOMPUTE_MISMATCH` | 派生结果重算不一致 |
| `CLAIM_UNSUPPORTED` | 事实没有足够证据 |
| `CRITICAL_CLAIM_UNVERIFIABLE` | 关键事实无法验证 |
| `VERIFIER_VERSION_MISSING` | 缺少 verifier 配置身份 |

## 10. 验收测试

| ID | 场景 | 期望 |
| --- | --- | --- |
| EV-01 | 完整、同 release 的文档事实 | `VERIFIED` |
| EV-02 | 报告引用了错误学校或年份 | `CONTRADICTED`，阻断 |
| EV-03 | 概率数字被改写 | 派生 fact `CONTRADICTED` |
| EV-04 | 样本年份被改写 | 派生 fact `CONTRADICTED` |
| EV-05 | 结构化计划数与权威行不一致 | `CONTRADICTED` |
| EV-06 | 证据 URL 存在但 excerpt 与 hash 不符 | `CONTRADICTED` |
| EV-07 | 证据包混入两个 release | `EVIDENCE_RELEASE_MISMATCH` |
| EV-08 | 缺关键规则证据 | `UNVERIFIABLE`，正式发布阻断 |
| EV-09 | 非关键段落无证据 | 可剥离并记录 claim id |
| EV-10 | 资格 UNKNOWN 被写成“通过” | 资格事实 `CONTRADICTED` |
| EV-11 | 报告省略免责声明 | 发布门禁失败 |
| EV-12 | 同输入重复验证 | 同状态、同原因、同证据顺序 |

## 11. 非目标

- 不定义自然语言报告模板；
- 不定义用户界面如何展示引用；
- 不把检索召回率当作事实正确率；
- 不在本规格实现对象存储、数据库或模型；
- 不允许 LLM 自行生成或补全证据 ID。
