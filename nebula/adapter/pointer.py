"""指令文件里的"指针块"（§10.5 B 档）。

**文件是说明书，不是存储。** 记忆不进 `CLAUDE.md` / `AGENTS.md`——那会膨胀、污染
仓库、多人必冲突、且没有衰减。这里只放 5 行固定规则，告诉模型"什么时候该调工具"。

两条工程约束：

1. **幂等**：重复 install 只替换 marker 之间的内容，不会叠出第二份；
2. **残缺不猜**：只有 BEGIN 没有 END（或反过来）时直接报错，不改文件。
   猜一个边界的成本是用户丢内容，收益只是省一次手工修复。
"""

from __future__ import annotations

from pathlib import Path

BEGIN = "<!-- nebula:begin -->"
END = "<!-- nebula:end -->"


def render_block() -> str:
    """指针块正文。

    用命令式措辞（§10.5：比"你可以…"有效），并且刻意**不写工具命名空间**：
    Claude Code 是 ``mcp__nebula__remember``、别的宿主又是另一套前缀，写死一个
    就会在另一个宿主上指引错。只给工具名，让宿主自己补前缀。
    """
    return "\n".join(
        [
            BEGIN,
            "## 记忆库（Nebula）",
            "",
            "你有跨会话的长期记忆库，通过 nebula 的 MCP 工具访问。规则：",
            "",
            "1. 会话开始或接到新任务时，先调 `recall`：查询用当前任务的原话，不要改写。",
            "2. 定下结论、被纠正、学到可复用的东西时调 `remember`；用户本人明确下的结论用"
            " `assertion_type=\"decision\"`。",
            "3. 召回结果里带 `|未验证` 的是推测，不能当事实用。",
            "4. 某条记错了调 `feedback(id, kind=\"wrong\")`，不要自己改写正文。",
            "5. 没召回到就不要编造记忆内容，直接说没查到。",
            END,
        ]
    )


def _read(path: Path) -> tuple[str, str]:
    """返回 (规范化成 \\n 的文本, 原换行符)。文件不存在时给空文本 + \\n。

    已知限制（审查 n-6）：**混合换行**（部分 CRLF 部分 LF）的文件在真正改写时会被
    统一成多数的那种换行——指针块之外的字节也跟着变。只有确实要改块时才写回；
    ``install`` 判定 ``unchanged`` 时不碰文件，所以日常重复安装不会触发。
    """
    if not path.is_file():
        return "", "\n"
    with open(path, encoding="utf-8", newline="") as handle:
        raw = handle.read()
    newline = "\r\n" if "\r\n" in raw else "\n"
    return raw.replace("\r\n", "\n"), newline


def _write(path: Path, text: str, newline: str) -> None:
    payload = text if newline == "\n" else text.replace("\n", newline)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(payload)


def install(path: Path) -> str:
    """写入或更新指针块。返回 ``created`` / ``appended`` / ``updated`` / ``unchanged``。"""
    block = render_block()
    text, newline = _read(path)
    exists = path.is_file()

    if not exists or not text.strip():
        _write(path, block + "\n", newline)
        return "created"

    has_begin = BEGIN in text
    has_end = END in text
    if has_begin != has_end:
        raise ValueError(
            f"{path} 里的 nebula 标记不成对（begin={has_begin}, end={has_end}），"
            "已停止写入以免覆盖你的内容；请手工修好或删掉残缺标记"
        )

    if has_begin and has_end:
        start = text.index(BEGIN)
        stop = text.index(END) + len(END)
        rebuilt = text[:start] + block + text[stop:]
        if rebuilt == text:
            return "unchanged"
        _write(path, rebuilt, newline)
        return "updated"

    separator = "" if text.endswith("\n") else "\n"
    _write(path, text + separator + "\n" + block + "\n", newline)
    return "appended"


def uninstall(path: Path) -> bool:
    """移除指针块及其留下的空行。没有块时返回 ``False``，不报错。"""
    text, newline = _read(path)
    if BEGIN not in text or END not in text:
        return False
    start = text.index(BEGIN)
    stop = text.index(END) + len(END)
    left = text[:start].rstrip("\n")
    right = text[stop:].strip("\n")
    if left and right:
        merged = left + "\n\n" + right + "\n"
    elif left:
        merged = left + "\n"
    elif right:
        merged = right + "\n"
    else:
        merged = ""
    _write(path, merged, newline)
    return True
