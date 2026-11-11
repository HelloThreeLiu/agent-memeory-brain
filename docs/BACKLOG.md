# 本批待办（BACKLOG）

> 规则见 `WORKFLOW.md` ①：M/L 级需求必须登记在这里，不允许只存在于对话或记忆里。
> 状态：`todo` / `doing` / `blocked` / `done`。

## 上批：P0 骨架（目标：能存、能召回、能断言）—— 已收口

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

## 当前批次：P1 生命周期（目标：会忘、能复原、能追责）—— 已收口

| ID | 类型 | 级别 | 事项 | 验收标准 | 状态 |
|---|---|---|---|---|---|
| P1-1 | feat(schema) | L | schema v3：`memories` 补 `archived_at` / `deleted_at` / `decayed_at`，`events` 补 `scope` / `visibility` | v2→v3 旧库升级用例；文档 DDL 同步门禁绿 | done |
| P1-2 | feat(store) | L | 生命周期写口：`touch_recall` / `pin` / `archive` / `unarchive` / `forget` / `restore` / `purge_expired` / `resolve_arbitration` | 每个动作有状态 + FTS + 审计事件三件套断言 | done |
| P1-3 | feat(lifecycle) | L | 衰减与遗忘阶梯：heat 半衰期、dormant、auto-archive、回收站、容量上限 | 时间可注入；`pinned` 全程豁免；只用 `now` 参数就能模拟 180 天 | done |
| P1-4 | feat(lifecycle) | L | 睡眠维护周期：单事务、dry-run、报告、幂等、失败整体回滚 | 中途抛错时 heat 也回滚；连跑两次第二次不动 | done |
| P1-5 | feat(recall) | L | 召回记账（Q-8）、weight/confidence 因子、`expand`、`explain` | 命中才记账；`record=False` 只读；explain 覆盖四道闸 | done |
| P1-6 | feat(cli) | M | `explain` / `expand` / `forget` / `restore` / `pin` / `sleep` | 断网、零 API key 可用 | done |
| P1-7 | test(lifecycle) | L | 500 轮连续使用不膨胀 | 活跃条数钉在软上限；总量零丢失；后段注入规模与耗时持平 | done |
| P1-8 | docs(design) | S | 附录 F：P1 落地范围与偏差 | 未落地项逐条给理由 | done |

**实测结果（2026-11-11，本机 Python 3.13.14 / SQLite 3.53.1）**

- 门禁：`ruff` 0 问题 / `mypy --strict` 0 问题 / `pytest` **180 passed**（两轮审查修复后复跑）/ `python -m build` 出 wheel（零运行时依赖，`pip-audit` 按 CI 口径跳过）。
- 检索基线：20 查询 hit@5 **19/20 = 95%**，与 P0 完全一致——加 weight/confidence 两项因子没掉点；p50 0.34ms、p95 0.49ms（`record=False`，纯检索）。
- 500 轮压测（每轮 3 写 + 1 召回，每 50 轮一次 sleep，共 1500 条、时间跨度 1500 天）：总量 1500 一条不丢；活跃 **270**（把软上限压到 300 来测容量机制）；归档 1230（`capacity` 1036 / `decay` 194）；FTS 行数 = 活跃条数（归档条目不占索引）。
- 回收站往返：29 天可复原、31 天拒绝；`purge_expired` 只删超期行，且先清 links / arbitration 再删主体。
- 仲裁终态：left/right 输家退出当前区间但行保留、claim 输家转 `archived(refuted)`、`both` 两条都不动、重复裁决被拒、输家已失效时不撞 INV3 触发器。
- 全量跑测试时偶发一次并发用例 `SQLITE_READONLY`（单跑 5/5 绿），是 Q-9 那条 Windows 多进程已知问题；审查修复批次已补 `BEGIN` 有界重试。

## 原 P1 口子（Q-1..Q-9）的收口

| ID | 处置 |
|---|---|
| Q-1 | **已收口**。`resolve_arbitration` 落 §9.3 三态终态；`merged` 显式拒绝（§9.3 未定义正文归属） |
| Q-2 | 不修。进程内单写者 + SQLite 事务串行化，真唯一写者是 P4 |
| Q-3 | **已完成**。`expand` / `explain` / `forget` / `pin` 全部落地 |
| Q-4 | **部分**。开了 weight（0.10）与 confidence（0.08）；时近性（0.14）有意不开，理由见设计文档附录 F.3 |
| Q-5 | **已收口**。归档/删除/复原与状态变更在同一事务里维护 FTS；500 轮用例断言 FTS 行数 = 活跃条数 |
| Q-6 | **已收口**。`events` 补 `scope` / `visibility`，v3 迁移按 `target` 回填，装回 append-only 触发器后仍拒绝篡改 |
| Q-7 | 未做。merge 升级正文后旧措辞仍不可单独查；严格双时间轴要把它落成 evidence 文本或历史行，等 P1.5 真实数据看是否值得 |
| Q-8 | **已收口**。命中写 `last_recalled` / `recall_count`，dormant 命中即唤醒；写仍经 `write_service` |
| Q-9 | **已收口**。`BEGIN IMMEDIATE` 加有界重试（5 次 / 线性退避），只包"开事务"这一步——语句已执行再重试等于重放半截写入；非瞬态错误当场抛（`attempts == 1` 断言）。P4 daemon 成为唯一写者后这层重试可以删掉 |

## P1 留下的口子（按优先级）

| ID | 事项 | 为什么 P1 不做 |
|---|---|---|
| R-1 | L3 写入管道（episode 切段）与由此而来的睡眠 ①②③ | 没有输入的处理是空转；等 P1.5 接宿主后按真实回合切 |
| R-2 | 复核队列与 `stale` 标记 | `stale` 要先有 confidence 下降的来源（删 L3 的传播），同样依赖 R-1 |
| R-3 | 孤立节点降权 | "孤立"口径未定（无 `derived_from` / 无 `evidence` / 入度 0）。P0 数据几乎全是孤立点，现在降权等于全库降权 |
| R-4 | 时近性因子 | 会惩罚长期有效的 decision，与 G1 冲突；`last_recalled` 数据太新 |
| R-5 | `resolution=merged` | §9.3 未定义合并后正文归属 |
| R-6 | 快照 JSON | P2 星图才消费 |
| R-7 | 半衰期分档表的真实校准 | 现为先验值，需要 P1.5 的召回日志 |
| R-8 | `explain` / `expand` 输出不按 viewer/scope 过滤；`events` 的分区列暂无读路径消费 | P1 审查 n-8：与 U4/U8 开放口径一致，不算新洞；但 P1.5 把 expand 变成 agent 工具时即成跨 session 泄漏面，接宿主前必须先定口径 |

## 审查记录（P1，独立会话执行）

原始报告 `.reviews/CODE-REVIEW-P1-lifecycle-2026-11-11.md`（过程稿，不入库）。结论「修复后通过」，**M-1 / M-2 阻塞合并**。以下每条都有会红的用例或实跑输出，不采信"已修好"。

| 编号 | 级别 | 问题 | 处理 | 验证证据 |
|---|---|---|---|---|
| M-1 | **Major** | 裁决终态在"旧条胜出"时写出倒挂区间：`valid_to` 无条件取赢家 `valid_from`，观测条挑战 decision、用户裁 left 时得到 `valid_to < valid_from`，这条历史成了"从未成立"且被 INV3 触发器锁死 | **先改规格再改实现**：§9.3 新增「终态的区间几何」三档；`_retire_boundary` 按几何取边界（赢家更晚→赢家生效时刻；赢家更早→裁决时刻；缺失/异常→裁决时刻），硬约束 `valid_to >= 输家 valid_from` | repro A：loser `2026-05-02 → 2026-05-03`，`valid_to >= valid_from = True`；新增 `test_resolve_right_truncates_at_the_winner_start` 覆盖"新压旧"几何 |
| M-2 | **Major** | `explain._seat_blocker` 与召回自相矛盾：忽略 open 仲裁双注入、同档用 `seq` 定席（召回用综合分）、偷用 `now_ts()` 不接 `now` | 重写为 `_seat_check`：① 先查 open 对子，命中则豁免并给出"双注入例外"理由；② 时间基统一用传入 `now`；③ 同档只提示不拦（无查询语境算不出 relevance） | repro B：`explain(claim).recallable=True`，理由「与 mem-5 在 open 仲裁中：一席制双注入例外」；新增 3 条用例，其中 `test_explain_honours_double_injection...` 断言 explain 与 recall 结果同口径 |
| m-3 | Minor | `purge_expired` 预读在事务外、DELETE 不复核 status；并发 `restore` 后可能硬删刚复原的记忆 | 预读挪进 `BEGIN IMMEDIATE` 之后；DELETE 追加 `status='deleted' AND deleted_at <= cutoff` | purge 用例（29/31 天边界、先清外键）全绿 |
| m-4 | Minor | 记账阈值与 Q-8 口径不符：弱命中（0.10–0.25）已注入却不计 `recall_count` | 拆开两件事：`touch_recall(wake_ids=...)` 全量记账、仅达阈值者唤醒 | `test_recall_records_weak_hits_without_waking_them`：`recall_count=1` 且 `status` 仍 `dormant` |
| m-5 | Minor | 容量候选含 dormant、水位只数 active：最冷的几乎总是 dormant，先埋能唤醒的条目，active 不降、跨轮不收敛 | 候选池只取 `active`，并排除本轮将休眠/归档的条目，与水位口径一致 | `test_capacity_only_buries_active_rows_not_dormant`：3 条更冷的 dormant 一条不动，归档 3 条 active |
| n-6 | Nit | `except` 里 ROLLBACK 未判 `in_transaction`，SQLITE_FULL/IOERR 类错误会顶掉原始异常 | 加守卫 | 全量用例绿 |
| n-7 | Nit | `sweep_status` 的 `deleted` 分支无生产调用、无用例 | 补两条用例（正向落库 + 白名单拒绝） | `test_sweep_status_moves_rows_to_the_recycle_bin` / `..._rejects_targets_outside_the_whitelist` |
| n-8 | Nit | explain/expand 不按 viewer/scope 过滤；events 分区列暂无读路径消费 | 不改，与 U4/U8 口径一致 | 登记为 R-8 |
| m-9 | Minor | 同刻几何：§9.3 写"时刻相同 → 裁决时刻"，实现用 `>=`（含相等）走"赢家生效时刻"，得到零宽区间 `[t, t)`，丢掉"对子存续过"的记录 | 规格是对的，代码差一个字符：`>=` 改 `>`。同刻与"赢家更早"同归裁决时刻 | `test_resolve_same_moment_uses_the_decision_time`：同刻对子裁 left 得 `t → t+2d`；回退为 `>=` 时该用例变红（实跑：得到 `2026-05-01`，期望 `2026-05-03`） |
| m-10 | Minor | m-3 修复无回归用例（WORKFLOW ④"没有用例的修复视为未完成"） | 补用例的同时发现修复不完整：扫描后若条目被复原，`DELETE memories` 的条件会跳过它，但 links / arbitration / FTS 已按旧 id 删掉，留下"主体还在、却搜不到也没有关联"的不一致。补二次确认：只按"此刻仍是超期 deleted"的 id 清附属数据 | `test_purge_skips_rows_restored_after_the_scan`：扫描后注入一次 restore，断言 `purged=0`、状态 `active`、FTS 与 links 都还在；改成按扫描结果直接删时用例变红（实跑：`purged` 得到 1） |

**修复后复验（2026-11-11）**：`ruff` 0 / `mypy` 0（20 文件）/ `pytest` **180 passed** / 16 格路由表 **38 passed** / 仲裁三态 **8 passed** / `python -m build` 出 `nebula_memory-0.0.2` wheel。

第二轮复查（同日，独立会话）结论「通过」，两条 Minor 记档后已在本批收口：m-9（同刻几何差一个字符）、m-10（m-3 缺回归用例，补用例时顺带补了 `purge` 的二次确认）。

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
| P0-1 | schema v1 + 迁移执行器 + INV 触发器 | `0611c5a` |
| P0-2/P0-3 | 唯一写入口 + 9.3 冲突路由（纯函数）与 INV 断言 | `6232659` |
| P0-4 | FTS5 中文 2-gram + 两因子打分 + 一席制 | `fd4a86b` |
| P0-5 | CLI：init / remember / recall / status | `a32c557` |
| P0-6 | 设计文档 §8.1.1 回填实际 DDL + 同步门禁 | `903a0d4` |
| build | pyproject 与门禁配置（运行时零依赖） | `4de6c46` |
| review-1 | 首轮审查 12 条修复 | `dd4b40c` / `1061327` / `ab9eb70` |
| review-2 | 二轮审查 M-1（schema v2 + 仲裁槽位） | `451715a` / `76767ef` |
| ci | 零依赖时跳过 pip-audit | `427c644` |
| P1-1 | schema v3：生命周期时间戳 + 审计表分区列 | `2f1b90f` |
| P1-2 | 生命周期写口 + 可复用事务边界 | `317c354` |
| P1-3/P1-4 | 衰减与遗忘阶梯 + 睡眠维护周期 | `30df02b` |
| P1-5 | 召回记账 / 因子 / expand / explain | `9d26aaf` |
| P1-6 | CLI：explain / expand / forget / restore / pin / sleep | `cd1a8dc` |
| P1-7 | 500 轮膨胀测试 + hit@5 门槛收到 90% | `f38cf0d` |
| P1-8 | 设计文档附录 F + 版本号 0.0.2 | `5a5ab85` |
| review-3 | P1 审查闭环（M-1/M-2/m-3/m-4/m-5/n-6/n-7） | `b879772` |
| docs | 审查记录与 R-8 登记 | `2a4df13` |
