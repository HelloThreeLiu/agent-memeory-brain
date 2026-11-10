# Nebula

> 给 AI 助手一套**能追溯、能回滚、能看见**的记忆。本地优先，零模型也能跑。

**状态：P0 骨架可用（v0.0.1）。** 设计文档经过三轮独立评审，25 条意见全部处理；P0 已落地 schema、不变量断言、中文两因子召回与 CLI。还没有宿主接入（P1.5 起），`explain` / `forget` 等治理能力在 P1。

---

## 为什么做这个

现在的 AI 助手有三类失忆：

1. **跨会话失忆**：昨天定下的接口约定，今天要重新讲一遍。
2. **跨宿主各自为政**：在 ZCode 里写的决定，Claude Code 不知道；记忆插件各存各的，换个工具就归零。
3. **记了不可解释**：它"记得"你不吃香菜，但你查不到这条记忆是什么时候、从哪句话来的；想纠正也无从下手。

Nebula 想解决的是第三类——**记忆的可解释性与可撤销性**。前两类是顺带的结果。

## 它是什么

一个四层记忆引擎 + 一个能反向治理的星图界面：

- **四层**：内核层（人设与边界，只读）、语义层（事实与决策）、情节层（对话与事件，即证据底账）、工作层（当前会话）。
- **四条通路**：写入、晋升、召回、遗忘。成本几乎全在通路的阈值上，不在表结构。
- **双时间轴**：事实被取代时不删除，只写失效时间——可以回答"当时的结论是什么"。
- **证据链**：每条长期记忆都能追到支撑它的原始对话与写入时间。
- **可视化即调试器**：星图不只是好看，点开能改、能合并、能遗忘。

## 四层划分

| 层 | 存什么 | 写入门 | 生命周期 |
|---|---|---|---|
| **L1 核** | 身份、边界、授权、长期目标 | 提案 → diff → 人工确认 | 不衰减，版本化可回滚 |
| **L4 星** | 事实、偏好、决策、关系、程序 | 睡眠周期抽取 + 冲突判定 | 180–3650 天，只降权；不硬删，可归档 |
| **L3 尘** | 对话链、事件、结果、来源 | 回合结束自动切段 | 天级衰减，热→温→归档→回收站 |
| **L2 场** | 当前会话与召回包 | 运行时自动 | 会话级，不落长期库 |

名字沿用认知科学的四类记忆（工作 / 情节 / 语义 / 程序），但**层的分界依据是"权威 × 时间尺度"**——因为它决定写入门与衰减，而类型只决定抽取与检索算法。

## 三条关键设计

**1. 只追加，不覆盖。**
同一个 `fact_key` 出现矛盾时，旧条写 `valid_to` / `expired_at`，行保留、可查历史。失效按 `valid_from` 排序，**与写入顺序解耦**——补录一条三年前的记录也不会把现在的结论覆盖掉。

**2. 每个 `fact_key` 一席。**
召回时同一键最多占一席，席位按档位固定优先：`decision > observation > claim > hypothesis`。否则换过三次的构建命令会占满 top-k，还互相打架。

**3. 断言分档，意见不能变事实。**
审查者说"这里并发会丢更新"，只能记成 `claim`（未验证），不能记成 `fact`。它要变成结论，必须经用户确认，或者自己跑一次验证留下 `observation`。

## 架构

```
宿主：DSH / Claude Code / opencode / ZCode / WorkBuddy
                    │  MCP（工具注入）
            stdio shim × N        ← 无状态转发
                    │
        daemon（唯一写者，单调 seq）
          ├── write_service      ← 所有写入必经此处
          ├── recall             预算 + 降级阶梯
          ├── lifecycle          失效 / 归档 / 睡眠整合
          └── adapter            各宿主注入契约
                    │
        SQLite 单文件（WAL）
          └── 派生：FTS 索引 / 快照 JSON → 星图
```

单写者的理由很实际：多个宿主同时开窗口时，只有一处分配 `seq`、跑睡眠整合、算会话预算，才不会出现两条互斥结论都活着的情况。

## 宿主支持

| 宿主 | 接入方式 | 档位 |
|---|---|---|
| DSH | MCP（官方只桥接 Tools） | B |
| Claude Code | MCP + hooks（SessionStart / UserPromptSubmit） | A |
| opencode | MCP + 插件 hook | A |
| ZCode | MCP + hooks | A |
| WorkBuddy | MCP + hooks | A |

A 档 = 自动注入，模型无感；B 档 = 靠指令块驱动模型主动调工具，会漏但能用。

## 现在能跑的部分

```bash
git clone https://github.com/HelloThreeLiu/agent-memeory-brain.git
cd agent-memeory-brain

python -m nebula init                      # 建库、建 FTS 索引（默认 ~/.nebula/memory.db）
python -m nebula remember "接口统一用 POST" --key api.method
python -m nebula remember "构建命令是 npm run build" --key build.cmd
python -m nebula recall "接口用什么方法"     # 打印注入格式，--json 给机器读
python -m nebula status                    # 库的规模与状态
```

零运行时依赖、断网可用、不需要任何 API key。库路径优先级：`--db` > `NEBULA_DB` > `~/.nebula/memory.db`。

**环境要求**：Python ≥ 3.11，SQLite ≥ 3.39（FTS5 的 `remove_diacritics 2` 与 `UPDATE … RETURNING` 需要它；CPython 3.11+ 的官方 Windows/macOS 构建自带的 SQLite 均满足，部分 Linux 发行版系统库可能偏旧，用 `python -c "import sqlite3;print(sqlite3.sqlite_version)"` 确认）。

## 路线

| 阶段 | 内容 | 状态 |
|---|---|---|
| P0 | schema + 不变量断言 + 两因子召回 + CLI | 已完成（v0.0.1） |
| P1 | 晋升 / 失效 / 归档 / 睡眠周期 / `explain` | 未开始 |
| P1.5 | 单宿主接入，攒真实召回日志（校准阈值的唯一机会） | 未开始 |
| P2 | 星图 v1：快照 + 宇宙视图 + 时间回放 | 未开始 |
| P3 | 星图 v2：神经视图 + 反向治理 | 未开始 |
| P4 | 多宿主：daemon + shim + adapter | 未开始 |
| P5 | 多 Agent 协作：租约、裁决队列、冲突可视化 | 未开始 |

## 文档

| 文档 | 内容 |
|---|---|
| [`docs/记忆系统调研与设计.md`](docs/记忆系统调研与设计.md) | 调研 + 设计主文档（v0.5），含不变量 INV1–INV12 |
| [`docs/BACKLOG.md`](docs/BACKLOG.md) | 本批待办与验收标准 |
| [`WORKFLOW.md`](WORKFLOW.md) | 研发流程：S/M/L 分级、门禁、审查规则 |
| [`AGENTS.md`](AGENTS.md) | 给 AI 协作者的开场说明（跨宿主通用） |
| `docs/评审意见-GLM-*.md` | 三轮独立评审原文（保留作决策记录） |

## 仓库结构

```
agent-memeory-brain/
├── docs/            设计文档、待办、评审记录
├── WORKFLOW.md      研发流程（硬约束）
├── AGENTS.md        AI 协作入口
├── .github/         CI 门禁
├── nebula/          引擎
│   ├── core/        纯逻辑：路由、打分、分词、时间（无 I/O，可穷举单测）
│   ├── store/       schema、迁移、write_service（唯一写入口）
│   ├── recall/      候选池谓词 + FTS5 + 一席制
│   └── __main__.py  CLI
└── tests/           不变量断言与检索回归
```

## 参与开发

1. 读 `WORKFLOW.md`，按 S/M/L 分级决定流程深度。
2. M/L 级先给方案（改哪些文件、怎么验证、拆几个 commit），确认后再动手。
3. 提交前跑门禁，全绿才许合入：

```bash
ruff check . && mypy nebula && pytest -q && pip-audit
```

4. 实现方不得自审——审查要另起会话，输入只有 diff + 需求 + 验收标准。

## 许可

[MIT](LICENSE)。记忆数据永远属于使用者，本项目不上传、不同步、不遥测。
