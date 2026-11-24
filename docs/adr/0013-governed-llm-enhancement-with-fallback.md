# 0013 · LLM 仅增强语义与文案，失败时可审计降级

- 状态：已接受
- 决策者：Francis

第一阶段把 LLM 接入报告叙事和报告后模糊意图分类。所有模型调用继续只经过
`svc-llm-gateway`，使用 release 绑定的模型路由、Prompt、响应 Schema、数据分类、
预算和调用账本。真实 provider 采用 DeepSeek OpenAI-compatible API。

确定性核心仍是最终事实来源：分数、位次、候选身份、历史概率、资格、冲稳保、排序和
SQL 均由既有确定性代码生成。LLM 只能提出受限画像 patch 或改写不改变事实的叙事；
服务端重新校验所有 patch 和结构化输出。

降级是产品能力而不是异常旁路。报告模型不可用时，完整报告继续发布并标记
`DETERMINISTIC_FALLBACK` 和 `degradation_reason`，面向用户明确说明文案降级；意图模型
不可用时，退回确定性规则或澄清，并把降级原因写入消息 metadata。LLM 失败不得导致
推荐算法失败，也不得静默伪装成模型已参与。

云端数据分类只允许 `PUBLIC_DATA` 和最小化后的 `PRINCIPAL_DATA`。报告叙事只发送公共
范围与候选数量；意图分类只发送当前消息和必要上下文。`SENSITIVE_ELIGIBILITY_DATA`、
`SECRET` 等分类不得发送。模型 key 只存在于 llm-gateway；无 key 时保持失败关闭并验证
确定性降级。
