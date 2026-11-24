# Macro Region Resolution Contract

| 项 | 内容 |
| --- | --- |
| 版本 | v1.0 |
| 状态 | 已确认 |
| 上游 | [PRD v1.3](../prd.md)、[ADR-0014](../adr/0014-province-city-hard-filters.md)、[ADR-0017](../adr/0017-governed-domain-queries-not-text-to-sql.md) |
| 关联 | [Profile Contract](./profile-contract.md)、[Run Lifecycle Contract](./run-lifecycle.md) |

## 1. 目标

本规格定义“北方”“南方”“华南”“东北”等大区表达如何澄清为可执行的受控省份集合。
大区表达本身不是画像字段，也不是可直接执行的筛选条件。

## 2. 不变量

1. 未经用户确认的大区表达 MUST NOT 写入
   `required_provinces` 或 `excluded_provinces`。
2. 画像 MUST 只保存受控省份名称，不保存大区名称或快捷组 ID。
3. 快捷组 MUST 来自 release 绑定的不可变目录快照，并展示其包含的具体省份。
4. LLM MUST NOT 自行把大区表达展开为省份集合，也 MUST NOT 生成 SQL。
5. 未解决的大区硬约束 MUST NOT 进入候选生成或创建新的 `REVISE_PLAN`。
6. 首次画像和报告后修订 MUST 使用同一套大区解析语义。

发布新 release 时 MUST 调用 `ensure_region_preset_catalog(release_id)`；目录缺失时
在线解析 fail closed，不回退到代码常量或当前最新目录。

## 3. 受控快捷组

| ID | 名称 | 省份 |
| --- | --- | --- |
| `northeast` | 东北 | 辽宁、吉林、黑龙江 |
| `north_china` | 华北 | 北京、天津、河北、山西、内蒙古 |
| `east_china` | 华东 | 上海、江苏、浙江、安徽、福建、江西、山东 |
| `central_china` | 华中 | 河南、湖北、湖南 |
| `south_china` | 华南 | 广东、广西、海南 |
| `southwest_china` | 西南 | 重庆、四川、贵州、云南、西藏 |
| `northwest_china` | 西北 | 陕西、甘肃、青海、宁夏、新疆 |

用户同时选择多个快捷组时，省份集合按并集合并并去重；最终仍须以具体省份集合确认。

## 4. 回答契约

大区澄清使用结构化 `SELECT_PROVINCES` 回答：

```text
SELECT_PROVINCES
  provinces: list<受控省份名称>
  selected_preset_ids: list<release 快照内快捷组 ID>
  cancel_region_restriction: bool
```

约束：

- 界面同时提供 31 个受控省份多选和七个快捷组；
- 点击快捷组只填充或取消对应省份，快捷组 ID 不进入画像；
- 多个快捷组按省份并集合并并去重；
- 自然语言只可解析为一个或多个精确省份名称，或当前界面已展示且来自目录的快捷组名称；
- 自然语言无法无歧义解析时保持未解决，不调用 LLM 猜测；
- 最终展示的省份集合须经画像确认后生效。

## 5. 未解决行为

用户首次说出大区表达时：

- 系统 MUST 返回一次针对性澄清，不得沿用通用“解释还是重做”问句；
- 首次生成时，当前 run 保持人工等待，不进入候选生成；
- 报告后修订时，不创建新的 `REVISE_PLAN`，原结果保持不变；
- 澄清后仍未得到省份集合时，不调用 LLM 猜测，也不使用默认大区映射；
- 用户明确表示取消该限制后，方可继续生成或修订不含该硬约束的方案。

稳定原因码为 `MACRO_REGION_SCOPE_UNRESOLVED`。

## 6. 审计来源

对话或 interrupt 元数据 MUST 保留以下非权威来源：

- `selected_preset_ids`
- `region_catalog_version`
- `resolved_provinces`

这些字段只用于复盘澄清过程，不属于画像，也不得被后续 run 当作筛选条件读取。
权威筛选条件始终是确认画像中的 `required_provinces` / `excluded_provinces`。
