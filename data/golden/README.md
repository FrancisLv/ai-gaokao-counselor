# data/golden —— 评测标注集

完整契约见 [Evaluation and SLO Contract](../../docs/specs/evaluation-slo-contract.md)。

放两类文件：

- `qa.jsonl`：检索用例，至少包含 `case_id / query / scope / expected_chunk_ids / expected_doc_ids / must_exclude_sources / tags / criticality`
- `cases.jsonl`：端到端用例，至少包含 `case_id / input_profile / scripted_model_responses / expected / tags / criticality`
- `manifest.json`：记录 `dataset_version`、两个 JSONL 的 SHA256、标注人和创建时间

首批规模为检索集 30 条、端到端集 10 条；后续检索集扩展目标为 100–200 条。修改任何用例都必须创建新的 `dataset_version`，不能覆盖旧版本。

用途（见架构文档第 11 章）：
- 检索质量：recall@k、MRR、citation accuracy
- 生成质量：faithfulness、幻觉率、引用覆盖率
- 每次 PR 使用冻结替身跑一遍；安全指标为绝对门禁，质量指标不得显著退化。
