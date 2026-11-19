# M2/M3 Run 验收控制台

Vue 3 + Vite 前端，只通过 svc-gateway 验证 M2/M3：

- 结构化画像提交
- run 状态恢复
- SSE 事件时间线
- 画像 interrupt 确认
- ALGORITHM_PREVIEW
- 取消与稳定错误状态

不直接连接 svc-orchestrator，不实现 M4 的资格规则、在线 RAG、LLM
Gateway、Strategy/Critic 或正式报告。

## 环境变量

在 `frontend/.env.local` 创建：

```dotenv
VITE_PRINCIPAL_ID=frontend-verifier
```

该变量只用于本地验收。它会通过 `x-principal-id` 请求头发送，不写入 URL、
sessionStorage 或请求正文。公网部署必须替换为真实认证。

## 启动

后端需要：

- svc-gateway：`localhost:8000`
- svc-orchestrator：`localhost:9100`
- svc-algo：`localhost:9002`
- PostgreSQL：已应用 `00` 到 `14` 迁移

```bash
cd frontend
npm install
npm run dev
```

打开 <http://localhost:5173>。Vite 会把 `/v1` 代理到
`http://localhost:8000`。

## 手工验收

1. 选择湖南、2025、物理类或历史类、本科批。
2. 填写分数和 3+1+2 选科。
3. 提交后记录 `runId`。
4. 等待 `WAITING_HITL`，检查画像卡和 `DEFAULT` 批次。
5. 确认画像。
6. 等待 `SUCCEEDED`，检查非空 ALGORITHM_PREVIEW。
7. 检查全部未核验条件、风险提示和免责声明。
8. 刷新页面，确认通过 URL 中的 `run` 参数恢复同一 run。
9. 对终态再次取消，应显示 `RUN_ALREADY_TERMINAL`。

## 测试

```bash
npm test
npm run build
npx playwright install chromium
npm run e2e
```

Vitest 在 HTTP/SSE 边界 mock，不 mock composable 内部实现。Playwright
访问真实 Vue 页面，并在浏览器网络边界提供验收响应。
