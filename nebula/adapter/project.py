"""``project_id`` 解析（§10.4）。

规则一句话：**同一个仓库换宿主、换目录，算出来的 id 必须一样；换仓库必须不一样。**

```
显式参数 > 向上找 .nebula/project.json > 向上找 .git（优先用 remote url）
         > 都没有 → 起点的 realpath（loose project）
```

三个刻意的选择：

1. **不调 ``git`` 命令**，直接读 ``.git/config``。零运行时依赖不只是"不装包"，
   也包括不假设机器上装了 git；而且子进程调用在宿主沙箱里经常被拦。
2. **remote 优先于路径**：同一仓库在 Windows 与 WSL 上路径不同，用路径 hash 会
   各记一套记忆。有 remote 就用 remote。
3. **非 git 目录标 ``loose``**：换盘符就失忆这点必须在返回值里说得出来，不能让
   调用方以为"项目记忆怎么没了"（§10.4 明确要求文档写清）。
"""

from __future__ import annotations

import hashlib
import json
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

#: 项目内覆盖文件（monorepo 子包用）
MANIFEST = Path(".nebula") / "project.json"

#: 向上找的层数上限：防止在深目录里为了找一个不存在的 .git 走到盘根
MAX_WALK_UP = 40


@dataclass(frozen=True)
class ProjectRef:
    """解析结果。``source`` 说明这个 id 是从哪来的，便于排障。"""

    project_id: str
    root: Path
    source: str
    name: str | None = None

    @property
    def is_loose(self) -> bool:
        return self.source == "loose"


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def _read_remote(git_dir: Path) -> str | None:
    """从 ``.git/config`` 里取第一个 remote 的 url。

    ``.git`` 可能是文件（worktree / submodule，内容形如 ``gitdir: ../x``），
    此时真正的 config 在那行指向的目录里。
    """
    config = git_dir / "config"
    if git_dir.is_file():
        pointer = git_dir.read_text(encoding="utf-8", errors="replace").strip()
        if not pointer.startswith("gitdir:"):
            return None
        target = Path(pointer.split(":", 1)[1].strip())
        config = (git_dir.parent / target).resolve() / "config"
    if not config.is_file():
        return None

    remote: str | None = None
    in_remote = False
    for raw in config.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            in_remote = line.startswith('[remote "')
            continue
        if in_remote and line.lower().startswith("url"):
            _, _, value = line.partition("=")
            candidate = value.strip()
            if candidate:
                remote = candidate
                break
    return remote


def _name_from_remote(remote: str) -> str | None:
    tail = remote.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    if not tail:
        return None
    return tail[:-4] if tail.endswith(".git") else tail


def _read_manifest(folder: Path) -> str | None:
    path = folder / MANIFEST
    if not path.is_file():
        return None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(parsed, dict):
        return None
    value = parsed.get("project_id")
    return value if isinstance(value, str) and value.strip() else None


def _walk(start: Path) -> tuple[Path, Path | None]:
    """从 ``start`` 向上找：返回 (最靠近的 manifest 目录或起点, 最近的 git 根)。"""
    current = start
    for _ in range(MAX_WALK_UP):
        if _read_manifest(current) is not None:
            return current, None
        if (current / ".git").exists():
            return start, current
        if current.parent == current:
            break
        current = current.parent
    return start, None


def resolve(start: str | Path | None = None, *, explicit: str | None = None) -> ProjectRef:
    """解析当前工作目录对应的项目。

    ``explicit`` 给了就原样当 id 用——调用方明确指定时不该再被推断覆盖，
    否则 ``--project`` 就成了建议而不是指令。
    """
    origin = Path(start) if start is not None else Path.cwd()
    # Windows 上个别被占用的路径 resolve 会抛 OSError；解析失败不该让整个召回挂掉
    with suppress(OSError):
        origin = origin.resolve()

    if explicit:
        return ProjectRef(project_id=explicit, root=origin, source="explicit")

    manifest_dir, git_root = _walk(origin)
    manifest_id = _read_manifest(manifest_dir)
    if manifest_id is not None:
        return ProjectRef(
            project_id=manifest_id, root=manifest_dir, source="manifest", name=manifest_dir.name
        )

    if git_root is not None:
        remote = _read_remote(git_root / ".git")
        if remote:
            return ProjectRef(
                project_id=_digest(remote),
                root=git_root,
                source="remote",
                name=_name_from_remote(remote),
            )
        return ProjectRef(
            project_id=_digest(str(git_root)), root=git_root, source="toplevel", name=git_root.name
        )

    return ProjectRef(
        project_id=_digest(str(origin)), root=origin, source="loose", name=origin.name
    )
