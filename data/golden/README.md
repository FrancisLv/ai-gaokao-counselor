# data/golden —— 评测标注集（须入库，用于 CI 回归）

放两类文件：

- `qa.jsonl`：问答对，字段 `{ "query": ..., "expected_sources": [...], "must_include": [...] }`
- `cases.jsonl`：端到端用例，字段 `{ "profile": {...}, "assert": {...} }`

用途（见架构文档第 11 章）：
- 检索质量：recall@k、MRR、citation accuracy
- 生成质量：faithfulness、幻觉率、引用覆盖率
- 每次 PR 跑一遍，指标不许下降，否则阻断合并。
