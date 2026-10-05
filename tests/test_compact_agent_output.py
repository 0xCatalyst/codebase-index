"""Compact refs/impact output an agent can act on without follow-up reads."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from codebase_index.cli import app

FILES = {
    "src/a.ts": "export function foo(x: number) {\n  return x + 1;\n}\n",
    "src/b.ts": (
        "import { foo } from './a';\n"
        "export function bar() {\n"
        "  return foo(2);\n"
        "}\n"
        'export function leak() { const password = "abcdefghijklmnopqrst"; return foo(3); }\n'
    ),
    "src/c.ts": "import { bar } from './b';\nexport const baz = () => bar();\n",
    "src/d.ts": "import { baz } from './c';\nexport function qux() {\n  return baz();\n}\n",
}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for rel, text in FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    result = CliRunner().invoke(app, ["--root", str(root), "index"])
    assert result.exit_code == 0, result.output
    return root


def _run(root: Path, *args: str) -> list[str]:
    result = CliRunner().invoke(app, ["--root", str(root), *args])
    assert result.exit_code == 0, result.output
    return [ln for ln in result.output.splitlines() if not ln.startswith("[codebase-index]")]


def test_refs_compact_shows_each_call_site_source_line(repo: Path):
    out = _run(repo, "refs", "foo", "--compact")
    site = next(ln for ln in out if ln.startswith("src/b.ts:3 "))
    assert site.endswith("  | return foo(2);")


def test_refs_compact_redacts_secrets_on_the_site_line(repo: Path):
    out = _run(repo, "refs", "foo", "--compact")
    site = next(ln for ln in out if ln.startswith("src/b.ts:5 "))
    assert "abcdefghijklmnopqrst" not in site
    assert "<<redacted:secret>>" in site


def test_impact_compact_lists_each_file_once_and_flags_a_short_walk(repo: Path):
    out = _run(repo, "impact", "src/a.ts", "--compact")
    assert out[0].endswith("· 2 files")
    assert out[1].startswith("# incomplete: 1+ more files beyond depth 2")
    body = [ln for ln in out if not ln.startswith("#")]
    assert [ln.split()[1] for ln in body] == ["src/b.ts", "src/c.ts"]
    assert body[0].startswith("d1 src/b.ts (")


def test_impact_compact_full_depth_has_no_warning(repo: Path):
    out = _run(repo, "impact", "src/a.ts", "--depth", "10", "--compact")
    assert not any(ln.startswith("# incomplete") for ln in out)
    body = [ln for ln in out if not ln.startswith("#")]
    assert [(ln.split()[0], ln.split()[1]) for ln in body] == [
        ("d1", "src/b.ts"), ("d2", "src/c.ts"), ("d3", "src/d.ts"),
    ]
