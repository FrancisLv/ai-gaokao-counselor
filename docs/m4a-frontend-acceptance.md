# M4a 前端验收

本验收只检查 **POLICY_QA 文档证据检索**：提交适用范围和问题，查看
`FOUND` / `NO_EVIDENCE` 状态以及原始文档证据。

不在本切片内：

- 生成最终政策回答；
- 判断 PASS / FAIL / UNKNOWN 或报考资格；
- 生成最终报告；
- 调用 LLM Gateway。

## 1. 准备隔离数据库

使用名称含 `test` 或 `acceptance` 的数据库。以下命令会重建该数据库的
`public` schema，不能对业务库执行。

```bash
export M4A_ACCEPTANCE_DSN=postgresql://francis@localhost:5432/ai_gaokao_ticket2_test

make m4a-acceptance-seed \
  DSN="$M4A_ACCEPTANCE_DSN" \
  RESET_SCHEMA=1 \
  PY=/Users/francis/miniconda3/envs/ai-gaokao-counselor/bin/python3
```

seed 会创建：

- `PUBLISHED` release：`rel_m4a_frontend_acceptance`
- 湖南 2025 物理类本科批院校章程
- 全国长期学生资助政策
- 跨省干扰章程
- 已过期章程

可用查询：

```text
转专业政策是什么？
全国学生资助政策
火星基地招生政策
```

前两条应返回 `FOUND`；最后一条应返回 `NO_EVIDENCE`。

## 2. 启动服务

### Orchestrator

```bash
ORCHESTRATOR_DSN="$M4A_ACCEPTANCE_DSN" \
ALGO_ADDR=localhost:9002 \
PYTHONPATH=services/orchestrator:services/orchestrator/pb \
/Users/francis/miniconda3/envs/ai-gaokao-counselor/bin/python3 \
-m app.main
```

当前验收不要求启动真实 algo 服务。`POLICY_QA` 只调用本地 `RetrievePolicy`。

### Gateway

新终端：

```bash
GATEWAY_ADDR=127.0.0.1:18000 \
ORCHESTRATOR_ENDPOINT=localhost:9100 \
go run ./cmd/gateway
```

### Frontend

新终端：

```bash
cd frontend
VITE_PRINCIPAL_ID=principal-m4a-acceptance \
VITE_GATEWAY_TARGET=http://127.0.0.1:18000 \
npm run dev -- --host 127.0.0.1
```

打开：

```text
http://127.0.0.1:5173
```

浏览器请求仍需要可信 `x-principal-id`。当前验收前端通过
`VITE_PRINCIPAL_ID` 注入；该变量只能用于本地验收，生产身份仍由 gateway 认证层提供。

## 3. 手工验收步骤

### A. FOUND：院校章程

1. 选择“政策证据检索”。
2. 问题输入“转专业政策是什么？”。
3. 省份湖南、年份 2025、科类物理类、批次本科批。
4. 学校 ID 输入 `1001`，文档类型输入 `charter`。
5. 点击“检索政策证据”。

预期：

- Run 最终为 `SUCCEEDED`。
- 状态为 `FOUND`。
- 出现一张证据卡。
- 标题为“湖南测试大学2025年招生章程”。
- 章节为“第二章 > 政策说明”。
- 原文为纯文本，不执行 HTML。
- 页面显示 release `rel_m4a_frontend_acceptance`。
- excerpt hash 显示前 14 位，并可展开完整 hash。
- 字符定位为 `0–...`。
- “查看原始来源”在新窗口打开。
- 未配置 embedding 模型时显示“向量路不可用，已使用关键词路”，这是预期降级。

### B. FOUND：全国长期政策

将问题改为“全国学生资助政策”，学校 ID 保持 `1001`，文档类型改为 `policy`。

预期：

- 状态为 `FOUND`。
- 返回全国政策，不因学校筛选被错误排除。

### C. NO_EVIDENCE

将问题改为“火星基地招生政策”。

预期：

- Run 最终为 `SUCCEEDED`。
- 状态为 `NO_EVIDENCE`。
- 页面显示“未检索到可靠证据”。
- 页面显示“这不代表没有相关政策限制”。
- 不出现证据卡。
- 不出现“无限制”“可以报考”或“保证录取”。

### D. 跨范围证据不泄漏

问题改回“转专业”，学校 ID 保持 `1001`。

预期：

- 不出现湖北章程。
- 不出现 2024 过期章程。

### E. 不支持范围

将模式改为 `vector`，且不配置 `POLICY_EMBEDDING_MODEL`。

预期：

- Run 以 `VECTOR_UNAVAILABLE` 失败。
- 页面显示检索服务不可用，不伪装为 `NO_EVIDENCE`。

将年份改为 seed 未发布的年份时，应在 Submit 阶段得到 `RELEASE_UNAVAILABLE`。

### F. SSE 刷新与恢复

1. 完成一次 FOUND 查询。
2. 刷新页面。

预期：

- URL 中保留 `?run=<run_id>`。
- `GetRun` 恢复同一 `POLICY_EVIDENCE` 结果。
- 证据 ID、顺序、hash 和 locator 不变。
- 重新订阅 SSE 不触发第二次检索。

## 4. 自动验收命令

后端：

```bash
make api PY=/Users/francis/miniconda3/envs/ai-gaokao-counselor/bin/python3
make py-check py-test PY=/Users/francis/miniconda3/envs/ai-gaokao-counselor/bin/python3
make build
make check test
```

真实库：

```bash
ORCHESTRATOR_TEST_DSN="$M4A_ACCEPTANCE_DSN" \
make py-test PY=/Users/francis/miniconda3/envs/ai-gaokao-counselor/bin/python3
```

前端：

```bash
cd frontend
npm test
npm run build
npm run e2e
```

## 5. 通过标准

- POLICY_QA 使用 typed request，不依赖 opaque `user_text`。
- principal 和 release 不能由前端正文覆盖。
- `FOUND` 的 release、hash、locator、source 可核对。
- `NO_EVIDENCE` 与“无限制”明确区分。
- 技术失败与 `NO_EVIDENCE` 明确区分。
- GENERATE_PLAN 既有运行验收不回归。
