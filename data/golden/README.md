# data/golden —— 评测标注集

完整契约见 [Evaluation and SLO Contract](../../docs/specs/evaluation-slo-contract.md)。

评测集按不可变版本目录存放。当前检索集版本：

```text
retrieval-hunan-2025-v0.2.0-draft/
  qa.jsonl
  manifest.json
```

检索专用版本目录必须包含：

- `qa.jsonl`：检索用例，至少包含 `case_id / query / scope / expected_chunk_ids / expected_doc_ids / must_exclude_sources / tags / criticality / interference`
- `manifest.json`：记录 `dataset_version`、dataset/Case 文件 SHA256、case count、release、标注人、owner 状态和创建时间

只有包含端到端评测的版本才额外要求 `cases.jsonl`，至少包含
`case_id / input_profile / scripted_model_responses / expected / tags / criticality`。

当前检索草稿的 `owner_status` 为 `PENDING_SIGNOFF`。在 Francis 完成定标前，只能用于
harness 冒烟、diff 和待审阅基线，不能宣称正式检索质量门禁。全国/省级长期政策用例
还明确标记为 `PENDING_RELEASE_BINDING`，须绑定正式 release 后才能运行真实 baseline。

首批规模为检索集 30 条、端到端集 10 条；本版本先交付检索集。后续检索集扩展目标
为 100–200 条。修改任何用例都必须复制到新的版本目录并生成新 hash 与新
`dataset_version`，不能覆盖旧版本；loader 会拒绝 manifest hash 与 `qa.jsonl`
不一致的数据。

## 运行

常规 CI / 本地 harness 冒烟使用确定性边界替身，不连数据库、不下载模型：

```bash
PYTHONPATH=services/orchestrator:services/orchestrator/pb \
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
/Users/francis/miniconda3/envs/ai-gaokao-counselor/bin/python3 \
scripts/run_retrieval_eval.py --fixture --mode hybrid --as-of 2025-11-19
```

真实检索必须显式绑定已发布 release、隔离测试库和匹配的 embedding/index 身份：

```bash
scripts/run_retrieval_eval.py \
  --dsn "$ORCHESTRATOR_TEST_DSN" \
  --release-id "$RETRIEVAL_RELEASE_ID" \
  --mode hybrid \
  --embedding-model BAAI/bge-large-zh-v1.5 \
  --index-version "$POLICY_INDEX_VERSION"
```

真实基线与 fixture 基线必须分开保存。fixture 输出中的
`retrieval.authoritative_quality=false` 表示它只验证执行器、指标、scope leak 和
报告格式，不代表真实数据上的 recall 或 MRR。

当前冻结 fixture baseline：

```text
baselines/retrieval-hunan-2025-v0.2.0-draft.rerank-off.fixture.json
```

baseline 记录 dataset SHA、code identity、model route、price table、verifier、
algorithm config、hardware/environment、baseline identity，以及 retrieval 的
embedding/rerank/RRF/index 配置。

用途（见架构文档第 11 章）：
- 检索质量：recall@k、MRR、citation accuracy
- 生成质量：faithfulness、幻觉率、引用覆盖率
- 每次 PR 使用冻结替身跑一遍；安全指标为绝对门禁，质量指标不得显著退化。
