"""P0-5：CLI 端到端冒烟——断网、无 API key 也能 init / remember / recall。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from nebula.store.schema import CURRENT_SCHEMA_VERSION

ROOT = Path(__file__).resolve().parents[1]


def run_cli(db: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["NEBULA_DB"] = str(db)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(ROOT)
    # 明确抹掉模型相关环境变量：零 LLM 是硬约束
    for key in list(env):
        if key.upper().endswith(("_API_KEY", "_TOKEN")) and key.upper().startswith(
            ("OPENAI", "ANTHROPIC", "DEEPSEEK", "MOONSHOT")
        ):
            env.pop(key, None)
    return subprocess.run(
        [sys.executable, "-m", "nebula", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=str(ROOT),
        timeout=120,
    )


def test_init_creates_database(tmp_path: Path) -> None:
    db = tmp_path / "cli" / "memory.db"
    result = run_cli(db, "init")
    assert result.returncode == 0, result.stderr
    assert db.exists()
    assert f"schema 版本：{CURRENT_SCHEMA_VERSION}" in result.stdout


def test_remember_then_recall(tmp_path: Path) -> None:
    db = tmp_path / "cli.db"
    written = run_cli(db, "remember", "接口统一用 POST", "--key", "api.method")
    assert written.returncode == 0, written.stderr
    assert "已写入" in written.stdout

    recalled = run_cli(db, "recall", "接口用什么方法", "--json")
    assert recalled.returncode == 0, recalled.stderr
    payload = json.loads(recalled.stdout)
    assert payload["hits"][0]["fact_key"] == "api.method"


def test_remember_is_idempotent_within_window(tmp_path: Path) -> None:
    db = tmp_path / "cli.db"
    run_cli(db, "remember", "接口统一用 POST", "--key", "api.method")
    again = run_cli(db, "remember", "接口统一用 POST", "--key", "api.method")
    assert "已合并" in again.stdout


def test_status_reports_counts(tmp_path: Path) -> None:
    db = tmp_path / "cli.db"
    run_cli(db, "remember", "接口统一用 POST")
    result = run_cli(db, "status")
    assert result.returncode == 0, result.stderr
    assert "记忆总数：1" in result.stdout
    assert "当前有效：1" in result.stdout


def test_status_uses_the_current_window(tmp_path: Path) -> None:
    """未来才生效的条目不该算进"当前有效"。"""
    db = tmp_path / "cli.db"
    run_cli(db, "remember", "未来才生效的约定", "--valid-from", "2030-01-01")
    result = run_cli(db, "status")
    assert result.returncode == 0, result.stderr
    assert "记忆总数：1" in result.stdout
    assert "当前有效：0" in result.stdout


def test_write_failure_is_visible(tmp_path: Path) -> None:
    """INV4 拒绝时必须非零退出并说清原因，不能假装写成功。"""
    db = tmp_path / "cli.db"
    result = run_cli(db, "remember", "没有证据的观察", "--type", "observation")
    assert result.returncode == 2
    assert "INV4" in result.stderr


def test_forget_then_restore_round_trip(tmp_path: Path) -> None:
    db = tmp_path / "cli.db"
    run_cli(db, "remember", "接口统一用 POST")
    assert "已进回收站" in run_cli(db, "forget", "mem-1").stdout
    assert "回收站：1" in run_cli(db, "status").stdout

    assert "已复原" in run_cli(db, "restore", "mem-1").stdout
    assert "回收站：0" in run_cli(db, "status").stdout


def test_forget_unknown_id_does_not_crash(tmp_path: Path) -> None:
    db = tmp_path / "cli.db"
    result = run_cli(db, "forget", "mem-404")
    assert result.returncode == 2
    assert "没有这条记忆" in result.stderr


def test_pin_shows_up_in_status(tmp_path: Path) -> None:
    db = tmp_path / "cli.db"
    run_cli(db, "remember", "这条别动")
    assert "置顶" in run_cli(db, "pin", "mem-1").stdout
    assert "置顶：1" in run_cli(db, "status").stdout
    run_cli(db, "pin", "mem-1", "--off")
    assert "置顶：0" in run_cli(db, "status").stdout


def test_explain_explains_why_it_is_recallable(tmp_path: Path) -> None:
    db = tmp_path / "cli.db"
    run_cli(db, "remember", "接口统一用 POST")
    result = run_cli(db, "explain", "mem-1")
    assert result.returncode == 0, result.stderr
    assert "可召回" in result.stdout
    assert "四道闸" in result.stdout


def test_explain_reports_the_recycle_bin(tmp_path: Path) -> None:
    db = tmp_path / "cli.db"
    run_cli(db, "remember", "接口统一用 POST")
    run_cli(db, "forget", "mem-1")
    result = run_cli(db, "explain", "mem-1")
    assert "不可召回" in result.stdout
    assert "回收站" in result.stdout


def test_expand_prints_evidence_and_timeline(tmp_path: Path) -> None:
    db = tmp_path / "cli.db"
    run_cli(db, "remember", "接口统一用 POST", "--key", "api.method", "--evidence", "ep-1")
    result = run_cli(db, "expand", "mem-1")
    assert result.returncode == 0, result.stderr
    assert "ep-1" in result.stdout
    assert "生效" in result.stdout


def test_sleep_dry_run_does_not_write(tmp_path: Path) -> None:
    db = tmp_path / "cli.db"
    run_cli(db, "remember", "接口统一用 POST")
    result = run_cli(db, "sleep", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "[dry-run]" in result.stdout
    # 状态没动
    assert "当前有效：1" in run_cli(db, "status").stdout
    assert "回收站：0" in run_cli(db, "status").stdout


def test_sleep_json_is_machine_readable(tmp_path: Path) -> None:
    db = tmp_path / "cli.db"
    run_cli(db, "remember", "接口统一用 POST")
    result = run_cli(db, "sleep", "--json")
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is False
    assert payload["scanned"] == 1
