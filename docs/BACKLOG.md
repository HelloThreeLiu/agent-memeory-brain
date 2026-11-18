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

## 上批：P1 生命周期（目标：会忘、能复原、能追责）—— 已收口

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
- 并发用例的成功率：单跑 6/6、全量 16/16 绿；此前在 build 同时进行时失败过 1 次（约 1/20 量级），是 Q-9 那条 Windows 多进程瞬态问题。审查修复批次补了 `BEGIN` 有界重试，但没有清零，故 Q-9 记为"部分收口"而非"已收口"。

## 当前批次：P1.5a 接入面（目标：宿主能调、日志能攒、私有分区不漏）

> 切分理由：a 段全部可在本机自动化验证；b 段（选宿主、配 MCP、连续用 5 天）需要外部时间。
> 设计口径见 §10.6（读路径可见性）与 §10.5（B 档注入落法）。

| ID | 类型 | 级别 | 事项 | 验收标准 | 状态 |
|---|---|---|---|---|---|
| A-1 | feat(schema) | L | schema v4：`injection_log` / `feedback` 两表 | v3→v4 升级用例绿；§8.1.1 文档 DDL 同步门禁绿；旧库升级后原有行不受影响 | done |
| A-2 | feat(store) | L | 写口 `log_injection` / `record_feedback` | 每次都落一行且同事务落 `events`；`feedback` 对不存在的 id 报错而非静默；断言会红 | done |
| A-3 | feat(recall) | L | 读路径可见性口径（§10.6，收口 R-8） | `expand`/`explain` 对 private 无 session 调用方返回"不可见"且**输出不含原文**；`events` 按 scope/visibility 过滤；CLI `--show-all` 可看全部 | done |
| A-4 | feat(adapter) | M | `project_id` 解析（§10.4）+ B 档指针块生成器（§10.5） | git remote / git toplevel / 非 git 路径三种解析用例；指针块重复 install 幂等、uninstall 无残留 | done |
| A-5 | feat(mcp) | L | 零依赖 MCP stdio server + `remember`/`recall`/`expand`/`feedback` 四工具 | `initialize → tools/list → tools/call` 全流程可跑；`recall` 每次落 `injection_log`；断网与零 LLM 均可用；不新增运行时依赖 | done |
| A-6 | test(mcp) | L | 接入面专项门禁 | subprocess 逐行 JSON-RPC 端到端用例；四工具各 ≥1 契约用例 + 错误路径；全门禁绿 + wheel 可出 | done |

**实测结果（2026-11-11，本机 Python 3.13.12 / SQLite 3.53.1）**

- 门禁：`ruff` 0 问题 / `mypy --strict` 0 问题（29 文件）/ `pytest` **227 passed** / `python -m build` 出 `nebula_memory-0.0.3` wheel（零运行时依赖，`pip-audit` 按 CI 口径跳过；wheel 内含新增的 `nebula/mcp/` 与 `nebula/adapter/`，已解包核对）。
- **协议**：真起子进程喂 stdio 跑通 `initialize → notifications/initialized → tools/list → remember → recall → feedback`；故意发一行坏 JSON 后进程不退出，下一条 `ping` 正常响应。`protocolVersion` 原样回显客户端值。
- **注入日志**：命中与未命中的召回各落一行（`memory_ids` 分别是 `[]` 与 `["mem-1"]`），`host`/`project_id` 落全，`chars` 为实际注入文本长度。
- **可见性**：private 条目对别的 session 的 `expand` 返回"不可见"，输出与异常文本**都不含原文**（断言同时查 `9222` 与整句）；历史版本按同口径过滤并给出"N 个版本不可见"；无分区信息的历史事件不返回。反例验证：去掉 private 判定后 11 条用例里 6 条变红。
- **反馈回连**：用 `json_each` 而非 `LIKE` 找最近注入——专测 `mem-1` / `mem-10` 前缀歧义，反馈不会挂错注入。
- **project 解析**：remote / toplevel / loose / manifest 四种来源各有用例；同一 remote 的两个本地路径得到同一个 id（这正是它存在的理由）。
- **指针块**：install 幂等（第二次 `unchanged`）、uninstall 后原文逐字节恢复、CRLF 文件保持 CRLF、标记不成对时抛错且一个字节不改。手动往返冒烟已跑。
- **loose 目录**：非 git 目录不让路径 hash 冒充项目边界——读过滤不用它、`remember` 默认退回 `global`、显式要 `scope=project` 时要求给真 id。

**未覆盖、留给 b 段**：真宿主上的工具调用成功率（模型会不会主动调 `recall`）、5 天 ≥300 条注入记录、三项指标报告、阈值是否要调。这些都需要真实对话量，本机自动化测不出来。

**b 段范围外清单**（明确不在本批做）：选宿主与写配置、指针块写进真实仓库、连续 5 天使用、`nebula report` 三项指标、阈值校准结论、负反馈是否降 `confidence`（要基线才能定降多少，现在拍数字就是猜）。

## 当前批次：P1.5c A 档自动召回（目标：模型不用决定查不查）

> 起因：P1.5b 接入后两天**自发 `recall` 为 0**（14 次调用全是用户显式要求触发）。
> 指令块确实在注入路径上，模型读得到规则但不执行——B 档"靠指令驱动自觉"的假设被证伪。
> 方案与两轮评审见 `.plans/`、`.reviews/`（过程稿，不入库）；契约已写进 §10.7。

| ID | 类型 | 级别 | 事项 | 验收标准 | 状态 |
|---|---|---|---|---|---|
| C-1 | docs(design) | L | §10.7 A 档注入契约 + §9.4 前缀约束偏差登记 | 预算/去重/记账/fail-open/隐私/偏差逐条可查；不假装满足前缀约束 | done |
| C-2 | feat(hook) | L | `prompt_submit`：按 prompt 自动召回 + 增量去重 + 内部消息过滤 | 同 prompt 两轮只记一次账；内部消息零副作用；任何异常退出 0 | done |
| C-3 | feat(hook) | L | `session_start`：开场块 + 非查询选取路径 | pinned 与项目 top-N 按 `importance/heat`；private/他项目不出现；不记账 | done |
| C-4 | test(hook) | L | 契约 / 记账 / 去重 / fail-open / 预算 / 可见性用例 | 36 条新用例，关键断言做变异测试（改坏必红） | done |
| C-5 | chore | M | `scripts/coverage.py`：transcript 数分母的覆盖率统计 | 能给出分会话明细；"只有注入没有 transcript" 单独标出 | done |
| C-6 | chore(host) | L | 宿主配置：两个 wrapper 脚本 + `settings.json` 换掉探针 | hook 由 Git Bash 绝对路径调起，库/解释器走绝对路径 | done（待重启生效） |
| C-7 | test(e2e) | L | 实机连续使用的验收数据 | 覆盖率 ≥80%、每轮 ≤600 字符、`open→stdout` p95 <200ms、`feedback` ≥1 | **todo（阻塞在重启 + 真实使用）** |

**已实测（2026-11-15，本机）**

- 门禁：`ruff` 0 / `mypy --strict` 0（37 文件）/ `pytest` **301 passed**。
- 单体耗时（真库副本，19 条记忆）：`prompt_submit` 注入 1 条 36ms（冷）、二轮 6ms；
  `session_start` 开场 3 条 6–7ms。均远低于 200ms 门槛。
- 去重：同一 prompt 第二轮不注入、`recall_count` 不涨、`injection_log` 落一行 `[]`。
- 记账边界：变异成 `record=True` → 用例红；开场块改成记账 → 两轮会话即把 `recall_count`
  推到 2（正是要防的污染）。
- 覆盖率脚本在**上线前**的基线：58 轮 / 0 行 = 0.0%。这不是"没测出来"，是"确实一次没注入"。
- 端到端（按宿主调用方式，Git Bash 绝对路径调 wrapper）：两条通道都退出 0，stderr 有摘要，
  开场块按 `〔本项目〕` 正常渲染。

**上线前待办**

- ~~重启 WorkBuddy~~：已完成，2026-11-17 审查会话确认**开场块确实进了上下文**
  （`[记忆|会话开始] 项目 agent-memeory-brain` + 4 条）——`SessionStart` 的纯文本 stdout 通道成立。
- 覆盖率还差数据：近 2 天只有 3 轮（2 行注入，66.7%），样本太小，C-7 继续等真实使用。

**本批明确不做**：`Stop` 自动写入（§10.7 非目标 1）、`PostCompact` 清去重集（已知限制 2）、
private 分区注入（已知限制 1）、会话累计预算的 12k 降级阶梯（等真实用量）。

## 审查记录（P1.5c，独立会话执行，2026-11-17）

原始报告 `.reviews/CODE-REVIEW-P1.5c-hook-2026-11-17.md`（过程稿，不入库）。结论
「**打回修复**」，C-1 阻塞合并、M-1/M-2 阻塞 C-7 验收。15 条全部处理，逐条证据如下。

| 编号 | 级别 | 问题 | 处理 | 验证证据 |
|---|---|---|---|---|
| **C-1** | Critical | 另一连接持写锁时 hook 阻塞 **55.3s**（`busy_timeout=10s` × `_BEGIN_ATTEMPTS=5` + 两个写事务），三条承诺全破 | 新增 `nebula/hook/db.py`：hook 自己 connect+migrate，`busy_timeout` 压到 **300ms**，并在迁移**之前**装 progress handler 到点中断；记账与日志合并成一个事务 | 持写锁实测 **2.6s** 返回（旧 55s），stdout 仍有注入块；`test_write_lock_does_not_stall_the_hook_nor_drop_the_injection`；变异：busy_timeout 改回 10000 → 红 |
| M-1 | Major | 覆盖率分母含宿主内部通知（实测 318 轮里 97 轮是 `<task-notification>`），80% 门槛会假红 | 脚本直接 import `nebula.hook.payload` 的两个函数，不写第二份正则 | 新增 6 条 `tests/test_coverage.py`；含 2 轮真实 + 1 轮内部通知的 transcript 数出 2 |
| M-2 | Major | stdout 管道关闭时退出码 **120**（CPython 退出时二次 flush 失败） | `_write` 失败时把 `sys.stdout` 换成 `StringIO`，并**不记账**；顺带兜住 `utf8_streams()` 在 fd 已关闭时抛的 `EBADF` | 子进程 `os.close(1)` 实测 exit **0**（修复前 120），stderr 有「输出失败·未记账」；变异：去掉置换 → 红 |
| M-3 | Major | 去重每轮全表扫 `injection_log`（100k 6.4ms / 300k 20.8ms） | schema **v5** 加 `ix_injection_log_session (session, ts)`；去重拆成两条各自可走索引的查询 | `EXPLAIN` 两条都是 `SEARCH … USING INDEX`；100k 行 `seen_ids` p50 **0.196ms**；`test_migrate_v4_to_v5…` 断言无 `SCAN` |
| M-4 | Major | 记账/落日志失败会连带丢掉本轮注入（块已渲染、stdout 为空） | `_settle` 包 try/except 只写 stderr；输出改为**先 emit 后记账** | `test_accounting_failure_keeps_the_block`（打桩 `log_injection` 抛错，stdout 仍有块）；变异：去掉 try/except → 红 |
| N-1 | Minor | `log_injection` 未传 `now`，一轮两个时钟；去重窗口无上界 | 两处补 `now=parse_ts(stamp)`；窗口加 `ts <= now` | `test_log_row_uses_the_injected_now` 断言日志 ts == T0 |
| N-2 | Minor | 未知 `<tag>` 一律跳过；宿主若改成传包装后的原文，召回整轮失效 | 分两张表：`WRAPPER_TAGS` 剥壳继续召回（只剥整条包住的外壳，保留内容），`INTERNAL_TAGS` 才跳过 | `test_wrapped_prompt_is_unwrapped_before_recall`：带 `<system-reminder>` 的消息仍注入，落库 query 是用户原话 |
| N-3 | Minor | `resolve_for_host(None)` 回落进程 cwd，可能记到别的仓库 | 显式返回 `None`（= 没有项目身份） | `test_host_scope_drops_loose_projects` + 现有 project 用例 |
| N-4 | Minor | 预算"逐条 break"不是最优装箱 | 不改行为，docstring 写明这是有意的（保留分数最高的前 k 条） | — |
| N-5 | Minor | 尾注把 top_k 挤掉也算成"按长度截断" | 改为「（另有 N 条未列出）」 | 相关断言同步 |
| N-6 | Minor | 文档 `AUTO_TOP_K` / 代码 `PROMPT_TOP_K` 分叉 | 统一到代码命名（文档 §10.7 表格改 `PROMPT_*` / `SESSION_MAX_CHARS` / `LINE_MAX_CHARS`） | 文档与 `render.py` 常量逐项对齐 |
| N-7 | Minor | 覆盖率脚本无用例；`stat` 未兜错；`--days` 只看 mtime | 新增 `tests/test_coverage.py`（6 条）；`OSError` 跳过；`--days` 改按轮次 `timestamp` 筛 | 6 条用例全绿 |
| N-8 | Minor | `core_memories` 没进 16 格可见性矩阵 | 矩阵参数化到两个入口（pinned 条目覆盖全部六个分区） | 新增 30 格参数化用例 |
| N-9 | Nit | `Outcome` / `HOST_DEFAULT` 各写一遍；`Payload.event` 未用 | 抽 `nebula/hook/common.py`；事件名不匹配只提示不跳过（跳过会让通道静默失效） | 两入口共用；摘要有「事件名不匹配」 |
| N-10 | Nit | 记账与注入日志两个事务，中间崩溃留不一致 | 合并成一个事务（与 C-1③ 同一处） | C-1 的用例覆盖；`_settle` 单事务 |

**修复后复验（2026-11-17）**：`ruff` 0 / `mypy --strict` 0（39 文件）/ `pytest` **346 passed**。
真库（已迁移到 schema v5）按宿主方式跑 wrapper：注入 1 条 50ms、exit 0。
`feedback` 仍为 0——提醒：注入块自带 id 是它唯一的自然触发点，用起来才有数据。

### 复审（R2，同日，同一审查者）

报告 `.reviews/CODE-REVIEW-P1.5c-hook-R2-2026-11-17.md`（过程稿，不入库）。结论
「**修复后通过**」，首轮 15 条逐条复核通过；新增 2 条 Minor（R2-1 影响 C-7 读数，先修再用）。

| 编号 | 级别 | 问题 | 处理 | 验证证据 |
|---|---|---|---|---|
| R2-1 | Minor | `<user_query>` 用子串 `search`：正文里**提到**这个标签（贴 transcript、讨论 payload）会把话截成标签里的内容，截断后的文本既去召回又落 `injection_log.query` | 只在"去掉标签后没有别的正文"时才采纳 `<user_query>`（`_only_tag_blocks` 闸） | `test_user_query_tag_inside_prose_does_not_truncate`：单标签与双标签两句都原样返回；变异：去掉闸 → 红 |
| R2-2 | Minor | 分母仍多算宿主控制消息：`data-role="error-recovery"` 剥壳后是英文样板句，被当成一轮（实测 1 条就让 6b7c2603 显示 50%、合计 75%） | 统一判定 `payload.is_user_turn`：`data-role` 不是 `user-context` 一律不算一轮，hook 也跳过；`coverage.py` 只用这一个函数 | `test_control_wrapper_is_not_a_user_turn` / `test_control_wrapper_is_skipped_by_the_hook`；实测分母 223 → **208**，近 2 天读数 4/4 = **100%**（原 75%） |

顺带把复审的残余风险 1 也修了：`connect()` 加可选 `busy_timeout_ms`，从建连接那一刻就生效
（`journal_mode = WAL` 的切换也要拿写锁，库还不是 WAL 时会等满 10s）。

**复验（R2 后）**：`ruff` 0 / `mypy` 0 / `pytest` **349 passed**。残余风险 2（migrate 耗时）
与 3（"前缀包装 + 正文在壳外"）已分别登记为"约 10M 行才撞线"与"已支持前缀形态"。

## 审查记录（P1.5a，独立会话执行）

原始报告 `.reviews/CODE-REVIEW-P1.5a-adapter-2026-11-11.md`（过程稿，不入库）。结论「修复后通过」，**C-1 / M-1 阻塞合并**。以下每条都有会红的用例或实跑输出。

| 编号 | 级别 | 问题 | 处理 | 验证证据 |
|---|---|---|---|---|
| C-1 | **Critical** | `_fetch_pool` 在 `project_id` 为 None 时**不加任何 scope 条件**，loose 视图等于全库视图：别的项目的 `scope=project` 条目、所有宿主的 `scope=host` 条目都会被召回注入。而 `deny_reason` 判它们"不可见"——两套口径分叉，P1 审查 M-2 在 scope 轴重演 | 口径下沉到 `nebula/core/viewer.py`（`store` 不该反向依赖 `recall`）：新增 `visibility_predicate` / `scope_predicate` 两个 SQL 片段，与 `deny_reason` 逐条对应；`_fetch_pool` 用它们拼条件，返回前再用 `deny_reason` 复核（只能再删不能补回，失败方向是少召回而非泄漏） | 复现脚本实跑：loose 查项目A / 宿主X 均 `[]`（修复前分别是 `['mem-1']` / `['mem-2']`）；新增 `test_loose_recall_does_not_leak_other_partitions`；反例：SQL 谓词恒真 → 16 条红；旧 `_fetch_pool` 全量视图+无复核 → 20 条红 |
| M-1 | **Major** | `scope=host` 写得到、召不回：项目视图谓词 `(scope='global' OR project_id=?)` 把 host 行（`project_id=NULL`）排除，recall 根本没有 host 维度 | 同一处修复加 host 分支；`recall` 新增 `viewer_host`，MCP/CLI 都传；`remember` 在写口要求 `scope=host` 必须带 `host` | `test_host_scope_is_not_in_project_view` 断言同宿主命中、异宿主不命中（修复前同宿主也是 `[]`）；实跑 myhost 条目在 `project_id=proj-B` 视图下由 `[]` → `['mem-3']`；反例：去掉 host 分支 → 4 条红 |
| m-2 | Minor | `deny_reason` 的 reason 带 owner 的 session id / project hash / host 名，经 `expand`/`explain` 回给模型 | 措辞只留维度名："visibility 不匹配（private 分区…）" / "scope 不匹配（属于另一个项目/宿主）" | 三条断言：reason 不含 `s-1`、`p-1`、`claude-code` |
| m-3 | Minor | `record_feedback` 不校验可见性，可对他人私有条目写 note；且"没有这条记忆"构成 id 存在性预言机 | `record_feedback` 新增 `viewer`，不可见与不存在**报同一句话**；MCP 传 `ctx.viewer` | `test_feedback_on_invisible_memory_looks_exactly_like_missing`：除 id 外措辞逐字一致，`feedback`/`events` 均为 0 行；`test_owner_can_still_give_feedback_on_own_private_memory` 保证没误杀 |
| n-4 | Nit | `pointer.has_block` 无调用方（死代码） | 删除 | ruff 无未使用告警 |
| n-5 | Nit | v4 两表用 `session`，`memories` 用 `session_id` | **不改**，与 `events` 对齐；记档待 P4 统一 | — |
| n-6 | Nit | 混合换行文件在改写时会被统一成多数换行 | **记档**：`install` 判定 `unchanged` 时不碰文件，只有确实要改块时才重写全文；已写进 `_read` 的 docstring | — |

**审查观察项**（不阻塞，登记备查）

1. `injection_log` / `feedback` 无保留策略——等 b 段数据量出来再定清理规则。
2. `_latest_injection` 不按 host/session 过滤，反馈可能挂到别的 session 的注入上——只影响指标归因，报告口径注意。
3. 模型自报 `role="user"` 即可写 decision（INV6 靠工具描述约束，代码不拦）——B 档接受，P4 多 agent 接入前重估。
4. `nebula.mcp` 从 spawn 时 cwd 推断项目——宿主以 home 为 cwd 启动时 git 项目会被判 loose；C-1 修复后读侧安全，写侧本来就退 global。

**修复后复验（2026-11-12）**：`ruff` 0 / `mypy --strict` 0（29 文件）/ `pytest` **262 passed** / `python -m build` 出 `0.0.3` wheel。新增 35 条用例（30 格一致性参数化 + C-1 反例 + M-1 正例 + m-2/m-3 断言）。

**复审（2026-11-12，独立会话）结论「通过」**，C-1 / M-1 / m-2 / m-3 / n-4 全部闭环，**R-8 正式收口**。复审对三条关键断言做了 mutation test（改坏实现看用例是否红）：只把 SQL 谓词改恒真 → 复核兜住不泄漏但一致性用例红；谓词与复核**双变异** → `['mem-1']` 泄漏复现、loose 用例变红；去掉 `record_feedback` 的 viewer 校验 → 反馈写入成功、用例变红。三条都咬合，不是"改断言让它绿"。

复审同时确认一处**行为变化**（规格对齐，非回归）：CLI `nebula recall` 不带 `--project` / `--host` 时，从"全库视图"变为"仅 global 分区"——§10.6 现在对 CLI 同样生效。本机要看全部用显式身份参数或 `expand --show-all`。

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
| Q-9 | **部分收口**。`BEGIN IMMEDIATE` 加有界重试（5 次 / 线性退避），只包"开事务"这一步；非瞬态错误当场抛（`attempts == 1` 断言）。但全量跑仍偶发（实测约 1/20，连跑 22 次里 1 次失败，当时机器同时在跑 build），根因未定位，CI 的 Linux 环境不复现。P4 daemon 成为唯一写者后可整层删掉 |

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
| R-8 | `explain` / `expand` 输出不按 viewer/scope 过滤；`events` 的分区列暂无读路径消费 | **已收口（P1.5a A-3）**。读路径与召回同一条谓词（§10.6）；v4 给 `events` 补了 `project_id`，连带数据一起过滤；CLI 留 `--show-all` 逃生口，MCP 不暴露 |

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
