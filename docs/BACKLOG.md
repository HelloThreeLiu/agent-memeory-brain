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

- 门禁：`ruff` 0 问题 / `mypy --strict` 0 问题 / `pytest` 107 passed / `pip-audit` 0 漏洞（项目依赖为空；本机共享环境里的第三方包漏洞不计，见 m-3）/ `python -m build` 出 wheel。
- 检索基线：200 条种子（40 主题 × 5 措辞）、20 查询，hit@5 **19/20 = 95%**（门槛 70%）；p50 0.4ms、p95 0.63ms（门槛 100ms）。
- 并发：两进程各写 100 条 → 200 个唯一 id，`seq` = 1..200 无洞（连跑 5 次稳定）。
- 唯一未命中的查询是「用什么数据库存储数据」——问句太泛，"数据/存储"命中了一堆邻近主题。合成种子的局限，等 P1.5 真实日志再校准。

## P1 待办（P0 留下的口子，按优先级）

| ID | 事项 | 为什么 P0 不做 |
|---|---|---|
| Q-1 | 仲裁未裁决期间同槽位多条 current：`_current_in_slot` 取 `seq` 最新的一条做路由基准 | 收口要等仲裁终态（9.3 的 resolution 落地）才有意义；P0 只如实记账、不猜 |
| Q-2 | INV1 只有"进程内单写者"：跨进程靠 SQLite 事务串行化 | 真 daemon + shim 是 P4；现在写"已兑现唯一写者"是自我美化 |
| Q-3 | `expand` / `explain` / `forget` / `pin` 工具 | P1 的生命周期与可解释性一起做，单独做会返工 |
| Q-4 | 检索相关性只算 2-gram 覆盖率，不含词频与长度惩罚 | P0 200 条量级不值得调参；先用真实召回日志看掉不掉点 |
| Q-5 | `memories_fts` 无 contentless-delete，归档条目仍占索引 | 归档动作 P1 才有；届时随 FTS 维护一起改 |
| Q-6 | `events` 没有 `scope` / `visibility` 列 | P0 已把 merge 事件 payload 里的原文改成只存摘要（`incoming_hash`），但表结构本身仍无分区字段；P1 做 `explain` / 导出前必须补列，否则审计表跨 scope 泄漏 |
| Q-7 | merge 时正文升级到超集后，被覆盖的旧措辞不再单独可查 | 旧措辞是新文的子串，信息没丢，但"当时怎么说的"查不到；P1 若要严格双时间轴，应把旧表述落成 evidence 文本或历史行 |
| Q-8 | `recall` 不写 `last_recalled` / `recall_count` | 9.2 的晋升触发（`recall_count ≥ 3`）与 9.5 的衰减/auto-archive 全依赖这两个字段。P0 只读，P1 必须补；补的时候要注意"召回写库"仍须经 `write_service` |

## 审查记录（P0 首轮，独立会话执行）

首轮结论「打回」，12 条发现全部处理；每条都有会红的用例：

| 编号 | 问题 | 处理 |
|---|---|---|
| C-1 | 包含关系把否定句判成补充：`禁用 GET 方法` 被 `用 GET 方法` merge 掉 | 加否定词判据，多出来的片段含 不/别/禁/勿/never/not 时不 merge，进路由 |
| M-2 | 幂等窗口不看 `status`/`valid_to`：A→B→A 会把第三次写入静默丢弃 | 幂等只认同槽位**当前有效**的行 |
| M-3 | 一席制只按 `fact_key` 分组，跨项目的同名键被合并成一席 | 席位 = (scope, project_id, fact_key) |
| M-4 | 组内有仲裁对子就整组放行，一个槽位开出 3 席 | 只保留仲裁对子，其余照常抢 1 席并给指针 |
| M-5 | `valid_to` 谓词没有可红断言（被一席制掩盖） | 补无 `fact_key` 的过期条用例 + 断言 `candidate_count` |
| M-6 | 同日回退 seq 会写出 `valid_to < valid_from` 的倒挂区间 | 按真实时刻比较，只有时刻相同才回退 seq（设计文档 9.3 同步修正） |
| m-7 | merge 丢细节、烧 seq | 超集时升级正文并重建 FTS；merge 归还 seq |
| m-8 | 幂等跨 visibility/session 合并，绕过 INV7 | 幂等与槽位查询都加 visibility/session 过滤 |
| m-9 | 项目视图把 `scope=host` 的条目一起捞进来 | 条件改为 `scope='global' OR project_id=?` |
| m-10 | `private` 但无 session 的条目谁都召回不到 | 写入时直接拒绝 |
| m-11 | merge 事件 payload 存了原文，审计表跨 scope 泄漏 | payload 只存 `incoming_hash` + 摘要 |
| n-12 | 注入格式缺「未验证」标记 | claim / hypothesis 一律带 `\|未验证` |

### 二轮审查（独立会话，结论「修复后通过」，阻塞项 M-1）

| 编号 | 问题 | 处理 |
|---|---|---|
| M-1 | 仲裁记账按 `fact_key` 全局复用：跨 session 的同名键共用一条仲裁、B 路由顶掉一侧后旧仲裁仍 open，新挑战对子**无任何记账**，召回静默消失 | schema v2（槽位列 + 槽位级 open 唯一索引）+ 复用前校验对子两侧仍 current + 过期对子自动终态（`resolution`=存续侧、`resolved_by=system:b-route`）；两个反例用例先红后绿 |
| m-1 | merge 升级正文后 `content_hash` 未同步 | 升级分支一并更新 hash；补断言 |
| m-2 | SQLite 版本下限未声明（`remove_diacritics 2` 需 ≥3.39） | README 注明 |
| m-3 | `pip-audit` 审计整个共享环境，本机永远无法复现"全绿" | CI 改为按 `pyproject.toml` 的 `dependencies` 判空：零依赖跳过（WORKFLOW ④ 原文即"有依赖后"），非空仍审；README 注明本机口径 |
| n-1 | 「未验证」标记无直接断言 | 补 `Hit.format_line` 用例 |
| n-2 | `status` 的"当前有效"用 `valid_to IS NULL`，把未来生效行算进来 | 改用候选池同款时间窗谓词 |
| n-3 | `supersede_new` 的 `supersedes` 边方向写反 | 改为 old→draft，补方向断言 |
| n-4 | `WriteResult.superseded` 在补录分支装自身 id，命名易误读 | 保留语义，注释写明是"本条已落历史" |

**观察项（已登记为口子，不在本批修）**

1. `recall` 不更新 `last_recalled` / `recall_count` → 9.2 晋升、9.5 衰减全依赖它（Q-8）。
2. 读路径席位分组不含 visibility/session，写路径路由含 → 跨分区同名键在召回时互相挤席（U8，与 U4 同类，需先定口径）。

## 待定（等数据，不排期）

| ID | 事项 | 等什么 |
|---|---|---|
| U4 | `private/shared` 分区粒度按 session 还是按角色 | 需要确认 reviewer 是否要看到 writer 的中间推理 |
| U6 | 是否值得做 `procedure` 通道（双门成本高） | 是否真的存在"被反复确认的工作流" |
| U7 | 注入信任度降到多少该告警 | P1.5 攒够 300 条注入记录后的负反馈率基线 |
| U8 | 召回的一席制要不要也按 `visibility` / `session_id` 分席 | 写路径的槽位是本分区内的，读路径分组目前只看 `(scope, project, fact_key)`：同一个共享事实与某个角色的私有片段会互相挤同一席。§9.4 字面可辩护，但多 agent 接入前必须定口径 |

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
