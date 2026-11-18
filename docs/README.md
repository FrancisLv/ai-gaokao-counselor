# docs

项目文档导航。PRD 描述目标态（系统做成什么样），架构总览是其图示派生，开发指南与差距分析描述当前实际能力。

建议阅读顺序：

1. [PRD](prd.md)：**目标态唯一权威**（v1.3，含端到端 15 步、发布层级、20 验收场景）。
2. [可执行规格](specs/README.md)：八份 runtime 契约与 proto/DB 变更计划；M1 进入实施阶段。
3. [架构总览](agent-architecture.md)：待按 PRD v1.3 重新派生的图示总览，非权威；[HTML 浏览版](agent-architecture.html)。
4. [差距分析](gap-analysis.md)：PRD 目标态对照代码现状的活文档（还差多少）。
5. [开发指南](development-guide.md)：系统边界、模块完成度、数据流、配置、运行与测试。
6. [算法通俗说明](algorithms.md)：设计动机与局限，不作为参数默认值来源。
7. [task2 实施文档](task2-implementation-plan.md)：algo 数据结构、算法细节、接口、错误码及验收状态。
8. [开发问题与验收记录](dev-log.md)：历史问题、修复证据、覆盖率和性能实测。
9. [设计问题决策索引](task2-review-todo.md)：Q 系列讨论结论与决策背景。

[根 README](../README.md) 提供最短构建入口；[招生章程目录](charters/) 保存本地语料。本轮仅只读解析验证，未重新生成数据库中的向量。
