"""A 档注入：宿主 hook 入口（§10.7）。

与 ``nebula.mcp`` 并列——一个是"模型主动来问"，一个是"宿主每轮自动送来"。
本层只做编排，四件事各占一个模块：

- :mod:`nebula.hook.payload` —— stdin 的 JSON 怎么解、内部消息怎么认；
- :mod:`nebula.hook.dedup` —— 本会话已注入过什么（A 档特有的增量去重）；
- :mod:`nebula.hook.render` —— 注入块长什么样、超预算怎么截；
- :mod:`nebula.hook.summary` —— stderr 那一行摘要。

本层不写 SQL：记账与注入日志一律经 :mod:`nebula.store.write_service`（INV1）。
"""

from __future__ import annotations
