"""零 LLM / 零网络 / 零运行时依赖的机械守卫。

不是"我们不会加网络调用"的口头承诺，而是每次跑测试都会红的断言。
"""

from __future__ import annotations

import ast
import tomllib
from collections.abc import Iterator
from pathlib import Path

import nebula

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_MODULES = {
    "requests",
    "httpx",
    "openai",
    "anthropic",
    "aiohttp",
    "urllib",
    "socket",
    "http",
    "openai_api",
}


def _imported_modules(path: Path) -> Iterator[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module.split(".")[0]


def test_no_network_or_model_imports() -> None:
    package_root = Path(nebula.__file__).parent
    offenders = [
        f"{path.relative_to(package_root)} → {module}"
        for path in package_root.rglob("*.py")
        for module in _imported_modules(path)
        if module in FORBIDDEN_MODULES
    ]
    assert offenders == [], f"零 LLM 守卫：出现网络/模型依赖 → {offenders}"


def test_no_runtime_dependencies() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["dependencies"] == []
