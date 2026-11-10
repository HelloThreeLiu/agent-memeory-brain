"""P0 检索回归用的固定种子集。

40 条事实 × 5 种措辞 = 200 条记忆，20 个查询。**这是合成数据**：它证明的是
"检索链路通、回归可重复"，不证明真实场景够用——真实基线要等 P1.5 的召回日志。

每条记忆都不带 ``fact_key``（否则同键的 5 种措辞会按 §9.3 互相失效，只剩 1 条），
查询按"主题标记"判定命中：top-5 里出现该主题任一措辞即算命中。
"""

from __future__ import annotations

MARKER = "【{slug}】"

FACTS: dict[str, tuple[str, ...]] = {
    "api.method": (
        "接口统一用 POST",
        "对外 API 一律走 POST 方法",
        "所有 HTTP 接口的请求方法都是 POST",
        "调用接口用 POST，不用 GET",
        "API 请求方法固定为 POST",
    ),
    "db.path": (
        "记忆库文件放在用户目录下的 .nebula 文件夹",
        "数据库默认路径是 ~/.nebula/memory.db",
        "本地记忆存在 ~/.nebula/memory.db 这个 SQLite 文件里",
        "库文件位置可以用 NEBULA_DB 环境变量改",
        "记忆数据默认落在 .nebula 目录的 memory.db",
    ),
    "ci.python": (
        "CI 固定跑 Python 3.11",
        "持续集成用的是 Python 3.11 版本",
        "流水线里 Python 版本锁在 3.11",
        "GitHub Actions 上只测 Python 3.11",
        "构建环境统一 Python 3.11",
    ),
    "lint.line": (
        "行宽上限 100 字符",
        "ruff 配置的行宽是 100",
        "代码每行不超过 100 个字符",
        "单行长度限制为 100",
        "超过 100 字符的代码行会被静态检查拦下",
    ),
    "branch.protect": (
        "main 分支禁止直接推送",
        "主分支只能通过 PR 合入，不能直推",
        "不允许 git push 到 main",
        "所有改动都要走 pull request",
        "主分支有保护规则，直推会被服务端拒绝",
    ),
    "commit.style": (
        "提交信息用 Conventional Commits 加中文描述",
        "commit message 按 Conventional Commits 写，正文用中文",
        "提交格式是 type(scope): 中文说明",
        "每次提交都要写清类型和作用域",
        "提交说明采用约定式提交规范",
    ),
    "test.gate": (
        "合入前要跑 ruff、mypy、pytest、pip-audit",
        "四件套门禁全绿才能合并",
        "提交前必须通过静态检查、类型检查、单测和依赖审计",
        "CI 上跑那四项检查，缺一不可",
        "门禁包含静态检查、类型检查、测试和依赖漏洞扫描",
    ),
    "deploy.target": (
        "只发布 Windows 版本",
        "P0 只支持 Windows 平台",
        "先只做 Windows，不做 Mac 和 Linux",
        "发布目标是 Windows 桌面",
        "跨平台支持暂时不在范围内",
    ),
    "db.engine": (
        "存储用 SQLite 单文件加 WAL 模式",
        "数据库选 SQLite，一个文件装下全部记忆",
        "底层用 SQLite，开启 WAL 保证读写不打架",
        "不用 Postgres，就用本地 SQLite 文件",
        "数据存在 SQLite 里，WAL 模式",
    ),
    "recall.index": (
        "召回先用 FTS5 加中文 2-gram",
        "检索走全文索引，中文按两个字切",
        "P0 的召回用 FTS5 倒排加 2-gram 分词",
        "搜索用 BM25 排序的全文本索引",
        "中文检索按 2-gram 建 FTS5 索引",
    ),
    "no.vector": (
        "P0 不引入向量检索",
        "这一阶段不做 embedding，先不上向量库",
        "向量和相似度检索等到收集数据之后再说",
        "暂时不用语义向量，只用词法检索",
        "别急着上向量数据库",
    ),
    "no.dep": (
        "运行时零依赖，不新增第三方包",
        "标准库够用，不引新依赖",
        "项目坚持零运行时依赖",
        "除开发工具外不装任何包",
        "新增依赖要走评审",
    ),
    "llm.off": (
        "没配置大模型也必须能跑",
        "零 LLM 可用是硬要求",
        "断网、没有 API key 时功能不能崩",
        "不调模型也要能写入和召回",
        "模型是可选项，不是必需项",
    ),
    "seq.rule": (
        "排序一律用 seq，不看时间戳",
        "唯一排序依据是单调序号 seq",
        "别用创建时间排序，用 seq",
        "seq 单调递增，排序都靠它",
        "时间戳跨进程不可比，排序只用 seq",
    ),
    "delete.rule": (
        "语义记忆不硬删，只能归档",
        "长期事实不许物理删除",
        "要删就归档，行必须留着",
        "语义层的条目只降权不删除",
        "硬删语义记忆是被禁止的",
    ),
    "expire.rule": (
        "失效写 valid_to，不删行",
        "被取代的条目记失效时间，行保留可查",
        "旧结论写 valid_to 和 expired_at，不删除",
        "事实更替用双时间轴标记，不做覆盖",
        "失效只是标记，历史仍然可查",
    ),
    "evidence.rule": (
        "每条语义记忆至少一条证据",
        "semantic 条目必须带 evidence 才能入库",
        "没有证据的长期记忆不许写",
        "写事实必须附来源 episode",
        "证据链是语义记忆的入场券",
    ),
    "claim.rule": (
        "未验证的意见只能记成 claim",
        "审查意见最多算 claim，不能当事实",
        "别人说的怀疑先记 claim，等验证",
        "claim 需要验证才能升级",
        "没跑过验证的判断不许记成 observation",
    ),
    "decision.rule": (
        "决策只能由用户确认",
        "只有 role=user 能写 decision",
        "agent 不能自己拍板定决策",
        "把 claim 升成 decision 必须人点头",
        "用户拍板的才算决策",
    ),
    "private.rule": (
        "私有片段不进别人的候选集",
        "visibility=private 的内容别人召回不到",
        "私密记忆对其他角色不可见",
        "私有数据在候选池就被过滤掉",
        "别人的私有片段不该出现在我的召回里",
    ),
    "daemon.rule": (
        "只有 daemon 可以写库",
        "所有写入都必须经过 write_service",
        "shim 不直接写数据库",
        "单写者：一个进程负责全部写入",
        "绕过写入服务直接改库是不允许的",
    ),
    "shim.rule": (
        "shim 无状态，只做转发",
        "stdio 桥接层不保存任何状态",
        "转发进程不解析业务逻辑",
        "shim 只管把调用转给 daemon",
        "桥接层挂了不影响宿主继续工作",
    ),
    "budget.token": (
        "单轮注入不超过 12k token",
        "每轮记忆注入有 12k 预算上限",
        "注入总字数控制在一万二千 token 内",
        "超预算就降级，不能无限塞",
        "会话累计注入上限 12k",
    ),
    "inject.order": (
        "内核块必须排在最前面",
        "注入时 L1 放最前，保证前缀稳定",
        "动态内容不许插到内核块前面",
        "prompt 前缀先放内核，再放其余",
        "缓存命中要求前面字节不变",
    ),
    "archive.days": (
        "热度最低一成且 180 天没召回就归档",
        "长期不用的记忆 180 天后归档",
        "归档条件是低热度加半年未触达",
        "半年没被召回的低热度条目可以归档",
        "180 天未召回进入归档候选",
    ),
    "dormant.rule": (
        "热度低于 0.25 且 30 天未召回转休眠",
        "30 天没用到就变 dormant",
        "休眠状态由低热度加一个月未召回触发",
        "dormant 的触发是热度 0.25 以下",
        "一个月不召回就进入休眠",
    ),
    "sleep.tx": (
        "睡眠周期在一个事务里跑完",
        "整合流程整体提交，失败回滚",
        "睡眠周期用单事务，不搞 worktree",
        "六个步骤要么全成要么全退",
        "夜间整合是原子的",
    ),
    "viz.fps": (
        "一万节点星图要跑到 50 帧",
        "可视化目标是一万节点 50fps",
        "星图帧率不能低于 50",
        "万级节点渲染要流畅",
        "首屏两秒内出来，帧率五十以上",
    ),
    "viz.readonly": (
        "星图治理动作必须走写入服务",
        "可视化不能直接改数据库",
        "前端只读派生视图，改数据要回调",
        "钉住、合并、遗忘都要经过 WriteService",
        "图上的操作不能旁路写入",
    ),
    "core.gate": (
        "内核改动要提案审批再应用",
        "L1 变更必须人工确认",
        "内核文件改动走 propose、apply、rollback",
        "人设和边界不能自动改",
        "内核版本化，可回滚",
    ),
    "host.mcp": (
        "宿主只通过 MCP 接入",
        "接入方式是 MCP 工具注入",
        "不绑定任何单一宿主",
        "各宿主统一走 MCP 协议",
        "DSH 只桥接 Tools，不走 resource",
    ),
    "project.scope": (
        "项目键不跨项目生效",
        "同名 fact_key 在不同项目里互不干扰",
        "scope 是 project 的键只在项目内路由",
        "两个项目各有一条构建命令，不互相替代",
        "全局键才跨项目共享",
    ),
    "dedupe.window": (
        "同键同内容 60 秒内合并",
        "一分钟内的重复写入只留一条",
        "幂等窗口是 60 秒",
        "重复内容在窗口内不新建",
        "两条一样的写入会合并证据",
    ),
    "confidence.rule": (
        "规则抽取的置信度是 0.6",
        "正则抽出来的结论置信零点六",
        "规则版抽取给 0.6 置信",
        "LLM 抽取置信 0.75，规则 0.6",
        "人工确认置信度是 1.0",
    ),
    "tool.confidence": (
        "工具输出置信度 0.85",
        "跑测试得到的结果按 0.85 计",
        "构建和测试输出置信零点八五",
        "可执行验证的结论置信 0.85",
        "observation 来自工具时置信较高",
    ),
    "review.rule": (
        "实现方不得自审代码",
        "写代码的人不能给自己出审查结论",
        "审查要换一个人或另开会话",
        "自审无效，必须独立审查",
        "审查输入只有 diff 和需求",
    ),
    "backlog.rule": (
        "待办必须登记进 backlog",
        "需求不能只留在对话里",
        "每件事都要写进 docs/BACKLOG.md",
        "M 级和 L 级的任务都要登记",
        "待办不进 backlog 就等于不存在",
    ),
    "windows.path": (
        "Windows 长路径要避开",
        "路径长度限制是 Windows 上的坑",
        "不要用超长目录嵌套",
        "Windows 下路径过长会失败",
        "单文件库避免了长路径问题",
    ),
    "export.format": (
        "导出用 JSON 快照",
        "快照导出成 JSON 文件",
        "可视化数据从 JSON 快照读",
        "导出的格式是 JSON，不是数据库文件",
        "报告和快照可以进 git",
    ),
    "rollback.rule": (
        "迁移前自动备份数据库",
        "改 schema 之前先备份 memory.db",
        "升级失败要能回滚到旧库",
        "迁移前生成 .bak 备份文件",
        "数据结构出问题优先保数据可回退",
    ),
}

#: 20 个查询 → 期望命中的主题
QUERIES: tuple[tuple[str, str], ...] = (
    ("接口用什么请求方法", "api.method"),
    ("记忆数据库文件放在哪个目录", "db.path"),
    ("持续集成用哪个 Python 版本", "ci.python"),
    ("代码行宽限制是多少", "lint.line"),
    ("能不能直接往主分支推代码", "branch.protect"),
    ("提交说明应该怎么写", "commit.style"),
    ("合并之前要跑哪些检查", "test.gate"),
    ("支持哪些操作系统", "deploy.target"),
    ("用什么数据库存储数据", "db.engine"),
    ("检索是怎么做的", "recall.index"),
    ("能不能加第三方依赖", "no.dep"),
    ("没有配置大模型还能用吗", "llm.off"),
    ("排序按什么字段", "seq.rule"),
    ("语义记忆可以物理删除吗", "delete.rule"),
    ("记忆失效怎么记录", "expire.rule"),
    ("一条语义记忆要凭什么入库", "evidence.rule"),
    ("审查意见能记成事实吗", "claim.rule"),
    ("谁可以确认决策", "decision.rule"),
    ("私有内容别人能看到吗", "private.rule"),
    ("谁负责写数据库", "daemon.rule"),
)


def seeded_contents() -> list[tuple[str, str]]:
    """返回 ``(主题, 带标记的正文)``；标记让"命中"可机械判定。"""
    rows: list[tuple[str, str]] = []
    for slug, phrasings in FACTS.items():
        for phrasing in phrasings:
            rows.append((slug, MARKER.format(slug=slug) + phrasing))
    return rows
