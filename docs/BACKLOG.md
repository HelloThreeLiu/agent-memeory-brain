# 本批待办（BACKLOG）

> 规则见 `WORKFLOW.md` ①：M/L 级需求必须登记在这里，不允许只存在于对话或记忆里。
> 状态：`todo` / `doing` / `blocked` / `done`。

## 当前批次：P0 骨架（目标：能存、能召回、能断言）—— 已收口

| ID | 类型 | 级别 | 事项 | 验收标准 | 状态 |
|---|---|---|---|---|---|
| P0-1 | feat(schema) | L | `memories` / `memory_links` / `episodes` / `events` 建表 + 迁移框架 | 空库初始化成功；`schema_version` 可读；重复初始化幂等 | done |
| P0-2 | test(store) | L | 把 INV1/INV3/INV5/INV6/INV12 落成断言测试 | 每条 INV 至少 1 个会红的用例（先写反例证明断言有效） | done |
| P0-3 | feat(store) | L | `write_service` 单一写入口（事务边界、`seq` 分配、`content_hash` 幂等） | 并发两个进程各写 100 条：无重复、`seq` 连续无洞 | done |
| P0-4 | feat(recall) | M | FTS5 + 中文 2-gram 召回，两因子打分（相关性 + 重要度） | 200 条种子数据、20 个查询人工命中 ≥70%；单次召回 <100ms | done |
| P0-5 | feat(cli) | M | `remember` / `recall` 两个命令 | 断网、无 API key 时可用 | done |
| P0-6 | docs(design) | S | 冻结 schema 后回填设计文档 §8.1 的实际 DDL | 文档 DDL 与代码一致（脚本比对） | done |
| CI-1 | chore(ci) | L | 升级 `actions/checkout` / `actions/setup-python` 到 v7 | CI 无 Node20 弃用告警；门禁全绿 | done |
| GOV-1 | chore(governance) | L | main 分支保护（禁直推、必需 gate、禁强推/删除、线性历史）+ 小改动快车道 | 直推 main 被服务端拒绝；文档类改动可一条命令合入 | done |

**实测结果（2026-11-10，本机 Python 3.13.12 / SQLite 3.53.1）**

- 门禁：`ruff` 0 问题 / `mypy --strict` 0 问题 / `pytest` 87 passed / `pip-audit` 0 漏洞 / `python -m build` 出 wheel。
- 检索基线：200 条种子（40 主题 × 5 措辞）、20 查询，hit@5 **19/20 = 95%**（门槛 70%）；p50 0.37ms、p95 0.49ms（门槛 100ms）。
- 并发：两进程各写 100 条 → 200 个唯一 id，`seq` = 1..200 无洞。
- 唯一未命中的查询是「用什么数据库存储数据」——问句太泛，"数据/存储"命中了一堆邻近主题。合成种子的局限，等 P1.5 真实日志再校准。

## P1 待办（P0 留下的口子，按优先级）

| ID | 事项 | 为什么 P0 不做 |
|---|---|---|
| Q-1 | 仲裁未裁决期间同槽位多条 current：`_current_in_slot` 取 `seq` 最新的一条做路由基准 | 收口要等仲裁终态（9.3 的 resolution 落地）才有意义；P0 只如实记账、不猜 |
| Q-2 | INV1 只有"进程内单写者"：跨进程靠 SQLite 事务串行化 | 真 daemon + shim 是 P4；现在写"已兑现唯一写者"是自我美化 |
| Q-3 | `expand` / `explain` / `forget` / `pin` 工具 | P1 的生命周期与可解释性一起做，单独做会返工 |
| Q-4 | 检索相关性只算 2-gram 覆盖率，不含词频与长度惩罚 | P0 200 条量级不值得调参；先用真实召回日志看掉不掉点 |
| Q-5 | `memories_fts` 无 contentless-delete，归档条目仍占索引 | 归档动作 P1 才有；届时随 FTS 维护一起改 |

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
| D-4 | main 分支保护 ruleset 与小改动快车道（PR #2） | `01e3091` |
| P0-1 | schema v1 + 迁移执行器 + INV 触发器 | `a65d396` |
| P0-2/P0-3 | 唯一写入口 + 9.3 冲突路由（纯函数）与 INV 断言 | `0aa6076` |
| P0-4 | FTS5 中文 2-gram + 两因子打分 + 一席制 | `50f5844` |
| P0-5 | CLI：init / remember / recall / status | `1e3990c` |
| P0-6 | 设计文档 §8.1.1 回填实际 DDL + 同步门禁 | 本批 docs 提交 |
| build | pyproject 与门禁配置（运行时零依赖） | `39fde29` |
