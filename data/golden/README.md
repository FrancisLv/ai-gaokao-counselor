# data/golden —— 评测标注集

完整契约见 [Evaluation and SLO Contract](../../docs/specs/evaluation-slo-contract.md)。

评测集按不可变版本目录存放。当前检索集版本：

```text
retrieval-hunan-2025-v0.4.0-draft/
  qa.jsonl
  manifest.json
```

当前联合检索 + 端到端草稿：

```text
evaluation-hunan-2025-v0.1.0-draft/
  qa.jsonl
  cases.jsonl
  manifest.json
```

联合版本的 `dataset_sha256` 由 `qa.jsonl` 与 `cases.jsonl` 的 SHA256 按固定顺序
规范化计算；任一文件被修改都必须生成新版本和新 manifest，不能覆盖当前目录。

检索专用版本目录必须包含：

- `qa.jsonl`：检索用例，至少包含 `case_id / query / scope / expected_chunk_ids / expected_doc_ids / must_exclude_sources / tags / criticality / interference`；同一文档内的低相关 hard negative 可额外使用 `must_exclude_chunk_ids`
- `manifest.json`：记录 `dataset_version`、dataset/Case 文件 SHA256、case count、release、标注人、owner 状态和创建时间

只有包含端到端评测的版本才额外要求 `cases.jsonl`，至少包含
`case_id / input_profile / scripted_model_responses / expected / tags / criticality`。

当前检索草稿的 `owner_status` 为 `PENDING_SIGNOFF`。在 Francis 完成定标前，只能用于
harness 冒烟、diff 和待审阅基线，不能宣称正式检索质量门禁。全国/省级长期政策用例
还明确标记为 `PENDING_RELEASE_BINDING`，须绑定正式 release 后才能运行真实 baseline。

v0.3 在 v0.2 的 30 条用例上新增一条真实同文档 relevance precision 用例。评测除
recall/MRR 外还报告 `evidence_precision@5` 与 `irrelevant_evidence_rate`；
`must_exclude_chunk_ids` 命中会逐例失败，不能被 recall 平均值掩盖。

v0.4 绑定真实 2025 release，并把 6 条暂不可评测的 case 用 `evaluation_status`
显式分类：2 条待政策 release 绑定、2 条待重新切分/索引、2 条待 semantic relevance
判断。非 ACTIVE case 不调用 `RetrievePolicy`，不进入 recall/MRR/precision 分母，
但会在 baseline 的 `skipped_case_count` 和 `skipped_case_reasons` 中逐类报告。

联合草稿 `evaluation-hunan-2025-v0.1.0-draft` 复用 v0.4 的 31 条检索 case，
并新增首批 10 条端到端报告 case。端到端 case 覆盖正常冲稳保、缺全局字段、
选科拒绝、条件 UNKNOWN、无可靠数据、事实篡改、画像改分、预算耗尽、worker
恢复和取消/发布竞争。

联合 loader 同时校验 `retrieval_cases_sha256`、`e2e_cases_sha256`、
`dataset_sha256`、两类 case count、`immutable=true` 和必备场景标签。当前
`owner_status` 仍为 `PENDING_SIGNOFF`，只能生成诊断与待审阅 EvalRun；
`release_gate_status` 保持 `BLOCKED_PENDING_SIGNOFF`。

端到端 runner 从 `RunLifecycle` 的 `SubmitRun → ResumeRun → GetRun` 获取最终
报告和保留后的 Claims，不直接调用报告节点、`VerifyPackage` 或
`PublishFormalResult`。报告指标只统计最终 Claims；stripped Claim 不进入分母。

首批规模为检索集 30 条、端到端集 10 条；后续检索集扩展目标为 100–200 条。
修改任何用例都必须复制到新的版本目录并生成新 hash 与新 `dataset_version`，
不能覆盖旧版本。

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
baselines/retrieval-hunan-2025-v0.4.0-draft.rerank-off.fixture.json
```

baseline 记录 dataset SHA、code identity、model route、price table、verifier、
algorithm config、hardware/environment、baseline identity，以及 retrieval 的
embedding/rerank/RRF/index 配置。

文件名不带提交短 SHA 的旧 fixture baseline 只作历史留存，不能作为当前代码身份
的复现证据。

用途（见架构文档第 11 章）：
- 检索质量：recall@k、MRR、citation accuracy
- 生成质量：faithfulness、幻觉率、引用覆盖率
- 每次 PR 使用冻结替身跑一遍；安全指标为绝对门禁，质量指标不得显著退化。
