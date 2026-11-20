# 知愿对话式志愿助手

Vue 3 + Vite 前端，采用桌面端三栏对话布局：

- 左侧：匿名对话历史与新对话
- 中间：DeepSeek 式对话、画像确认和结果卡
- 右侧：当前画像与处理进度

最终推荐使用两个独立 HTML 页面展示：

- `report.html?run=<run_id>`：不可变声明报告汇总页
- `school.html?run=<run_id>&section=<section_key>`：推荐学校详情页

页面只通过 svc-gateway 访问 `SubmitRun / ResumeRun / GetRun`，不直接连接
orchestrator、算法或 LLM Gateway。

## 环境变量

在 `frontend/.env.local` 创建：

```dotenv
VITE_PRINCIPAL_ID=frontend-verifier
```

该变量仅用于本地演示，通过 `x-principal-id` 请求头发送，不写入 URL 或请求正文。

## 启动

后端需要可用的 svc-gateway；Vite 默认将 `/v1` 代理到 `http://localhost:8000`。

```bash
cd frontend
npm install
npm run dev
```

打开 <http://localhost:5173>。

## 对话流程

1. 从欢迎页选择示例或输入分数、选科。
2. 系统继续追问缺失的科类、分数、选科、城市、专业兴趣和风险偏好。
3. 前端把已收集的强结构化画像提交给现有 `CreatePlan` / `SubmitRun`。
4. 后端进入 `WAITING_HITL` 后，页面展示服务端画像确认卡。
5. 用户确认后调用 `ResumeRun`，页面轮询 `GetRun` 直到正式结果或稳定失败。
6. 结果卡打开独立静态报告；报告中的学校继续跳转学校详情页。

当前对话追问由前端受控状态机驱动，不会伪造 LLM 输出；真正的自然语言画像解析仍
需要后续接入 orchestrator 的 intake / llm-gateway 路径。

## 原型

三套设计原型保留用于评审：

```bash
npm run prototype
```

## 测试

```bash
npm test
npm run build
npm run e2e
```

Vitest 在 HTTP 边界 mock，Playwright 覆盖完整对话、画像确认、静态报告和学校详情跳转。
