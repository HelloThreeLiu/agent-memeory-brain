"""宿主适配（M10）：作用域解析与指令文件。

P1.5 只做 B 档需要的两件事：

- :mod:`nebula.adapter.project` —— ``project_id`` 怎么算（§10.4）；
- :mod:`nebula.adapter.pointer` —— 指令文件里那 5 行"指针块"（§10.5 B 档）。

daemon / stdio shim / A 档 hook 注入属于 P4，这里刻意不碰。
"""
