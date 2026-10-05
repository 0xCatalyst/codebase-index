"""Global graph pass: resolve unresolved edges against the whole repo and
denormalize symbol degrees.

Runs once after all files are indexed (it needs the complete symbol/file tables).
Symbol-target edges (call/reference/extends/implements) resolve only on an
UNAMBIGUOUS match within the caller's language family — if two definitions share a
name, the edge is left unresolved rather than guessed. A call written against an
owner (`TownService.refresh(x)`, `activation::place(..)`) also resolves when exactly
one type or module of that name defines it; that match is a heuristic, recorded as
'inferred'. Import edges resolve their module path to a file by POSIX path-suffix
match (e.g. 'auth.token' -> '%/auth/token.py').

The pass is batched: one query for all symbols, one for file paths (expanded into
an in-memory suffix map), one executemany for the updates.
The per-edge variant did an indexed lookup per symbol edge and up to ~20
full-table LIKE scans per import edge, which dominated large builds.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from ..parsers import js_refs
from ..parsers.base import module_name, names_a_type
from ..storage import repo
from .js_modules import JsModuleResolver

_SYMBOL_EDGE_TYPES = {"call", "reference", "extends", "implements"}


def build_graph(conn: sqlite3.Connection, root: Optional[Path] = None) -> dict[str, int]:
    resolved = resolve_edges(conn, root)
    repo.recompute_degrees(conn)
    # Everything still unresolved that names a target is, by definition, a target we
    # could not pin to a unique node — record it as 'ambiguous' for the honesty trail.
    repo.mark_ambiguous_edges(conn)
    total_unresolved = len(repo.unresolved_edges(conn))
    # Architecture analytics (communities / god nodes / surprising bridges) are a
    # derived view of the graph. Compute once per build and cache the JSON in meta so
    # the `architecture` command and the HTML export read it instantly. Never let an
    # analysis failure fail the build — the graph itself is already written.
    try:
        from . import analysis

        analysis.refresh_analysis(conn)
    except Exception:  # pragma: no cover - defensive; analytics are best-effort
        pass
    return {"resolved": resolved, "unresolved": total_unresolved}


def resolve_edges(conn: sqlite3.Connection, root: Optional[Path] = None) -> int:
    """Resolve every unresolved edge; `root` lets JS/TS imports read tsconfig paths."""
    edges = repo.unresolved_edges(conn)
    if not edges:
        return 0

    symbol_rows = repo.symbols_for_resolution(conn)
    targets = _SymbolTargets(symbol_rows)
    file_rows = repo.all_file_ids_with_paths(conn)
    suffix_map = _path_suffix_map(file_rows)
    js = _JsTargets(
        symbol_rows, JsModuleResolver(root, {row["path"]: int(row["id"]) for row in file_rows})
    )

    # (dst_kind, dst_id, edge_id, confidence). A name unique within its language
    # family is an exact hit -> 'extracted'; an owner/module match and an import
    # resolved by path-suffix matching are heuristics -> 'inferred'.
    resolutions: list[tuple[str, int, int, str]] = []
    for edge in edges:
        name = edge["dst_name"]
        if language_family(edge["lang"]) == "js":
            js_hit = js.resolve(edge, targets)
            if js_hit is not None:
                resolutions.append((js_hit[0], js_hit[1], edge["id"], js_hit[2]))
            continue
        if edge["edge_type"] == "import":
            file_id = _module_to_file_id(suffix_map, name, lang=edge["lang"])
            if file_id is not None:
                resolutions.append(("file", file_id, edge["id"], "inferred"))
        elif edge["edge_type"] in _SYMBOL_EDGE_TYPES:
            hit = targets.resolve(language_family(edge["lang"]), edge["dst_qualifier"], name)
            if hit is not None:
                resolutions.append(("symbol", hit[0], edge["id"], hit[1]))

    repo.resolve_edges_bulk(conn, resolutions)
    return len(resolutions)


# Languages that call each other directly; any other pair cannot share a call edge.
_LANGUAGE_FAMILIES = {
    "kotlin": "jvm", "java": "jvm", "scala": "jvm",
    "typescript": "js", "javascript": "js",
    "cpp": "c",
}


def language_family(lang: Optional[str]) -> Optional[str]:
    return _LANGUAGE_FAMILIES.get(lang or "", lang)


_Key = tuple[Optional[str], str, str]


class _SymbolTargets:
    """Where a symbol edge may point, indexed three ways within a language family.

    - by name, for names defined once in the family;
    - by (owner type, name), for `TownService.refresh(x)`;
    - by (module, name), for a top-level definition called through its module
      (`activation::place(..)`, `token.refresh()`); see `module_name`.
    A key that two files claim maps to nothing: that is ambiguity, not a guess.
    Overloads (one owner, one file, several rows) map to the first of them.
    """

    def __init__(self, rows: list[sqlite3.Row]) -> None:
        by_name: dict[tuple[Optional[str], str], list[tuple[int, Optional[str]]]] = {}
        by_owner: dict[_Key, tuple[int, int]] = {}
        by_module: dict[_Key, tuple[int, int]] = {}
        clashes: set[_Key] = set()
        for row in rows:
            family = language_family(row["lang"])
            name, sym_id, file_id = row["name"], int(row["id"]), int(row["file_id"])
            parts = (row["qualified"] or name).split(".")
            owner = parts[-2] if len(parts) >= 2 and parts[-1] == name else None
            by_name.setdefault((family, name), []).append((sym_id, owner))
            if owner is not None:
                _claim(by_owner, clashes, (family, owner, name), sym_id, file_id)
            elif len(parts) == 1:
                module = module_name(row["path"])
                _claim(by_module, clashes, (family, module, name), sym_id, file_id)
        self._unique = {key: ids[0] for key, ids in by_name.items() if len(ids) == 1}
        self._owned = {k: v[0] for k, v in by_owner.items() if k not in clashes}
        self._module = {k: v[0] for k, v in by_module.items() if k not in clashes}

    def resolve(
        self, family: Optional[str], receiver: Optional[str], name: str
    ) -> Optional[tuple[int, str]]:
        unique = self._unique.get((family, name))
        # `ApprenticeService.take(x)` is not a call to the family's only `take` when
        # that one belongs to Holdings. A top-level definition keeps matching any
        # receiver (`Utils.fn()` may be a namespace import).
        if unique is not None and not (
            unique[1] is not None and names_a_type(receiver) and receiver != unique[1]
        ):
            return unique[0], "extracted"
        if receiver is None:
            return None
        sym_id = self._owned.get((family, receiver, name))
        if sym_id is None:
            sym_id = self._module.get((family, receiver, name))
        return (sym_id, "inferred") if sym_id is not None else None

    def resolve_owned(self, family: Optional[str], receiver: str, name: str) -> Optional[int]:
        """The member `name` of the one type named `receiver`, if exactly one has it."""
        return self._owned.get((family, receiver, name))


class _JsTargets:
    """JS/TS resolution: exact through the import a name came from (js_refs).

    `import { foo } from './x'; foo()` resolves to `foo` in the file './x' names;
    `kv.get()` on `import { kv }` to the member `kv.get` there. A name no import
    binds in an ES module is same-file (the pipeline already resolved it) or a
    global, and is left unresolved: matching it repo-wide is what bound every
    `Date.now()` to the repo's one `now`. An untyped member call (`items.push(x)`)
    may still match a repo-unique *method*, not a built-in's, as 'inferred'.
    """

    def __init__(self, rows: list[sqlite3.Row], modules: JsModuleResolver) -> None:
        self.modules = modules
        self._by_file: dict[tuple[int, str], int] = {}  # (file, name or qualified) -> symbol
        self._kind: dict[int, str] = {}
        unique: dict[str, list[int]] = {}
        for row in rows:
            if language_family(row["lang"]) != "js":
                continue
            sym_id, file_id = int(row["id"]), int(row["file_id"])
            self._kind[sym_id] = row["kind"]
            if row["parent_id"] is None:
                self._by_file.setdefault((file_id, row["name"]), sym_id)
            qualified = row["qualified"]
            if qualified and qualified != row["name"]:
                self._by_file.setdefault((file_id, qualified), sym_id)
            unique.setdefault(row["name"], []).append(sym_id)
        self._unique = {name: ids[0] for name, ids in unique.items() if len(ids) == 1}

    def resolve(self, edge: sqlite3.Row, targets: "_SymbolTargets") -> Optional[tuple[str, int, str]]:
        name, module = edge["dst_name"], edge["dst_module"]
        if edge["edge_type"] == "import":
            file_id = self.modules.resolve(name, edge["path"])
            return ("file", file_id, "extracted") if file_id is not None else None
        if module == js_refs.UNBOUND:
            return None
        if module == js_refs.SCRIPT:
            sym_id = self._unique.get(name)
            if sym_id is not None and self._kind.get(sym_id) != "method":
                return "symbol", sym_id, "inferred"
            return None
        if module is not None:
            return self._imported(edge, name, module, edge["dst_export"])
        return self._untyped_member(edge, name, targets)

    def _imported(self, edge, name: str, module: str, export: Optional[str]):
        file_id = self.modules.resolve(module, edge["path"])
        if file_id is None:
            return None  # a package, or a path the index does not hold
        export = export or name
        lookup = name if export == "default" else export
        sym_id = self._by_file.get((file_id, lookup))
        if sym_id is not None:
            return "symbol", sym_id, "extracted"
        # Not defined there: a barrel re-exporting it, or `obj.member` where the
        # member is a property the index has no symbol for. A repo-unique definition
        # of the name is the likely target, but only a heuristic one.
        last = lookup.rsplit(".", 1)[-1]
        sym_id = self._unique.get(last)
        if sym_id is None or (last in js_refs.BUILTIN_MEMBERS and "." in lookup):
            return None
        wants_method = "." in lookup
        if (self._kind.get(sym_id) == "method") != wants_method:
            return None
        return "symbol", sym_id, "inferred"

    def _untyped_member(self, edge, name: str, targets: "_SymbolTargets"):
        receiver = edge["dst_qualifier"]
        if edge["edge_type"] in ("extends", "implements"):
            hit = targets.resolve("js", receiver, name)
            return ("symbol", hit[0], hit[1]) if hit is not None else None
        if edge["edge_type"] != "call":
            return None
        if receiver not in ("this", "super") and name in js_refs.BUILTIN_MEMBERS:
            return None
        sym_id = self._unique.get(name)
        if sym_id is not None and self._kind.get(sym_id) == "method":
            hit = targets.resolve("js", receiver, name)
            if hit is not None and hit[0] == sym_id:
                return "symbol", sym_id, "inferred"
        if receiver is not None:
            owned = targets.resolve_owned("js", receiver, name)
            if owned is not None and self._kind.get(owned) == "method":
                return "symbol", owned, "inferred"
        return None


def _claim(
    table: dict[_Key, tuple[int, int]], clashes: set[_Key], key: _Key, sym_id: int, file_id: int
) -> None:
    first = table.get(key)
    if first is None:
        table[key] = (sym_id, file_id)
    elif first[1] != file_id:
        clashes.add(key)


def _path_suffix_map(rows: list[sqlite3.Row]) -> dict[str, Optional[int]]:
    """Map every '/'-aligned path suffix to its file id, or None when ambiguous.

    Mirrors files_with_suffix(path = suffix OR path LIKE '%/suffix') semantics:
    a suffix shared by several files resolves to None (like a multi-row result),
    and matching is case-insensitive the way SQLite LIKE folds ASCII.
    """
    mapping: dict[str, Optional[int]] = {}
    for row in rows:
        parts = row["path"].lower().split("/")
        for i in range(len(parts)):
            suffix = "/".join(parts[i:])
            mapping[suffix] = None if suffix in mapping else int(row["id"])
    return mapping


def _lang_suffixes(lang: Optional[str], base: str, rust_base: str, go_pkg: str) -> list[str]:
    """Import-path suffixes specific to one language, most-specific first."""
    return {
        "python": [f"{base}.py", f"{base}/__init__.py"],
        "typescript": [f"{base}.ts", f"{base}.tsx", f"{base}/index.ts", f"{base}/index.tsx"],
        "javascript": [f"{base}.js", f"{base}/index.js"],
        "java": [f"{base}.java"],
        "kotlin": [f"{base}.kt"],
        "go": [f"{go_pkg}.go"],
        "rust": [
            f"{rust_base}.rs", f"{rust_base}/mod.rs",
            f"src/{rust_base}.rs", f"src/{rust_base}/mod.rs",
        ],
        "csharp": [f"{base}.cs"],
        "ruby": [f"{base}.rb"],
        "php": [f"{base}.php"],
    }.get(lang or "", [])


def _module_to_file_id(
    suffix_map: dict[str, Optional[int]], module: str, lang: Optional[str] = None
) -> Optional[int]:
    """Resolve a module/import path to a unique file id, or None.

    Handles Python, TypeScript/JavaScript, Java/Kotlin/Scala, Rust (:: separator),
    Go (last path segment), C#, Ruby, and PHP import conventions. The importing
    file's `lang` is tried first so that, in a polyglot repo, `import './base'` from
    a .ts file resolves to base.ts rather than a same-named base.py earlier in the
    fixed fallback order. The fallback order is unchanged, so single-language repos
    and the lang-unknown path behave exactly as before.
    """
    base = module.lower().replace(".", "/").strip("/")
    rust_base = module.lower().replace("::", "/").strip("/")
    if not base:
        return None
    # Last segment used for Go package-level resolution
    go_pkg = base.rsplit("/", 1)[-1] if "/" in base else base

    fallback = (
        # Python
        f"{base}.py",
        f"{base}/__init__.py",
        # TypeScript / JavaScript
        f"{base}.ts",
        f"{base}.tsx",
        f"{base}.js",
        f"{base}/index.ts",
        f"{base}/index.tsx",
        f"{base}/index.js",
        # Java / Kotlin / Scala (dot-to-slash already done above)
        f"{base}.java",
        f"{base}.kt",
        f"{base}.scala",
        # Go: resolve last path segment to a .go file of the same name
        f"{go_pkg}.go",
        # Rust: :: separator mapped to /; also try under src/
        f"{rust_base}.rs",
        f"{rust_base}/mod.rs",
        f"src/{rust_base}.rs",
        f"src/{rust_base}/mod.rs",
        # C#
        f"{base}.cs",
        # Ruby
        f"{base}.rb",
        # PHP
        f"{base}.php",
    )
    for suffix in (*_lang_suffixes(lang, base, rust_base, go_pkg), *fallback):
        file_id = suffix_map.get(suffix)
        if file_id is not None:
            return file_id
    return None
