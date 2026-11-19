# Retrieval Runtime Contract

| 项 | 内容 |
| --- | --- |
| 版本 | v1.0 |
| 状态 | 已确认；作为 M4 检索实现基线 |
| 上游 | [PRD v1.3 FR-08/F10](../prd.md)、[ADR-0001](../adr/0001-defer-zhparser-keep-trgm.md)、[ADR-0004](../adr/0004-immutable-content-hash-release-id.md)、[ADR-0006](../adr/0006-consolidate-policy-tables-drop-charter.md) |
| 关联 | [Eligibility Rules Contract](./eligibility-rules-contract.md)、[Evidence Contract](./evidence-contract.md)、[Run Lifecycle Contract](./run-lifecycle.md) |

## 1. 目标与边界

本规格定义在线知识检索的唯一接口、文档适用域、索引版本、失败语义和检索结果结构。

本模块只检索非结构化材料，例如招生章程、政策、体检规定和院校说明。

本模块不负责：

- 分数、位次、招生计划等结构化事实；
- 判断用户是否具有报考资格；
- 生成或验证最终报告；
- 修改原始文档或绕过适用域。

数值和结构化事实只能由确定性数据接口读取，不能把 RAG 检索结果当作 SQL 查询的替代。

## 2. 核心不变量

1. **每次检索必须绑定 `release_id`。** 不允许读取未发布或跨 release 混排的索引。
2. **先过滤适用域，再计算相似度。** 省、年、科类、批次、学校、文档类型不是排序因素。
3. **年度章程与长期政策分别处理。** 文档使用生效区间和适用域，不机械要求 `effective_year == application_year`。
4. **零召回不等于“无限制”。** 必须返回 `NO_EVIDENCE`，不能生成 PASS 或自动扩大适用范围。
5. **失败不得放宽硬过滤。** 可以改写查询、增加 top_k、切换检索通道；不得改省、年、科、批次、学校或适用范围。
6. **结果是 `EvidenceRef`。** 检索器只能返回可绑定原文和 release 的证据，不返回脱离出处的自由文本。
7. **同输入同输出。** 默认排序必须包含稳定 tie-break；rerank 版本也属于索引/配置身份。
8. **文档内容是不可信数据。** 其中的指令不能改变系统规则、工具白名单、预算或图流程。
9. **Embedding 与索引必须可识别。** 模型、维度、索引版本不同的数据不得混用。

## 3. 适用域

### 3.1 查询范围

```text
ApplicabilityScope
  province: string
  application_year: int32
  category: string
  batch: string
```

V1 在线查询范围由已确认 Profile 或 POLICY_QA 的必要范围构造。V1 值域：

- `province=湖南`
- `category=物理类 | 历史类 | 不限`
- `batch=本科批 | 不限`

### 3.2 文档适用域

```text
DocumentApplicability
  regions: list<string>             # “全国”或省名列表
  categories: list<string>          # “不限”或科类列表
  batches: list<string>             # “不限”或批次列表
  valid_from: date                  # 含
  valid_to: date | null             # 含；null 表示持续有效
```

年度院校章程应把 `valid_from/valid_to` 设置为该招生年度，而不是只依赖 `effective_year`。

适用判断：

- `regions` 含 `全国`：匹配所有省；
- `regions` 不含 `全国`：必须精确包含查询省份；
- 科类或批次含 `不限`：匹配所有；
- 否则必须精确包含查询值；
- 查询日使用 `Asia/Shanghai` 的当前日期，并满足 `valid_from <= date <= valid_to`；
- 同一文档存在多个适用域记录时，任一匹配即可；
- 缺少适用域的数据不得进入正式结果。

### 3.3 文档与 chunk 元数据

```text
DocumentDescriptor
  doc_id: stable string
  title: string
  document_type: charter | policy | physical_exam | university_notice
  issuer_type: national | provincial | university
  issuer_id: string
  school_id: string | null
  applicability: list<DocumentApplicability>
  source_uri: string
  fetched_at: RFC3339 UTC
  content_sha256: string
  release_id: string

PolicyChunk
  chunk_id: stable string
  doc_id: string
  section_path: string
  text: string
  text_sha256: string
  char_start / char_end: optional locator
  embedding_model: string
  embedding_dim: int32
  index_version: string
  release_id: string
```

要求：

- `doc_id` 和 `chunk_id` 必须由稳定内容身份生成；
- `source_uri`、抓取时间和原件存储位置必须可回溯；
- `text` 的正式上限为 600 字符；超过时由 ETL 拒绝或重新切分；
- chunk 必须保留父级 `section_path`；
- `release_id` 不写入可由客户端覆盖的 payload。

## 4. Module Interface

```text
RetrievePolicy(query: PolicyRetrievalQuery) -> PolicyRetrievalResult
```

这是调用方和测试共同使用的唯一检索 seam。

```text
PolicyRetrievalQuery
  principal_id: trusted identity
  query_text: string
  scope: ApplicabilityScope
  school_ids: list<string>          # 可为空
  document_types: list<string>      # 可为空
  release_id: string
  mode: hybrid | vector | keyword
  top_k: int32                      # 默认 5
  pool_k: int32                     # 默认 20
```

`PolicyRetrievalResult`：

```text
status: FOUND | NO_EVIDENCE | UNSUPPORTED_SCOPE | RELEASE_UNAVAILABLE
release_id: string
evidence: list<EvidenceRef>
diagnostics:
  vector_candidates: int32
  keyword_candidates: int32
  rerank_enabled: bool
  degraded_reason: string | null
```

调用方不得依赖内部 SQL 行、向量距离或 rerank 模型的私有结构。诊断字段只用于测试和可观测。

## 5. 检索流水线

固定顺序：

1. 校验 `release_id` 存在、已发布且未撤销。
2. 按 `document_type / school_id / regions / categories / batches / valid interval` 做 SQL 硬过滤。
3. 在同一过滤结果内分别执行向量和关键词召回。
4. 使用 RRF 融合：`score = Σ 1/(60 + rank)`。
5. 在融合池内执行可选 rerank；rerank 只调整顺序，不能引入范围外 chunk。
6. 按最终得分和稳定 tie-break 取 `top_k`。
7. 为每个结果创建不可变 `EvidenceRef`。

默认 tie-break：

1. 最终得分降序；
2. `chunk_id` 升序。

### 5.1 向量路

- 使用 `pgvector` 余弦距离；线上查询要求 pgvector `0.8+`；
- V1 在同一个 SQL 查询中完成 release、文档类型、学校、地区、科类、批次、有效区间与证据完整性硬过滤，并以 `policy_chunk.embedding <=> query_embedding` 直接进入 HNSW 索引访问路径；不得先取跨适用域的固定 ANN 候选再由应用层补过滤；
- HNSW 只提供距离有序访问，无法直接完成 `(distance, chunk_id)` 复合排序。查询先用有界 `ann_candidates` CTE 取得不超过 `pool_k` 的距离候选，再在这个小池内按 `distance, chunk_id` 排序，避免全量物化候选破坏索引访问；
- 查询事务内使用 `hnsw.iterative_scan=strict_order`。pgvector 的图遍历过滤发生在索引访问之后，因此严格迭代扫描必须继续访问，直到取得足够候选或验证候选已耗尽；`hnsw.max_scan_tuples` 不得沿用会静默截断正确结果的默认上限；
- `hnsw.ef_search` 至少为 `max(40, pool_k)`，允许通过 release 级配置提高召回；任何参数调整都不能放宽适用域；
- `release_policy_index` 记录 release 的 embedding 模型、向量维度和 index version；查询 embedding 必须由该模型生成；
- release 映射的 `policy_chunk_set` 及其 chunk 身份必须与该 release 声明完全一致；
- 模型或维度不匹配时返回 `INDEX_MODEL_MISMATCH`，不得降级混读；
- `mode=vector` 时模型不可用或查询执行失败返回 `VECTOR_UNAVAILABLE`，不能返回 `NO_EVIDENCE`；
- `mode=vector` 在身份校验与查询均成功后零候选，返回 `NO_EVIDENCE`；
- `mode=hybrid` 的向量路失败可使用同范围关键词路并标记 `degraded_reason`。

### 5.2 关键词路

- V1 使用 `pg_trgm` 的 `ILIKE + similarity`；
- 查询切分最多保留 4 个不重复 term，每个至少 2 个字符；
- SQL 参数化并转义 `% / _ / \`；
- 关键词路没有结果不构成“无限制”；
- 只有 golden set 证明关键词路是瓶颈时，才评估 zhparser。

### 5.3 RRF

- 常量 `k=60`，写入配置版本；
- 每一路各自先做稳定排序，名次从 1 开始；
- 融合分为 `Σ 1/(60 + rank)`，同一候选两路命中时累加，缺一路时不获得该路分数；
- 融合后必须保留两路原始 rank，供证据和调试；最终按融合分降序、`chunk_id` 升序稳定排序。

### 5.4 Rerank

- rerank 模型版本写入 release；
- 仅在融合后的候选池内排序；
- rerank 超时或失败时使用 RRF 顺序并标记降级；
- rerank 不得删除适用域校验结果。

## 6. 结构化事实与 RAG 分流

以下问题必须走确定性接口，不走 RAG：

- 分数、位次、一分一段；
- 招生人数、计划数；
- 最低录取分和录取位次；
- 当年招生专业组/专业是否存在；
- 学费的结构化数值。

允许 RAG 回答：

- 转专业政策；
- 大类分流说明；
- 体检限制原文；
- 单科或语种要求原文；
- 无法可靠结构化但带原文出处的一般政策说明。

当问题同时包含结构化数值和文本政策时，调用方必须分别查询，再将结构化事实与文档证据组合；不能把 SQL 行塞入 RRF，也不能让 RAG 猜数值。

## 7. 失败与降级

| 场景 | 行为 |
| --- | --- |
| release 不存在/撤销 | `RELEASE_UNAVAILABLE`，不查询 |
| 省年科批不在支持范围 | `UNSUPPORTED_SCOPE`，不放宽 |
| 硬过滤后为空 | `NO_EVIDENCE`，不扩大范围 |
| release 声明的模型/维度或 chunk/chunk set 索引身份不一致 | `INDEX_MODEL_MISMATCH`，零混读 |
| vector-only 的模型不可用、查询执行失败 | `VECTOR_UNAVAILABLE`，不伪装为 `NO_EVIDENCE` |
| hybrid 的向量模型不可用 | 同范围关键词检索，`degraded_reason=VECTOR_UNAVAILABLE` |
| 关键词索引不可用 | 同范围向量检索，`degraded_reason=KEYWORD_UNAVAILABLE` |
| 两路均不可用或数据库不可达 | `RETRIEVAL_UNAVAILABLE`，不伪装为 `NO_EVIDENCE` |
| 命中候选无法形成完整证据 | `EVIDENCE_PROJECTION_FAILED`，不跳过候选后伪装为 `NO_EVIDENCE` |
| rerank 不可用 | 使用 RRF 顺序并记录降级 |
| 文档缺少适用域 | 正式结果不可使用该文档 |

## 8. 离线索引契约

发布一个文档索引前必须完成：

1. 原件落对象存储并记录 hash；
2. 元数据、适用域、来源和抓取时间完整；
3. 切分、Embedding、关键词索引使用同一批输入；
4. chunk 长度、来源、section path 校验通过；
5. 同 release 引用一致的 embedding/model/index 版本；
6. 索引写入暂存区，发布后正式读取端只读；
7. 发布清单包含文档 ID 集合与内容 hash；
8. 撤销时新 run 不得再绑定该 release。

索引更新不得原地覆盖正在被 run 使用的版本。

## 9. 验收测试

| ID | 场景 | 期望 |
| --- | --- | --- |
| RT-01 | 湖南物理类 2025 院校章程 | 只返回适用域匹配的 chunk |
| RT-02 | 全国长期有效政策 | 可按生效区间命中，不因年份不等被删除 |
| RT-03 | 无 result 的查询 | `NO_EVIDENCE`，不生成“没有限制” |
| RT-04 | 查询范围缺年份 | `UNSUPPORTED_SCOPE`，不默认当年 |
| RT-05 | release 已撤销 | `RELEASE_UNAVAILABLE`，零查询 |
| RT-06 | 向量结果与关键词结果 | RRF 分数、双路 rank 和 tie-break 可复现 |
| RT-07 | 查询含 `%/_/\\` | SQL 参数化转义，不发生注入或通配符泄漏 |
| RT-08 | rerank 超时 | RRF 顺序保留并标记降级，不越界召回 |
| RT-09 | 文档正文包含“忽略系统指令” | 只能作为证据文本，不改变图或工具 |
| RT-10 | 结构化数值 mix 政策问题 | 两个独立查询，SQL 结果不与 RAG chunk 混成同名证据 |

## 10. 非目标

- 不在本规格定义资格规则判断；
- 不在本规格定义报告文本生成；
- 不在本规格实现 SQL schema 或 rerank 模型；
- 不把 retrieval recall 当作事实正确率。
