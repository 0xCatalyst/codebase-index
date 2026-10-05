"""Reads answer from the working tree as it is now, with one index writer at a time.

An agent that edits a file and then asks `refs` about it must see the edit; before
this, `refs` and `impact` answered from the last build and claimed to be fresh.
"""

from __future__ import annotations

import json
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codebase_index import scaffold
from codebase_index.cli import app
from codebase_index.config import load
from codebase_index.indexer.refresh import refresh_if_stale, write_lock

FILES = {
    "src/a.ts": "export function foo(x: number) {\n  return x + 1;\n}\n",
    "src/b.ts": "import { foo } from './a';\nexport function bar() {\n  return foo(2);\n}\n",
}
# Appended to src/b.ts: the call lands on line 6.
NEW_CALL = "export function again() {\n  return foo(9);\n}\n"

runner = CliRunner()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("CBX_NO_AUTO_REFRESH", raising=False)
    root = tmp_path / "repo"
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    result = runner.invoke(app, ["--root", str(root), "index"])
    assert result.exit_code == 0, result.output
    return root


def _db(root: Path) -> Path:
    return root / ".claude" / "cache" / "codebase-index" / "index.sqlite"


def _meta(root: Path, key: str) -> "str | None":
    con = sqlite3.connect(f"file:{_db(root)}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def _add_call(root: Path) -> None:
    with (root / "src" / "b.ts").open("a", encoding="utf-8") as fh:
        fh.write(NEW_CALL)


def _run(root: Path, *args: str):
    result = runner.invoke(app, ["--root", str(root), *args])
    assert result.exit_code == 0, result.output
    return result


def test_refs_answers_from_the_edit_without_a_manual_update(repo: Path):
    _add_call(repo)
    result = _run(repo, "refs", "foo", "--compact")
    assert any(ln.startswith("src/b.ts:6 call again -> foo") for ln in result.stdout.splitlines())
    assert "refreshed 1 changed file(s) before answering" in result.stderr
    assert "index stale" not in result.stdout


def test_impact_and_symbol_see_the_edit_too(repo: Path):
    (repo / "src" / "c.ts").write_text(
        "import { foo } from './a';\nexport const viaC = () => foo(3);\n", encoding="utf-8"
    )
    impact = _run(repo, "impact", "src/a.ts", "--compact")
    assert any(ln.split()[1:2] == ["src/c.ts"] for ln in impact.stdout.splitlines()
               if ln.startswith("d1 "))
    symbol = _run(repo, "--json", "symbol", "viaC")
    assert [s["path"] for s in json.loads(symbol.stdout)["symbols"]] == ["src/c.ts"]


def test_a_fresh_read_does_not_write(repo: Path):
    before = (_meta(repo, "built_at"), _meta(repo, "updated_at"))
    for _ in range(2):
        result = _run(repo, "refs", "foo", "--compact")
        assert result.stderr == ""
    assert (_meta(repo, "built_at"), _meta(repo, "updated_at")) == before


def test_reads_report_stale_when_refresh_is_off(repo: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CBX_NO_AUTO_REFRESH", "1")
    _add_call(repo)
    stale = "# index stale (1 files changed): run `codebase-index update`"
    refs = _run(repo, "refs", "foo", "--compact").stdout.splitlines()
    assert refs[1] == stale
    assert not any(ln.startswith("src/b.ts:6 ") for ln in refs)
    impact = _run(repo, "impact", "src/a.ts", "--compact").stdout.splitlines()
    assert impact[1] == stale


def test_a_read_answers_from_the_current_index_while_another_writer_holds_it(repo: Path):
    _add_call(repo)
    cfg = load(repo)
    with write_lock(_db(repo).parent) as held:
        assert held
        result = refresh_if_stale(cfg, _db(repo), timeout=0.2)
    assert result.lock_timed_out
    assert result.freshness is not None and result.freshness.stale
    assert result.refreshed_files == 0


def test_parallel_stale_reads_refresh_once(repo: Path):
    _add_call(repo)
    cmd = [sys.executable, "-m", "codebase_index", "--root", str(repo), "refs", "foo", "--compact"]
    procs = [subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for _ in range(4)]
    outputs = [p.communicate(timeout=120) for p in procs]
    assert [p.returncode for p in procs] == [0, 0, 0, 0], [err for _, err in outputs]
    for out, _ in outputs:
        assert any(ln.startswith("src/b.ts:6 call again -> foo") for ln in out.splitlines())
    refreshes = sum("refreshed 1 changed file(s)" in err for _, err in outputs)
    assert refreshes == 1, [err for _, err in outputs]


def _template_hook_command() -> str:
    template = Path(scaffold._template_root()) / "examples" / "hooks" / "settings.json"
    data = json.loads(template.read_text(encoding="utf-8"))
    return data["hooks"]["PostToolUse"][0]["hooks"][0]["command"]


def test_shipped_hook_command_runs(repo: Path):
    command = _template_hook_command()
    assert "update --quiet" not in command
    argv = shlex.split(command.split(">")[0])
    assert argv[0] == "codebase-index"
    result = runner.invoke(app, ["--root", str(repo), *argv[1:]])
    assert result.exit_code == 0, result.output


def test_init_repairs_the_broken_hook_spelling(tmp_path: Path):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    broken = "codebase-index update --quiet >/dev/null 2>&1 &"
    settings_path.write_text(json.dumps({"hooks": {"PostToolUse": [
        {"matcher": "Edit|Write|MultiEdit", "hooks": [{"type": "command", "command": broken}]},
    ]}}), encoding="utf-8")
    assert scaffold.merge_hook_settings(tmp_path) is True
    hooks = json.loads(settings_path.read_text(encoding="utf-8"))["hooks"]["PostToolUse"]
    assert [hk["command"] for e in hooks for hk in e["hooks"]] == [
        "codebase-index --quiet update >/dev/null 2>&1 &"
    ]
    assert scaffold.merge_hook_settings(tmp_path) is False
