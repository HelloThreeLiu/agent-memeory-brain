"""注入块的渲染与预算（§10.7「注入格式与预算」）。

三步按顺序做，顺序不能换：

1. 单条正文按 ``LINE_MAX_CHARS`` 截断（保留 id，全文交给 ``expand``）——不丢整条，
   丢一条长记忆等于它永远注入不进来；
2. 逐条（连同它的"异议指针"行）往块里加，加不下就停在上一条；
3. 没进块的条数（超出 ``top_k`` 的 + 被预算挤掉的）记成一句"另有 N 条"——**只报数
   不展开**，展开又把预算吃回去了。

``render_prompt_block`` 的返回值里带着"真正进了块的那几条"，因为记账只该记它们：
被预算挤掉的条目虽然召回了，但没注入给模型，算"用过"就是污染 ``recall_count``
（P1.5c 评审 n-1）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from nebula.recall.search import Hit

#: 每轮都跑，必须比 MCP 的默认 5 更省
PROMPT_TOP_K = 3

#: ≈200 token。一轮 20 句合计 4000 token，是当前接受的成本上界（§10.7）
PROMPT_MAX_CHARS = 600

#: 单条正文上限：把 §9.4 的"每条 ≤100 字"落到实现，留一点余量
LINE_MAX_CHARS = 120

HEADER = "[记忆|本次检索]"
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

    taken: list[Hit] = []
    lines: list[str] = []
    for hit in candidates[:top_k]:
        chunk = _chunk(hit)
        if _size([*lines, *chunk], 0) > max_chars:
            break
        taken.append(hit)
        lines.extend(chunk)

    # 块里还要放"另有 N 条"，加上它可能反而超预算——从尾部退一条再算，直到装得下
    while taken and _size(lines, len(candidates) - len(taken)) > max_chars:
        taken.pop()
        lines = _lines_of(taken)
    if not taken:
        # 一条都装不下：宁可不注入，也不塞半个块（"出现即信号"的前提会破）
        return Block("", (), len(candidates))
    dropped = len(candidates) - len(taken)
    return Block(_assemble(lines, dropped), tuple(taken), dropped)


def _chunk(hit: Hit) -> list[str]:
    """一条记忆占的行：正文一行 + 一席制"落选异议"指针（有则加）。

    指针必须给：§9.4 要求"落选方不许静默吞掉"。按条算进预算，加不下就跟正文一起退。
    """
    lines = [replace(hit, content=clip(hit.content)).format_line()]
    if hit.pointer_count:
        lines.append(f"  另有 {hit.pointer_count} 条未验证异议（expand {hit.id}）")
    return lines


def _lines_of(hits: Sequence[Hit]) -> list[str]:
    lines: list[str] = []
    for hit in hits:
        lines.extend(_chunk(hit))
    return lines


def _assemble(lines: Sequence[str], dropped: int) -> str:
    body = [HEADER, *lines]
    if dropped:
        body.append(f"（另有 {dropped} 条未列出，按长度上限截断）")
    body.append(FOOTER)
    return "\n".join(body)


def _size(lines: Sequence[str], dropped: int) -> int:
    return len(_assemble(lines, dropped))
