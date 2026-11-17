"""``UserPromptSubmit`` hook：按当前 prompt 自动召回并注入（§10.7）。

宿主每轮把用户消息的 JSON 塞进 stdin，本模块回一段
``{"hookSpecificOutput": {"hookEventName": ..., "additionalContext": ...}}``，
宿主把 ``additionalContext`` 拼进模型上下文。

四步是评审核心、实现时最容易被"顺手简化"掉的：

- **步骤 0 先剥包装标签、再拦内部消息**（N-2）：宿主在 transcript 里会给用户消息套
  ``<system-reminder>…<user_query>真正的话</user_query>``，包装要剥掉继续召回，
  只有 ``<task-notification>`` 这类才是"跳过"；
- **检索用 ``record=False``，只对截断后仍在块里的条目记账**（M-1 / n-1）：否则同一条
  记忆每轮 +1，``last_recalled`` 永不老化，§9.5 的"30 天未召回"永远不触发；
- **先输出、后记账，且两者都不许丢注入**（M-4）：账本是账，注入是读；账本故障只能让
  这一轮少一条日志，不能让用户看不到记忆。写 stdout 失败时不记账——否则这些 id 会被
  去重集记成"已注入"，而模型其实没看到，永久静默；
- **无论成功、无命中、跳过还是失败，都往 stderr 写一行摘要**（M-3）。

用法（宿主配置里由 wrapper 固定解释器与库路径，见 §10.7 配置前提）::

    python -m nebula.hook.prompt_submit --db <绝对路径> --host workbuddy
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from collections.abc import Callable, Sequence
from datetime import datetime

from nebula.adapter.project import resolve_for_host
from nebula.core.timeutil import fmt, now_ts, parse_ts
from nebula.hook.common import HOST_DEFAULT, Outcome, event_notice
from nebula.hook.db import open_hook_database
from nebula.hook.dedup import seen_ids
from nebula.hook.payload import Payload, internal_message, parse, strip_wrappers
from nebula.hook.render import PROMPT_TOP_K, Block, render_prompt_block
from nebula.hook.summary import emit, failure, line, note, utf8_streams
from nebula.recall.search import Hit, recall, record_hits
from nebula.store.write_service import WriteService

#: 自身耗时软上限：超了就放弃注入，绝不拖住宿主（settings.json 里另有 10s 硬超时）。
#: 与 :func:`nebula.hook.db.open_hook_database` 的 deadline 同源，避免两处各算一套。
SOFT_DEADLINE_MS = 5000

#: ``injection_log.query`` 只存前 200 字符（§10.7「隐私」）：要它才能回答
#: "哪种问法召不回来"，但不能把用户原话全量落库
QUERY_MAX_CHARS = 200

#: 本入口服务的事件名（N-9：挂错入口要能看出来）
EVENT = "UserPromptSubmit"


def run(
    payload: Payload,
    db: str | None,
    *,
    host: str = HOST_DEFAULT,
    now: datetime | None = None,
    deliver: Callable[[str], bool] | None = None,
) -> Outcome:
    """跑完 §10.7 的九步。**异常一律上抛**，由 :func:`main` 兜成 fail-open。

    ``deliver`` 负责把注入块写到 stdout 并返回是否成功（``None`` 表示只渲染不输出，
    单元用例走这条路）。它在**记账之前**被调用：写不出去的东西不该被记成"已注入"。
    """
    started = time.perf_counter()
    prompt = strip_wrappers(payload.prompt)
    internal, unknown_tag = internal_message(prompt)
    if internal:
        detail = "跳过·内部消息" if unknown_tag is None else f"跳过·未知内部标签 <{unknown_tag}>"
        return Outcome(None, line(detail))
    if not prompt:
        return Outcome(None, line("跳过·空消息"))

    stamp = fmt(now) if now is not None else now_ts()
    conn = open_hook_database(db, deadline_ms=SOFT_DEADLINE_MS)
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
        elapsed_ms = _elapsed_ms(started)
        if elapsed_ms > SOFT_DEADLINE_MS:
            return Outcome(None, line(f"跳过·超时 {elapsed_ms:.0f}ms"))

        delivered = True
        if block.text and deliver is not None:
            delivered = deliver(block.text)
        if delivered:
            _settle(
                conn,
                block=block,
                prompt=prompt,
                payload=payload,
                host=host,
                project_id=project_id,
                stamp=stamp,
                elapsed_ms=elapsed_ms,
            )
    finally:
        conn.close()

    notice = event_notice(payload.event, EVENT)
    detail = _summary(block, result.hits, fresh, delivered, elapsed_ms)
    if notice is not None:
        detail = f"{notice}·{detail}"
    return Outcome(block.text or None, line(detail))


def _settle(
    conn: sqlite3.Connection,
    *,
    block: Block,
    prompt: str,
    payload: Payload,
    host: str,
    project_id: str | None,
    stamp: str,
    elapsed_ms: float,
) -> None:
    """记账 + 注入日志，**一个事务**（N-10），失败只写 stderr（M-4）。

    合成一个事务是为了消掉"``recall_count`` +1 却没有 ``injection_log`` 行"的中间态：
    那样下一轮会再注入一次并再 +1，而去重集又因为缺行而失效。

    ``now`` 统一用同一个 ``stamp``（N-1）：否则一轮里出现两个时钟，
    ``record_hits`` 记的是注入时刻、日志行记的是真实时刻，测试里能差半年。
    """
    moment = parse_ts(stamp)
    try:
        service = WriteService(conn)
        with service.transaction():
            if block.hits:
                record_hits(
                    conn,
                    block.hits,
                    host=host,
                    session_id=payload.session_id,
                    now=moment,
                )
            service.log_injection(
                query=prompt[:QUERY_MAX_CHARS],
                memory_ids=[hit.id for hit in block.hits],
                chars=block.chars,
                top_k=PROMPT_TOP_K,
                host=host,
                session_id=payload.session_id,
                project_id=project_id,
                elapsed_ms=round(elapsed_ms, 3),
                now=moment,
            )
    except Exception as exc:  # 账本故障不许传导给宿主，更不许吞掉已经渲染好的注入
        note(failure(exc))


def _summary(
    block: Block,
    hits: Sequence[Hit],
    fresh: Sequence[Hit],
    delivered: bool,
    elapsed_ms: float,
) -> str:
    if block.text:
        if not delivered:
            return f"输出失败·未记账 {elapsed_ms:.0f}ms"
        return f"注入 {len(block.hits)} 条 {elapsed_ms:.0f}ms"
    if not hits:
        return f"无命中 {elapsed_ms:.0f}ms"
    if not fresh:
        return f"跳过·已注入 {elapsed_ms:.0f}ms"
    return f"跳过·预算 {elapsed_ms:.0f}ms"


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
        outcome = run(
            parse(sys.stdin.read()),
            args.db,
            host=args.host,
            deliver=lambda text: emit(EVENT, text),
        )
    except Exception as exc:  # 库、检索、渲染，任何一步出错都不许传导给宿主
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


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000


if __name__ == "__main__":
    raise SystemExit(main())
