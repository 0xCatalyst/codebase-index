"""JS/TS name binding: which import a called or referenced name comes from.

Tree-sitter sees `foo()` but not which `foo`. In an ES module a bare name is one
of three things: imported (`import { foo } from './x'`), defined in this file, or
a global (`setTimeout`, `Date`). Without that distinction the global graph pass
bound a bare name to the repository's only definition of it, so every `Date.now()`
became a call to the one `now` helper in the repo and every `useCallback(...)` a
call to a test stub. This pass records on each edge the import its name came from
(`Edge.module` / `Edge.export`), so the graph pass resolves it exactly — module
file plus exported name — and leaves globals unresolved.

It also adds the uses that are not calls, which in a React code base carry most
of the dependency graph: JSX elements (`<Foo />`), type references (`x: Foo`) and
imported names passed as values (`items.some(isReady)`, `queryKeys.market(id)`).
They are 'reference' edges, so `refs` and `impact` see them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .base import Edge, Symbol

JS_LANGS = frozenset({"typescript", "javascript"})

# Edge.module sentinels for a bare name that no import binds.
# UNBOUND: an ES module — the name is defined in this file or is a global, so it is
# never matched against another file's definitions.
UNBOUND = ""
# SCRIPT: a classic script (no import/export) — the name may be a global defined in
# another script, so the graph pass may still match it by repo-unique name.
SCRIPT = "*"


@dataclass(frozen=True)
class Binding:
    module: str  # the import specifier as written ('./x', '@/lib/kv', 'react')
    export: str  # the name the module exports it under; 'default', or '*' for a namespace


@dataclass
class FileBindings:
    names: dict[str, Binding]
    is_module: bool

    def bare(self) -> str:
        return UNBOUND if self.is_module else SCRIPT


def file_bindings(root, source: bytes) -> FileBindings:
    """Top-level import bindings of one file, and whether it is an ES module."""
    names: dict[str, Binding] = {}
    is_module = False
    for stmt in _named_children(root):
        kind = stmt.type
        if kind == "export_statement":
            is_module = True
            continue
        if kind != "import_statement":
            continue
        is_module = True
        module = _string_value(stmt.child_by_field_name("source"), source)
        if module is None:
            continue
        for clause in _named_children(stmt):
            if clause.type != "import_clause":
                continue
            for part in _named_children(clause):
                if part.type == "identifier":
                    names[_text(part, source)] = Binding(module, "default")
                elif part.type == "namespace_import":
                    ident = next((c for c in _named_children(part) if c.type == "identifier"), None)
                    if ident is not None:
                        names[_text(ident, source)] = Binding(module, "*")
                elif part.type == "named_imports":
                    for spec in _named_children(part):
                        if spec.type != "import_specifier":
                            continue
                        name = spec.child_by_field_name("name")
                        alias = spec.child_by_field_name("alias")
                        if name is None:
                            continue
                        local = alias if alias is not None else name
                        names[_text(local, source)] = Binding(module, _text(name, source))
    return FileBindings(names=names, is_module=is_module)


def bind_heritage(edges: list[Edge], bindings: FileBindings) -> None:
    """Give `class A extends B` / `implements I` edges the import `B`/`I` came from."""
    for edge in edges:
        if edge.edge_type not in ("extends", "implements"):
            continue
        binding = bindings.names.get(edge.callee_name)
        if binding is None:
            edge.module, edge.export = bindings.bare(), None
        else:
            edge.module, edge.export = binding.module, binding.export


# Methods of JS built-ins (Array, String, Map/Set, Promise, Object, Date, Math, JSON,
# console, DOM, streams). A member call by one of these names on a value the index
# cannot type is almost always the built-in, so it is not matched to the repo's
# only method of that name (`items.push(x)` is not `Separator.push`).
BUILTIN_MEMBERS = frozenset({
    # Array
    "at", "concat", "copyWithin", "entries", "every", "fill", "filter", "find",
    "findIndex", "findLast", "findLastIndex", "flat", "flatMap", "forEach", "from",
    "includes", "indexOf", "isArray", "join", "keys", "lastIndexOf", "map", "of", "pop",
    "push", "reduce", "reduceRight", "reverse", "shift", "slice", "some", "sort",
    "splice", "toReversed", "toSorted", "toSpliced", "unshift", "values", "with",
    # String / RegExp
    "charAt", "charCodeAt", "codePointAt", "endsWith", "exec", "localeCompare",
    "match", "matchAll", "normalize", "padEnd", "padStart", "repeat", "replace",
    "replaceAll", "search", "split", "startsWith", "substr", "substring", "test",
    "toLocaleLowerCase", "toLocaleUpperCase", "toLowerCase", "toUpperCase", "trim",
    "trimEnd", "trimStart",
    # Map / Set / WeakMap / Headers / URLSearchParams / FormData / storage
    "add", "append", "clear", "delete", "get", "getAll", "getItem", "has", "removeItem",
    "set", "setItem",
    # Promise / iterators / streams
    "all", "allSettled", "any", "catch", "finally", "next", "race", "reject", "resolve",
    "return", "then", "throw", "pipe", "pipeTo", "pipeThrough", "getReader", "getWriter",
    "read", "write", "close", "cancel", "abort", "enqueue",
    # Object / Function
    "apply", "assign", "bind", "call", "create", "defineProperty", "freeze",
    "fromEntries", "getOwnPropertyNames", "getPrototypeOf", "hasOwn", "hasOwnProperty",
    "isPrototypeOf", "toJSON", "toLocaleString", "toString", "valueOf",
    # Number / Math / JSON / Date
    "abs", "ceil", "floor", "isFinite", "isInteger", "isNaN", "isSafeInteger", "log",
    "log10", "log2", "max", "min", "parse", "parseFloat", "parseInt", "pow", "random",
    "round", "sign", "sqrt", "stringify", "toFixed", "toPrecision", "trunc", "now",
    "getDate", "getDay", "getFullYear", "getHours", "getMinutes", "getMonth",
    "getSeconds", "getTime", "getUTCDate", "getUTCFullYear", "getUTCHours",
    "getUTCMonth", "setDate", "setHours", "setTime", "toDateString", "toISOString",
    "toLocaleDateString", "toLocaleTimeString", "toUTCString",
    # console
    "assert", "count", "debug", "dir", "error", "group", "groupCollapsed", "groupEnd",
    "info", "table", "time", "timeEnd", "trace", "warn",
    # DOM / events / fetch
    "addEventListener", "appendChild", "blob", "blur", "click", "clone", "closest",
    "contains", "dispatchEvent", "focus", "getAttribute", "getBoundingClientRect",
    "getElementById", "json", "preventDefault", "querySelector", "querySelectorAll",
    "remove", "removeChild", "removeEventListener", "scrollIntoView", "scrollTo",
    "select", "setAttribute", "stopPropagation", "text", "arrayBuffer", "emit", "on",
    "off", "once",
})


def call_binding(
    fn, source: bytes, bindings: FileBindings
) -> tuple[Optional[str], Optional[str]]:
    """(module, export) for the callee node `fn` of a JS/TS call.

    `foo()` with `foo` imported -> its import; `foo()` otherwise -> UNBOUND (ES
    module) or SCRIPT. `ns.run()` on a namespace import -> (module, 'run');
    `kv.get()` on an imported object -> (module, 'kv.get'). Any other member call
    -> (None, None): the receiver is a value the index cannot type.
    """
    if fn is None:
        return None, None
    if fn.type == "identifier":
        binding = bindings.names.get(_text(fn, source))
        if binding is None:
            return bindings.bare(), None
        return binding.module, binding.export
    if fn.type == "member_expression":
        obj = fn.child_by_field_name("object")
        prop = fn.child_by_field_name("property")
        if obj is None or prop is None or obj.type != "identifier":
            return None, None
        binding = bindings.names.get(_text(obj, source))
        if binding is None:
            return None, None
        member = _text(prop, source)
        if binding.export == "*":
            return binding.module, member
        return binding.module, f"{binding.export}.{member}"
    return None, None


# Identifier positions that declare a name rather than use one.
_DECLARING_PARENTS = frozenset({
    "variable_declarator", "function_declaration", "generator_function_declaration",
    "function_expression", "class_declaration", "class", "method_definition",
    "required_parameter", "optional_parameter", "formal_parameters", "rest_pattern",
    "assignment_pattern", "object_pattern", "array_pattern", "pair_pattern",
    "catch_clause", "import_specifier", "import_clause", "namespace_import",
    "interface_declaration", "type_alias_declaration", "enum_declaration",
    "type_parameter", "labeled_statement", "abstract_class_declaration",
})


def reference_edges(
    root, source: bytes, symbols: list[Symbol], bindings: FileBindings, enclosing
) -> list[Edge]:
    """Non-call uses: imported names used as values or types, and JSX elements.

    A JSX element names a component even when it is defined in this file, so it is
    recorded with the UNBOUND/SCRIPT sentinel when no import binds it; the pipeline
    then resolves it to the same-file definition. Another unbound identifier counts
    only when it names a module-level definition and the file declares that name
    nowhere else — then no local variable can shadow it.
    `enclosing(symbols, line)` picks the calling symbol, as for call edges.
    """
    edges: list[Edge] = []
    seen: set[tuple[str, int, Optional[str]]] = set()
    declared = _declaration_counts(root, source)
    module_level = {
        s.name for s in symbols
        if s.parent_index is None and s.kind != "method" and declared.get(s.name) == 1
    }

    def add(name: str, line: int, module: Optional[str], export: Optional[str]) -> None:
        key = (name, line, module)
        if key in seen:
            return
        seen.add(key)
        edges.append(Edge(
            edge_type="reference",
            callee_name=name,
            line=line,
            src_symbol_index=enclosing(symbols, line),
            module=module,
            export=export,
        ))

    stack = list(_named_children(root))
    while stack:
        node = stack.pop()
        kind = node.type
        if kind == "import_statement":
            continue
        if kind == "export_statement" and node.child_by_field_name("source") is not None:
            _reexport_edges(node, source, add)
            continue
        if kind in ("jsx_opening_element", "jsx_self_closing_element"):
            name_node = node.child_by_field_name("name")
            head = _jsx_head(name_node)
            if head is not None:
                text = _text(head, source)
                if text[:1].isupper() or head is not name_node:
                    binding = bindings.names.get(text)
                    line = head.start_point[0] + 1
                    if binding is not None:
                        add(_bound_name(text, binding), line, binding.module, binding.export)
                    elif text[:1].isupper():
                        add(text, line, bindings.bare(), None)
            # Attributes and children still hold expressions to scan.
            stack.extend(c for c in _named_children(node) if c is not name_node)
            continue
        if kind in ("identifier", "type_identifier", "shorthand_property_identifier"):
            text = _text(node, source)
            binding = bindings.names.get(text)
            if binding is not None:
                if binding.export != "*" and _is_use(node):
                    add(_bound_name(text, binding), node.start_point[0] + 1,
                        binding.module, binding.export)
            elif text in module_level and _is_use(node):
                add(text, node.start_point[0] + 1, bindings.bare(), None)
            continue
        stack.extend(_named_children(node))
    return edges


def _declaration_counts(root, source: bytes) -> dict[str, int]:
    """How many times each name is declared anywhere in the file (any scope)."""
    counts: dict[str, int] = {}
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in _DECLARABLE and _declares(node):
            name = _text(node, source)
            counts[name] = counts.get(name, 0) + 1
        stack.extend(_named_children(node))
    return counts


_DECLARABLE = frozenset({"identifier", "type_identifier", "shorthand_property_identifier_pattern"})
_NAMED_DECLARATIONS = frozenset({
    "variable_declarator", "function_declaration", "generator_function_declaration",
    "function_expression", "class_declaration", "class", "abstract_class_declaration",
    "interface_declaration", "type_alias_declaration", "enum_declaration", "type_parameter",
})


def _declares(node) -> bool:
    if node.type == "shorthand_property_identifier_pattern":
        return True
    parent = node.parent
    if parent is None:
        return False
    ptype = parent.type
    if ptype in _NAMED_DECLARATIONS:
        return parent.child_by_field_name("name") == node
    if ptype in ("required_parameter", "optional_parameter"):
        return parent.child_by_field_name("pattern") == node
    if ptype in ("formal_parameters", "rest_pattern", "array_pattern", "object_pattern",
                 "import_specifier", "import_clause", "namespace_import"):
        return True
    field = {
        "pair_pattern": "value", "assignment_pattern": "left", "arrow_function": "parameter",
        "catch_clause": "parameter", "for_in_statement": "left",
    }.get(ptype)
    return field is not None and parent.child_by_field_name(field) == node


def _reexport_edges(stmt, source: bytes, add) -> None:
    """`export { a, b as c } from './x'` uses `a` and `b` of './x'."""
    module = _string_value(stmt.child_by_field_name("source"), source)
    if module is None:
        return
    for clause in _named_children(stmt):
        if clause.type != "export_clause":
            continue
        for spec in _named_children(clause):
            name = spec.child_by_field_name("name") if spec.type == "export_specifier" else None
            if name is not None:
                text = _text(name, source)
                add(text, name.start_point[0] + 1, module, text)


def _bound_name(local: str, binding: Binding) -> str:
    """Record a named import under its exported name, so `refs isReady` also finds
    `import { isReady as ready }` uses; default imports keep the local name."""
    return local if binding.export in ("default", "*") else binding.export


def _jsx_head(name_node):
    """The identifier a JSX element name starts with: `Foo` in `<Foo.Bar>`."""
    node = name_node
    while node is not None and node.type in ("member_expression", "nested_identifier"):
        node = node.child_by_field_name("object") or (
            _named_children(node)[0] if _named_children(node) else None
        )
    return node if node is not None and node.type == "identifier" else None


def _is_use(node) -> bool:
    parent = node.parent
    if parent is None:
        return False
    ptype = parent.type
    if ptype in _DECLARING_PARENTS:
        # `const x = foo` uses foo (value) but declares x (name).
        return parent.child_by_field_name("name") != node and ptype not in (
            "formal_parameters", "import_specifier", "import_clause", "namespace_import",
            "object_pattern", "array_pattern", "rest_pattern", "pair_pattern",
            "type_parameter", "catch_clause",
        ) and not (
            ptype in ("required_parameter", "optional_parameter")
            and parent.child_by_field_name("pattern") == node
        ) and not (
            ptype == "assignment_pattern" and parent.child_by_field_name("left") == node
        )
    if ptype == "call_expression" and parent.child_by_field_name("function") == node:
        return False  # a call: already a 'call' edge
    if ptype in ("export_specifier",):
        return True  # `export { foo }` re-exports the binding
    return True


def _string_value(node, source: bytes) -> Optional[str]:
    if node is None:
        return None
    fragment = next((c for c in _named_children(node) if c.type == "string_fragment"), None)
    if fragment is not None:
        return _text(fragment, source)
    raw = _text(node, source).strip()
    return raw[1:-1] if len(raw) >= 2 and raw[0] in "'\"`" else None


def _text(node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="ignore")


def _named_children(node) -> list:
    return list(node.named_children)
