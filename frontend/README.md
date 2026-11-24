# 知愿对话式志愿助手

Vue 3 + Vite 前端，采用桌面端三栏对话布局：

- 左侧：服务端会话历史与新对话（按登录账号隔离）
- 中间：DeepSeek 式对话、画像确认和结果卡
- 右侧：当前画像与处理进度

最终推荐使用两个独立 HTML 页面展示：

- `/reports/<run_id>`：登录态不可变报告汇总入口（内部跳转到静态渲染页）
- `school.html?run=<run_id>&section=<section_key>`：推荐学校详情页
- 报告汇总页可导出 A4 纵向 PDF 志愿报告；导出源固定为已保存草稿，未保存修改会先要求保存
- 报告汇总页和学校详情页展示 2022–2024 专业组最低录取分与最低位次；缺失年份显示“暂无数据”
- 跨年专业组通过 release-bound lineage 匹配，表格标注“专业组代码一致”或“专业名称重合 N%”

页面只通过 svc-gateway 访问 run 生命周期和会话历史接口，不直接连接
orchestrator、算法或 LLM Gateway。

## 环境变量

浏览器身份完全来自同源 HttpOnly 登录 Cookie。客户端不再接受或发送
`VITE_PRINCIPAL_ID` / `x-principal-id`。首次访问会跳转到 `login.html`，注册需要
一次性邀请码，并使用离线恢复码重设密码。

登录后可使用：

- `wishlists.html`：我的志愿表，含当前可用、已被替代、已失效状态、版本历史、
  恢复和删除；
- `account.html`：昵称、密码、恢复码、敏感资格同意、退出登录和账户删除。

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
7. 报告生成后输入框保持可用：「为什么排在前面」绑定当前报告生成解释；
   「不要武汉」「只看计算机」「更稳一点」等筛选创建新版不可变方案。
8. 历史对话从 `/v1/sessions` 和 `/v1/sessions/{id}` 恢复，包含消息、画像、报告及
   各次修订版本。
9. 对话结果卡固定展示冲/稳/保三档概览，每档显示数量与最多 2 个概率升序候选；
   「查看全部」跳到完整报告对应档位，学校行打开学校详情。
10. 报告页保存草稿后可点击“导出 PDF”；浏览器打印面板中选择“另存为 PDF”。
    首版正式验收范围为桌面 Chrome/Edge，失效结果和未保存草稿会被打印拦截。

当前首次画像采集仍由前端受控状态机驱动；报告后的意图路由与受控解释由 orchestrator
执行。筛选条件由确定性算法重新计算，模型不直接改写候选身份、概率或顺序。

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

连接真实 gateway 的浏览器联调使用显式开关；默认 E2E 只运行 mock 回归：

```bash
PLAYWRIGHT_BASE_URL=http://127.0.0.1:15173 \
  E2E_REAL_GATEWAY=1 \
  npm run e2e -- e2e/real-gateway.spec.ts
```

运行前需启动 gateway、orchestrator、algo 和带可用 release/规则/招生数据的
PostgreSQL，并将 Vite 的 `VITE_GATEWAY_TARGET` 指向该 gateway。
