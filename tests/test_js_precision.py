"""JS/TS graph precision: TSX grammar, tsconfig-aware imports, import-bound edges."""

from __future__ import annotations

import json

import pytest

from codebase_index.config import Config
from codebase_index.discovery.classify import detect_language
from codebase_index.graph.js_modules import strip_jsonc
from codebase_index.indexer.pipeline import build_index
from codebase_index.parsers.treesitter import parse_file
from codebase_index.storage.db import Database

_TSX_AFTER_JSX = """\
export function Header() {
  return <div className="a">Rates & incentives</div>;
}

export function Footer() {
  return <footer />;
}
"""


def test_tsx_file_parses_with_the_tsx_grammar():
    names = {s.name for s in parse_file("typescript", _TSX_AFTER_JSX, path="ui/x.tsx").symbols}
    assert {"Header", "Footer"} <= names


def test_typescript_snippet_without_path_falls_back_to_tsx():
    names = {s.name for s in parse_file("typescript", _TSX_AFTER_JSX).symbols}
    assert {"Header", "Footer"} <= names


def test_ts_file_keeps_the_typescript_grammar():
    # `<T>value` is a type assertion in .ts; the TSX grammar would read it as JSX.
    text = "export function cast(v: unknown) { return <number>v; }\nexport function after() {}\n"
    names = {s.name for s in parse_file("typescript", text, path="a.ts").symbols}
    assert {"cast", "after"} <= names


def test_mts_and_cts_are_typescript():
    assert detect_language("a/b.mts") == "typescript"
    assert detect_language("a/b.cts") == "typescript"


def test_strip_jsonc_handles_comments_trailing_commas_and_strings():
    text = '{ // c\n "a": "http://x/*y*/", /* b */ "p": {"@/*": ["./*",],}, }'
    assert json.loads(strip_jsonc(text)) == {"a": "http://x/*y*/", "p": {"@/*": ["./*"]}}


_FILES = {
    "tsconfig.json": """{
      // comments and trailing commas, as tsc allows
      "compilerOptions": { "strict": true, "paths": { "@/*": ["./*"], }, },
    }""",
    "lib/kv.ts": """\
const now = () => Date.now();
export const kv = {
  get(key: string) { return [key, now()]; },
};
""",
    "lib/format.server.ts": "export function fmt(v: number) { return String(v); }\n",
    "lib/queries.ts": "export const queryKeys = { market: (id: string) => ['m', id] };\n",
    "components/types.ts": "export interface Props { label: string }\n",
    "components/badge.tsx": """\
import { memo } from 'react';
export function Badge() { return <span />; }
export const Pill = memo(function Pill() { return <i />; });
""",
    "components/index.ts": "export { Badge } from './badge';\n",
    "components/ui/sonner.tsx": "export function Toaster() { return null; }\n",
    "components/card.tsx": """\
import { useCallback } from 'react';
import { toast } from 'sonner';
import type { Props } from './types';
import { Badge, Pill } from '@/components/badge';
import { kv } from '@/lib/kv';
import { fmt } from '@/lib/format.server';
import { queryKeys } from '@/lib/queries';

export function Card(p: Props) {
  const items: string[] = [];
  items.push(fmt(Date.now()));
  const onClick = useCallback(() => toast(p.label), [p]);
  kv.get(queryKeys.market('1')[1]);
  return <div onClick={onClick}><Badge /><Pill /></div>;
}
""",
    "components/list.tsx": """\
import { Badge } from '@/components';
export function List() { return <Badge />; }
""",
    "lib/same-file.ts": """\
export const KEYS = { a: 1 };
export function useA() { return KEYS.a; }
export const cfg = 1;
export function shadowed(cfg: number) { return cfg; }
""",
    "tests/stub.test.ts": """\
export function push(x: number) { return x; }
const hooks = { useCallback(cb: unknown) { return cb; } };
export default hooks;
""",
}


@pytest.fixture
def js_index(tmp_path):
    root = tmp_path / "repo"
    for rel, text in _FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    cfg = Config()
    cfg.root = str(root)
    db = Database(tmp_path / "index.sqlite").open()
    build_index(cfg, db, root=root)
    yield db.conn
    db.close()


def _edges(conn, src_path, name, edge_type=None):
    sql = (
        "SELECT e.edge_type, e.resolved, e.confidence, e.dst_kind, "
        "       COALESCE(df.path, sf.path) AS dst_path, s.kind AS dst_symbol_kind "
        "FROM edges e JOIN files f ON f.id = e.file_id "
        "LEFT JOIN files df ON e.dst_kind = 'file' AND df.id = e.dst_id "
        "LEFT JOIN symbols s ON e.dst_kind = 'symbol' AND s.id = e.dst_id "
        "LEFT JOIN files sf ON sf.id = s.file_id "
        "WHERE f.path = ? AND e.dst_name = ?"
    )
    args = [src_path, name]
    if edge_type:
        sql += " AND e.edge_type = ?"
        args.append(edge_type)
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def test_alias_relative_and_dotted_imports_resolve_exactly(js_index):
    targets = {
        name: _edges(js_index, "components/card.tsx", name, "import")[0]
        for name in ("./types", "@/components/badge", "@/lib/kv", "@/lib/format.server")
    }
    assert targets["./types"]["dst_path"] == "components/types.ts"
    assert targets["@/components/badge"]["dst_path"] == "components/badge.tsx"
    assert targets["@/lib/kv"]["dst_path"] == "lib/kv.ts"
    assert targets["@/lib/format.server"]["dst_path"] == "lib/format.server.ts"
    assert all(t["confidence"] == "extracted" for t in targets.values())


def test_package_import_is_not_guessed_onto_a_same_named_file(js_index):
    (edge,) = _edges(js_index, "components/card.tsx", "sonner", "import")
    assert edge["resolved"] == 0


def test_barrel_reexport_is_an_import_edge(js_index):
    (edge,) = _edges(js_index, "components/index.ts", "./badge", "import")
    assert edge["dst_path"] == "components/badge.tsx"


def test_imported_call_resolves_to_its_module(js_index):
    (edge,) = _edges(js_index, "components/card.tsx", "fmt", "call")
    assert edge["dst_path"] == "lib/format.server.ts"
    assert edge["confidence"] == "extracted"


def test_member_call_on_imported_object_resolves_to_its_member(js_index):
    (edge,) = _edges(js_index, "components/card.tsx", "get", "call")
    assert edge["dst_path"] == "lib/kv.ts" and edge["dst_symbol_kind"] == "method"


def test_builtins_and_package_calls_stay_unresolved(js_index):
    for name in ("now", "push", "useCallback", "toast"):
        edges = _edges(js_index, "components/card.tsx", name, "call")
        assert edges and all(e["resolved"] == 0 for e in edges), name


def test_jsx_and_type_uses_are_references(js_index):
    for name, path in (("Badge", "components/badge.tsx"), ("Pill", "components/badge.tsx"),
                       ("Props", "components/types.ts"), ("queryKeys", "lib/queries.ts")):
        refs = _edges(js_index, "components/card.tsx", name, "reference")
        assert refs and refs[0]["dst_path"] == path, name


def test_barrel_import_reaches_the_definition(js_index):
    (ref,) = _edges(js_index, "components/list.tsx", "Badge", "reference")
    assert ref["dst_path"] == "components/badge.tsx" and ref["confidence"] == "inferred"


def test_module_constants_and_wrapped_components_are_symbols(js_index):
    kinds = dict(js_index.execute(
        "SELECT s.name, s.kind FROM symbols s JOIN files f ON f.id = s.file_id "
        "WHERE s.name IN ('queryKeys', 'kv', 'Pill', 'now', 'onClick')"
    ).fetchall())
    assert kinds["queryKeys"] == "constant" and kinds["kv"] == "constant"
    assert kinds["Pill"] == "function" and kinds["onClick"] == "function"
    assert kinds["now"] == "function"


def test_file_impact_follows_alias_imports(js_index):
    from codebase_index.graph.expand import impact_lookup

    result = impact_lookup(js_index, "lib/queries.ts", direction="up", depth=1)
    assert "components/card.tsx" in result.files


class _LazyDimBackend:
    """Like the local/external backends: the width is unknown until the first embed."""

    enabled = True
    name = "lazy"

    def __init__(self):
        self.dim = 0

    def embed(self, texts):
        self.dim = 4
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


def test_vectors_build_when_backend_learns_its_width_lazily(tmp_path, monkeypatch):
    pytest.importorskip("sqlite_vec")
    import codebase_index.indexer.pipeline as pipe
    from codebase_index.storage import repo

    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.ts").write_text("export function a() { return 1; }\n", encoding="utf-8")
    monkeypatch.setattr(pipe, "resolve_backend", lambda cfg, warn=None: _LazyDimBackend())
    cfg = Config()
    cfg.root = str(root)
    cfg.embeddings.enabled = True
    db = Database(tmp_path / "index.sqlite").open()
    stats = build_index(cfg, db, root=root)
    assert stats.vectors > 0
    assert repo.get_vec_meta(db.conn)["dim"] == 4
    db.close()


def test_same_file_uses_of_module_level_names_are_references(js_index):
    (ref,) = _edges(js_index, "lib/same-file.ts", "KEYS", "reference")
    assert ref["dst_path"] == "lib/same-file.ts" and ref["resolved"] == 1


def test_a_name_declared_twice_in_a_file_gets_no_same_file_references(js_index):
    # `cfg` is also a parameter: a use of it could be either, so none is recorded.
    assert _edges(js_index, "lib/same-file.ts", "cfg", "reference") == []
