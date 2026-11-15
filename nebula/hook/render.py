"""注入块的渲染与预算（§10.7「注入格式与预算」）。

两条通道各有一个 ``render_*_block``，共用同一套"先截单条、再逐条塞、塞不下就说
另有几条"的逻辑（:func:`_fit_lines`）。这个顺序不能换：

1. 单条正文按 ``LINE_MAX_CHARS`` 截断（保留 id，全文交给 ``expand``）——不丢整条：
   丢一条长记忆等于它永远注入不进来；
2. 逐条（连同它的"异议指针"行）往块里加，加不下就停在上一条；
3. 没进块的条数记成一句"另有 N 条"——**只报数不展开**，展开又把预算吃回去了。

``render_prompt_block`` 的返回值里带着"真正进了块的那几条"，因为记账只该记它们：
被预算挤掉的条目虽然召回了，但没注入给模型，算"用过"就是污染 ``recall_count``
（P1.5c 评审 n-1）。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from nebula.recall.search import Hit

#: 每轮都跑，必须比 MCP 的默认 5 更省
PROMPT_TOP_K = 3

#: ≈200 token。一轮 20 句合计 4000 token，是当前接受的成本上界（§10.7）
PROMPT_MAX_CHARS = 600

#: 开场块：≈400 token。只给"不用问也在那里"的东西
SESSION_MAX_CHARS = 1200

#: 单条正文上限：把 §9.4 的"每条 ≤100 字"落到实现，留一点余量
LINE_MAX_CHARS = 120

PROMPT_HEADER = "[记忆|本次检索]"
SESSION_HEADER = "[记忆|会话开始]"
CORE_TITLE = "〔核心〕"
PROJECT_TITLE = "〔本项目〕"
FOOTER = '（追溯用 expand <id>；这条错了用 feedback <id> "wrong"）'
_ELLIPSIS = "…"


@dataclass(frozen=True)
class Block:
    """一次注入的成品：文本、真正进块的条目、被挤掉的条数。"""

    text: str
    hits: tuple[Hit, ...]
    dropped: int = 0

    @property
    def chars(self) -> int:
        return len(self.text)


def clip(text: str, limit: int = LINE_MAX_CHARS) -> str:
    """折叠空白后按字符截断。按字符不按 token：渲染层不该知道模型怎么分词。"""
    body = " ".join(text.split())
    if len(body) <= limit:
        return body
    return body[: limit - len(_ELLIPSIS)] + _ELLIPSIS


def render_prompt_block(
    hits: Sequence[Hit],
    *,
    top_k: int = PROMPT_TOP_K,
    max_chars: int = PROMPT_MAX_CHARS,
) -> Block:
    """渲染 ``UserPromptSubmit`` 的注入块。

    空列表返回空块（**零命中不注入**：塞一句"没有命中"等于每轮往上下文加噪音）。
    """
    candidates = tuple(hits)
    if not candidates:
        return Block("", ())
    chunks = [_chunk(hit) for hit in candidates[:top_k]]
    lines, dropped = _fit_lines(
        chunks,
        total=len(candidates),
        build=lambda body, rest: _join(PROMPT_HEADER, body, rest, FOOTER),
        max_chars=max_chars,
    )
    if not lines:
        # 一条都装不下：宁可不注入，也不塞半个块（"出现即信号"的前提会破）
        return Block("", (), len(candidates))
    taken = len(candidates) - dropped
    return Block(_join(PROMPT_HEADER, lines, dropped, FOOTER), candidates[:taken], dropped)


def render_session_block(
    pinned: Sequence[Hit],
    project: Sequence[Hit],
    *,
    project_label: str | None = None,
    max_chars: int = SESSION_MAX_CHARS,
) -> Block:
    """渲染 ``SessionStart`` 的开场块：``〔核心〕``（pinned）在前，``〔本项目〕`` 在后。

    段标题跟着该段**第一条**一起进退：如果一条都放不下，标题也不该孤零零留着。
    内容刻意小而稳定——动态的那部分交给每轮的 ``UserPromptSubmit``。
    """
    items: list[Hit] = []
    chunks: list[list[str]] = []
    for title, group in ((CORE_TITLE, pinned), (PROJECT_TITLE, project)):
        for index, hit in enumerate(group):
            items.append(hit)
            chunk = _chunk(hit)
            chunks.append([title, *chunk] if index == 0 else chunk)
    if not items:
        return Block("", ())

    header = SESSION_HEADER + (f" 项目 {project_label}" if project_label else "")
    lines, dropped = _fit_lines(
        chunks,
        total=len(items),
        build=lambda body, rest: _join(header, body, rest, FOOTER),
        max_chars=max_chars,
    )
    if not lines:
        return Block("", (), len(items))
    taken = len(items) - dropped
    return Block(_join(header, lines, dropped, FOOTER), tuple(items[:taken]), dropped)


def _chunk(hit: Hit) -> list[str]:
    """一条记忆占的行：正文一行 + 一席制"落选异议"指针（有则加）。

    指针必须给：§9.4 要求"落选方不许静默吞掉"。按条算进预算，加不下就跟正文一起退。
    """
    lines = [replace(hit, content=clip(hit.content)).format_line()]
    if hit.pointer_count:
        lines.append(f"  另有 {hit.pointer_count} 条未验证异议（expand {hit.id}）")
    return lines


def _fit_lines(
    chunks: Sequence[Sequence[str]],
    *,
    total: int,
    build: Callable[[Sequence[str], int], str],
    max_chars: int,
) -> tuple[list[str], int]:
    """逐条塞进块，返回 ``(实际的行, 被挤掉的条数)``。

    ``total`` 是"本来有多少条"，与 ``chunks`` 的长度可以不等（prompt 块只渲染 top_k，
    但"另有 N 条"要算上 top_k 之外的）。判定用的是 ``build`` 出来的**最终文本长度**，
    不是行长度之和——尾注与"另有 N 条"也算预算。
    """
    lines: list[str] = []
    taken = 0
    for chunk in chunks:
        if len(build([*lines, *chunk], total - taken - 1)) > max_chars:
            break
        lines.extend(chunk)
        taken += 1
    while taken and len(build(lines, total - taken)) > max_chars:
        taken -= 1
        lines = [line for chunk in chunks[:taken] for line in chunk]
    return lines, total - taken


def _join(header: str, lines: Sequence[str], dropped: int, footer: str) -> str:
    body = [header, *lines]
    if dropped:
        body.append(f"（另有 {dropped} 条未列出，按长度上限截断）")
    body.append(footer)
    return "\n".join(body)
