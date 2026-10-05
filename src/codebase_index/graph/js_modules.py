"""Resolve JS/TS import specifiers to indexed files the way the TypeScript compiler does.

`./x` is relative to the importing file; anything else goes through the nearest
tsconfig.json / jsconfig.json (`compilerOptions.paths`, then `baseUrl`), else it is
a package and stays unresolved: guessing it onto a same-named repo file (`sonner`
-> `components/ui/sonner.tsx`) is how dependency graphs go wrong. Candidates are
probed with TypeScript's extension and `index` rules. The result is an exact path,
not a suffix match.
"""

from __future__ import annotations

import json
import posixpath
from pathlib import Path
from typing import Optional

_CONFIG_NAMES = ("tsconfig.json", "jsconfig.json")
_EXTENSIONS = (".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs", ".json")
_INDEX_FILES = tuple(f"/index{ext}" for ext in (".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mjs", ".cjs"))
# ESM-style TypeScript imports name the emitted file: `./x.js` means `./x.ts`.
_EMITTED_TO_SOURCE = {
    ".js": (".ts", ".tsx"), ".jsx": (".tsx",), ".mjs": (".mts",), ".cjs": (".cts",),
}
_MAX_EXTENDS = 8


class _PathConfig:
    """The `baseUrl` / `paths` that apply to one directory (repo-relative dirs)."""

    __slots__ = ("base_url", "paths_base", "patterns")

    def __init__(
        self,
        base_url: Optional[str],
        paths_base: str,
        patterns: list[tuple[str, Optional[str], list[str]]],
    ) -> None:
        self.base_url = base_url
        self.paths_base = paths_base
        # (prefix, suffix, targets), longest prefix first — the compiler's tie-break.
        self.patterns = sorted(patterns, key=lambda p: len(p[0]), reverse=True)


class JsModuleResolver:
    def __init__(self, root: Optional[Path], paths: dict[str, int]) -> None:
        self._root = Path(root).resolve() if root is not None else None
        self._files = paths
        self._lower: dict[str, Optional[int]] = {}
        for path, file_id in paths.items():
            # Case-insensitive fallback for imports written with another case
            # (they resolve on case-insensitive filesystems); a clash disables it.
            key = path.lower()
            self._lower[key] = None if key in self._lower else file_id
        self._dir_config: dict[str, Optional[_PathConfig]] = {}
        self._file_config: dict[Path, Optional[dict]] = {}

    def resolve(self, specifier: str, importer: str) -> Optional[int]:
        """File id for `specifier` imported from repo-relative `importer`, or None."""
        spec = specifier.split("?", 1)[0].split("#", 1)[0] if specifier[:1] != "#" else specifier
        if not spec:
            return None
        importer_dir = posixpath.dirname(importer)
        if spec.startswith(("./", "../")) or spec in (".", ".."):
            return self._probe(posixpath.join(importer_dir, spec))
        if spec.startswith("/"):
            return None
        config = self._config_for(importer_dir)
        if config is None:
            return None
        for prefix, suffix, targets in config.patterns:
            if suffix is None:
                if spec != prefix:
                    continue
                star = ""
            elif spec.startswith(prefix) and spec.endswith(suffix) and len(spec) >= len(prefix) + len(suffix):
                star = spec[len(prefix):len(spec) - len(suffix)]
            else:
                continue
            for target in targets:
                hit = self._probe(posixpath.join(config.paths_base, target.replace("*", star, 1)))
                if hit is not None:
                    return hit
            return None  # a matching pattern that finds nothing does not fall through
        if config.base_url is not None:
            return self._probe(posixpath.join(config.base_url, spec))
        return None

    # -- probing -------------------------------------------------------------------------

    def _probe(self, candidate: str) -> Optional[int]:
        path = posixpath.normpath(candidate)
        if path == "." or path.startswith("../") or path == "..":
            return None
        path = path[2:] if path.startswith("./") else path
        tries = [path]
        base, ext = posixpath.splitext(path)
        for source_ext in _EMITTED_TO_SOURCE.get(ext, ()):
            tries.append(base + source_ext)
        tries.extend(path + e for e in _EXTENSIONS)
        tries.extend(path + i for i in _INDEX_FILES)
        for item in tries:
            hit = self._files.get(item)
            if hit is not None:
                return hit
        for item in tries:
            hit = self._lower.get(item.lower())
            if hit is not None:
                return hit
        return None

    # -- tsconfig ------------------------------------------------------------------------

    def _config_for(self, rel_dir: str) -> Optional[_PathConfig]:
        """The path config of the nearest tsconfig/jsconfig at or above `rel_dir`."""
        if self._root is None:
            return None
        if rel_dir in self._dir_config:
            return self._dir_config[rel_dir]
        found: Optional[_PathConfig] = None
        for name in _CONFIG_NAMES:
            cfg_file = self._root / rel_dir / name if rel_dir else self._root / name
            if cfg_file.is_file():
                found = self._path_config(cfg_file)
                break
        else:
            if rel_dir:
                found = self._config_for(posixpath.dirname(rel_dir))
        self._dir_config[rel_dir] = found
        return found

    def _path_config(self, cfg_file: Path) -> Optional[_PathConfig]:
        options = self._compiler_options(cfg_file, depth=0)
        if options is None:
            return None
        base_url = options.get("baseUrl")
        paths = options.get("paths")
        base_dir = self._rel(options.get("__baseUrl_dir__"), base_url) if base_url else None
        # `paths` resolve against baseUrl when set, else against the config declaring them.
        if base_dir is not None:
            paths_base = base_dir
        else:
            paths_base = self._rel(options.get("__paths_dir__"), ".") or ""
        patterns: list[tuple[str, Optional[str], list[str]]] = []
        if isinstance(paths, dict):
            for key, targets in paths.items():
                if not isinstance(key, str) or not isinstance(targets, list):
                    continue
                targets = [t for t in targets if isinstance(t, str)]
                if "*" in key:
                    prefix, suffix = key.split("*", 1)
                    patterns.append((prefix, suffix, targets))
                else:
                    patterns.append((key, None, targets))
        if base_dir is None and not patterns:
            return None
        return _PathConfig(base_dir, paths_base, patterns)

    def _rel(self, cfg_dir: Optional[Path], value: str) -> Optional[str]:
        if cfg_dir is None or self._root is None:
            return None
        absolute = (cfg_dir / value).resolve()
        try:
            rel = absolute.relative_to(self._root).as_posix()
        except ValueError:
            return None
        return "" if rel == "." else rel

    def _compiler_options(self, cfg_file: Path, depth: int) -> Optional[dict]:
        """compilerOptions after `extends`, remembering which file set baseUrl/paths."""
        if depth > _MAX_EXTENDS:
            return None
        data = self._load(cfg_file)
        if data is None:
            return None
        merged: dict = {}
        parents = data.get("extends")
        for parent in ([parents] if isinstance(parents, str) else parents or []):
            if not isinstance(parent, str):
                continue
            parent_file = self._extends_file(cfg_file.parent, parent)
            if parent_file is not None:
                inherited = self._compiler_options(parent_file, depth + 1)
                if inherited:
                    merged.update(inherited)
        own = data.get("compilerOptions")
        if isinstance(own, dict):
            merged.update(own)
            if "baseUrl" in own:
                merged["__baseUrl_dir__"] = cfg_file.parent
            if "paths" in own:
                merged["__paths_dir__"] = cfg_file.parent
        return merged

    def _extends_file(self, cfg_dir: Path, ref: str) -> Optional[Path]:
        if ref.startswith(("./", "../", "/")):
            candidates = [cfg_dir / ref]
        else:
            candidates = [d / "node_modules" / ref for d in (cfg_dir, *cfg_dir.parents)]
        for cand in candidates:
            for item in (cand, Path(f"{cand}.json"), cand / "tsconfig.json"):
                if item.is_file():
                    return item
        return None

    def _load(self, cfg_file: Path) -> Optional[dict]:
        if cfg_file in self._file_config:
            return self._file_config[cfg_file]
        try:
            data = json.loads(strip_jsonc(cfg_file.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            data = None
        result = data if isinstance(data, dict) else None
        self._file_config[cfg_file] = result
        return result


def strip_jsonc(text: str) -> str:
    """JSON with comments and trailing commas (tsconfig's dialect) -> plain JSON."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        elif ch == ",":
            j = i + 1
            while j < n:
                if text[j].isspace():
                    j += 1
                elif text.startswith("//", j):
                    while j < n and text[j] != "\n":
                        j += 1
                elif text.startswith("/*", j):
                    end = text.find("*/", j + 2)
                    j = n if end < 0 else end + 2
                else:
                    break
            if j >= n or text[j] not in "}]":
                out.append(ch)
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)
