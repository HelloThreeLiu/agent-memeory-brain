# 本批待办（BACKLOG）

> 规则见 `WORKFLOW.md` ①：M/L 级需求必须登记在这里，不允许只存在于对话或记忆里。
> 状态：`todo` / `doing` / `blocked` / `done`。

## 当前批次：P0 骨架（目标：能存、能召回、能断言）

| ID | 类型 | 级别 | 事项 | 验收标准 | 状态 |
|---|---|---|---|---|---|
| P0-1 | feat(schema) | L | `memories` / `memory_links` / `episodes` / `events` 建表 + 迁移框架 | 空库初始化成功；`schema_version` 可读；重复初始化幂等 | todo |
| P0-2 | test(store) | L | 把 INV1/INV3/INV5/INV6/INV12 落成断言测试 | 每条 INV 至少 1 个会红的用例（先写反例证明断言有效） | todo |
| P0-3 | feat(store) | L | `write_service` 单一写入口（事务边界、`seq` 分配、`content_hash` 幂等） | 并发两个进程各写 100 条：无重复、`seq` 连续无洞 | todo |
| P0-4 | feat(recall) | M | FTS5 + 中文 2-gram 召回，两因子打分（相关性 + 重要度） | 200 条种子数据、20 个查询人工命中 ≥70%；单次召回 <100ms | todo |
| P0-5 | feat(cli) | M | `remember` / `recall` 两个命令 | 断网、无 API key 时可用 | todo |
| P0-6 | docs(design) | S | 冻结 schema 后回填设计文档 §8.1 的实际 DDL | 文档 DDL 与代码一致（脚本比对） | todo |
| CI-1 | chore(ci) | L | 升级 `actions/checkout` / `actions/setup-python` 到 v7 | CI 无 Node20 弃用告警；门禁全绿 | done |

## 待定（等数据，不排期）

| ID | 事项 | 等什么 |
|---|---|---|
| U4 | `private/shared` 分区粒度按 session 还是按角色 | 需要确认 reviewer 是否要看到 writer 的中间推理 |
| U6 | 是否值得做 `procedure` 通道（双门成本高） | 是否真的存在"被反复确认的工作流" |
| U7 | 注入信任度降到多少该告警 | P1.5 攒够 300 条注入记录后的负反馈率基线 |

## 已完成

| ID | 事项 | 提交 |
|---|---|---|
| D-1 | 调研 + 设计 v0.5（三轮评审 25 条全部处理） | `aa314cc` |
| D-2 | 研发流程规范 + CI 门禁落地 | `58b0d21` / `df29a9b` |
| D-3 | CI actions 升级至 v7，Node20 告警消除（PR #1） | `481e435` |
