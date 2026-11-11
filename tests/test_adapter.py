"""P1.5a：作用域解析与指针块（§10.4 / §10.5 B 档）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from nebula.adapter.pointer import BEGIN, END, install, render_block, uninstall
from nebula.adapter.project import resolve

REMOTE = "https://github.com/HelloThreeLiu/SciMate.git"


def _make_repo(root: Path, *, remote: str | None = REMOTE) -> Path:
    """手写一个最小 git 仓库（不调 git 命令——解析器本来就不该依赖它）。"""
    git_dir = root / ".git"
    git_dir.mkdir(parents=True)
    config = '[core]\n\trepositoryformatversion = 0\n'
    if remote:
        config += f'[remote "origin"]\n\turl = {remote}\n\tfetch = +refs/heads/*\n'
    (git_dir / "config").write_text(config, encoding="utf-8")
    return root


# --------------------------------------------------------------------- project


def test_git_remote_identity_is_path_independent(tmp_path: Path) -> None:
    """同一 remote、两个不同本地路径 → 同一个 project_id。

    这正是这套算法存在的理由：Windows 与 WSL 上的同一个仓库路径不同，
    用路径 hash 会各记一套记忆。
    """
    first = _make_repo(tmp_path / "a")
    second = _make_repo(tmp_path / "b")
    assert resolve(first).project_id == resolve(second).project_id
    assert resolve(first).source == "remote"
    assert resolve(first).name == "SciMate"


def test_git_without_remote_falls_back_to_toplevel(tmp_path: Path) -> None:
    root = _make_repo(tmp_path / "repo", remote=None)
    nested = root / "nebula" / "store"
    nested.mkdir(parents=True)

    from_root = resolve(root)
    from_nested = resolve(nested)
    assert from_root.source == "toplevel"
    assert from_root.project_id == from_nested.project_id
    assert from_root.root == from_nested.root == root.resolve()


def test_loose_directory_is_marked(tmp_path: Path) -> None:
    loose = tmp_path / "no-git"
    loose.mkdir()
    ref = resolve(loose)
    assert ref.source == "loose"
    assert ref.is_loose is True
    # 同目录不同层级 → 不同 id（这就是"换路径即失忆"，必须在返回值里说得出来）
    (loose / "sub").mkdir()
    assert resolve(loose / "sub").project_id != ref.project_id


def test_explicit_id_wins(tmp_path: Path) -> None:
    _make_repo(tmp_path / "repo")
    ref = resolve(tmp_path / "repo", explicit="my-own-id")
    assert ref.project_id == "my-own-id"
    assert ref.source == "explicit"


def test_manifest_overrides_git(tmp_path: Path) -> None:
    """monorepo 子包用 .nebula/project.json 覆盖（§10.4）。"""
    root = _make_repo(tmp_path / "repo")
    package = root / "packages" / "api"
    (package / ".nebula").mkdir(parents=True)
    (package / ".nebula" / "project.json").write_text(
        '{"project_id": "api-sub"}', encoding="utf-8"
    )
    ref = resolve(package)
    assert ref.project_id == "api-sub"
    assert ref.source == "manifest"
    assert ref.root == package.resolve()


# --------------------------------------------------------------------- pointer


def test_install_creates_file(tmp_path: Path) -> None:
    target = tmp_path / "CLAUDE.md"
    assert install(target) == "created"
    text = target.read_text(encoding="utf-8")
    assert text.count(BEGIN) == 1
    assert text.count(END) == 1
    assert "先调 `recall`" in text
    # 文件是说明书：不许把记忆正文写进来
    assert "## 记忆库（Nebula）" in text


def test_install_is_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "CLAUDE.md"
    install(target)
    first = target.read_text(encoding="utf-8")
    assert install(target) == "unchanged"
    assert target.read_text(encoding="utf-8") == first
    assert target.read_text(encoding="utf-8").count(BEGIN) == 1


def test_install_appends_without_touching_existing_text(tmp_path: Path) -> None:
    target = tmp_path / "AGENTS.md"
    target.write_text("# 我的规则\n\n不要删库。\n", encoding="utf-8")
    assert install(target) == "appended"
    text = target.read_text(encoding="utf-8")
    assert text.startswith("# 我的规则\n\n不要删库。\n")
    assert text.count(BEGIN) == 1


def test_install_updates_a_stale_block(tmp_path: Path) -> None:
    target = tmp_path / "CLAUDE.md"
    target.write_text(f"开头\n\n{BEGIN}\n旧内容\n{END}\n\n结尾\n", encoding="utf-8")
    assert install(target) == "updated"
    text = target.read_text(encoding="utf-8")
    assert "旧内容" not in text
    assert text.startswith("开头")
    assert text.rstrip().endswith("结尾")


def test_uninstall_removes_block_and_keeps_neighbours(tmp_path: Path) -> None:
    target = tmp_path / "CLAUDE.md"
    target.write_text("# 规则\n\n保持简洁。\n", encoding="utf-8")
    install(target)
    assert uninstall(target) is True
    assert target.read_text(encoding="utf-8") == "# 规则\n\n保持简洁。\n"
    assert uninstall(target) is False


def test_broken_marker_pair_raises_without_writing(tmp_path: Path) -> None:
    target = tmp_path / "CLAUDE.md"
    original = f"# 规则\n\n{BEGIN}\n没写完\n"
    target.write_text(original, encoding="utf-8")
    with pytest.raises(ValueError, match="不成对"):
        install(target)
    # 报错的代价必须小于猜错边界的代价：文件一个字节都不能动
    assert target.read_text(encoding="utf-8") == original


def test_crlf_file_keeps_crlf(tmp_path: Path) -> None:
    target = tmp_path / "CLAUDE.md"
    target.write_bytes(b"# rule\r\n\r\nkeep it short.\r\n")
    install(target)
    raw = target.read_bytes()
    # 每个 LF 都属于某个 CRLF 对 → 没有混进裸 LF（否则整文件 diff 会被打乱）
    assert raw.count(b"\n") == raw.count(b"\r\n")


def test_render_block_does_not_pin_a_namespace() -> None:
    """工具名前缀每个宿主不同，写死一个就会在别的宿主上指错。"""
    block = render_block()
    assert "mcp__nebula__" not in block
    assert "`recall`" in block and "`remember`" in block and "feedback(" in block
