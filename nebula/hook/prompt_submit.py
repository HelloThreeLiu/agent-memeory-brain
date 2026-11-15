"""``UserPromptSubmit`` hook：按当前 prompt 自动召回并注入（§10.7）。

宿主每轮把用户消息的 JSON 塞进 stdin，本模块回一段
``{"hookSpecificOutput": {"hookEventName": ..., "additionalContext": ...}}``，
宿主把 ``additionalContext`` 拼进模型上下文。

三步是评审核心、实现时最容易被"顺手简化"掉的：

- **步骤 0 先拦宿主内部消息**：探针实测后台任务通知也走这个事件，不拦就是白召回 + 污分母；
- **检索用 ``record=False``，只对截断后仍在块里的条目记账**（M-1 / n-1）：否则同一条
  记忆每轮 +1，``last_recalled`` 永不老化，§9.5 的"30 天未召回"永远不触发；
- **无论成功、无命中、跳过还是失败，都往 stderr 写一行摘要**（M-3）。

用法（宿主配置里由 wrapper 固定解释器与库路径，见 §10.7 配置前提）::

    python -m nebula.hook.prompt_submit --db <绝对路径> --host workbuddy
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from nebula.adapter.project import resolve_for_host
from nebula.core.timeutil import fmt, now_ts
from nebula.hook.dedup import seen_ids
from nebula.hook.payload import Payload, internal_message, parse
from nebula.hook.render import PROMPT_TOP_K, render_prompt_block
from nebula.hook.summary import emit, failure, line, note, utf8_streams
from nebula.recall.search import recall, record_hits
from nebula.store.db import open_database
from nebula.store.write_service import WriteService

#: 必须与 MCP 配置里的 ``--host`` 同名（§10.7 配置前提 3）：去重集的"最近两分钟"
#: 窗口靠它匹配 MCP 落下的行。两边不一致时去重退化成"只认本会话"，且不会有报错。
HOST_DEFAULT = "workbuddy"

#: 自身耗时软上限：超了就放弃注入，绝不拖住宿主（settings.json 里另有 10s 硬超时）
SOFT_DEADLINE_MS = 5000

#: ``injection_log.query`` 只存前 200 字符（§10.7「隐私」）：要它才能回答
#: "哪种问法召不回来"，但不能把用户原话全量落库
QUERY_MAX_CHARS = 200


@dataclass(frozen=True)
class Outcome:
    """一次运行的结果：要注入什么 + 给 stderr 的那一句话。"""

    context: str | None
    summary: str


def run(
    payload: Payload,
    db: str | None,
    *,
    host: str = HOST_DEFAULT,
    now: datetime | None = None,
) -> Outcome:
    """跑完 §10.7 的九步。**异常一律上抛**，由 :func:`main` 兜成 fail-open。"""
    started = time.perf_counter()
    internal, unknown_tag = internal_message(payload.prompt)
    if internal:
        detail = "跳过·内部消息" if unknown_tag is None else f"跳过·未知内部标签 <{unknown_tag}>"
        return Outcome(None, line(detail))

    prompt = payload.prompt.strip()
    if not prompt:
        return Outcome(None, line("跳过·空消息"))

    stamp = fmt(now) if now is not None else now_ts()
    conn = open_database(db)
    try:
        project_id = _project_of(payload.cwd)
        seen = seen_ids(conn, session_id=payload.session_id, host=host, now=now)
        result = recall(
            conn,
            prompt,
            PROMPT_TOP_K,
            project_id=project_id,
            # 不传 viewer_session：MCP 侧没有同口径的 session，传了会出现"一侧可见、
            # 另一侧不可见"，比放弃 private 分区更糟（§10.7 已知限制 1）
            viewer_host=host,
            now=stamp,
            record=False,
        )
        fresh = [hit for hit in result.hits if hit.id not in seen]
        block = render_prompt_block(fresh)
        elapsed_ms = (time.perf_counter() - started) * 1000
        if elapsed_ms > SOFT_DEADLINE_MS:
            return Outcome(None, line(f"跳过·超时 {elapsed_ms:.0f}ms"))
        if block.hits:
            # 记账范围就是"真正进了块的那几条"，不是全部命中
            record_hits(conn, block.hits, host=host, session_id=payload.session_id, now=stamp)
        WriteService(conn).log_injection(
            query=prompt[:QUERY_MAX_CHARS],
            memory_ids=[hit.id for hit in block.hits],
            chars=block.chars,
            top_k=PROMPT_TOP_K,
            host=host,
            session_id=payload.session_id,
            project_id=project_id,
            elapsed_ms=round(elapsed_ms, 3),
        )
    finally:
        conn.close()

    if block.text:
        detail = f"注入 {len(block.hits)} 条 {elapsed_ms:.0f}ms"
    elif not result.hits:
        detail = f"无命中 {elapsed_ms:.0f}ms"
    elif not fresh:
        detail = f"跳过·已注入 {elapsed_ms:.0f}ms"
    else:
        detail = f"跳过·预算 {elapsed_ms:.0f}ms"
    return Outcome(block.text or None, line(detail))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nebula.hook.prompt_submit",
        description="UserPromptSubmit hook：按当前 prompt 自动召回并注入（§10.7）",
    )
    parser.add_argument("--db", default=None, help="库路径（默认 ~/.nebula/memory.db）")
    parser.add_argument(
        "--host", default=HOST_DEFAULT, help="宿主名，必须与 MCP 的 --host 同名"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """**永远返回 0**（§10.7 fail-open）：记忆系统不能成为可用性单点。"""
    utf8_streams()
    # 用 parse_known_args：宿主可能在命令行尾巴上多塞事件名之类的参数（Beta 接口），
    # 多一个参数就抛 SystemExit=2 会让 hook 直接失败
    args, _ = build_parser().parse_known_args(argv)
    try:
        outcome = run(parse(sys.stdin.read()), args.db, host=args.host)
        if outcome.context:
            emit("UserPromptSubmit", outcome.context)
    except Exception as exc:  # 库、检索、渲染、输出，任何一步出错都不许传导给宿主
        note(failure(exc))
        return 0
    note(outcome.summary)
    return 0


def _project_of(cwd: str | None) -> str | None:
    """从 stdin 的 ``cwd`` 解析项目（§10.4）；解析不了或落在非 git 目录时返回 ``None``。

    ``None`` 的意思是"只看得见 ``global`` 分区"（§10.6），不是"看全部"。
    """
    ref = resolve_for_host(cwd)
    return None if ref is None else ref.project_id


if __name__ == "__main__":
    raise SystemExit(main())
