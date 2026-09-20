"""AST-based Python dependency graph construction.

Walks a repository checkout, parses every ``*.py`` file with the stdlib
``ast`` module, and builds a :class:`DependencyGraph` of import edges
between modules — absolute imports (``ast.Import``), ``from`` imports
(``ast.ImportFrom``, level 0), and relative imports (``ast.ImportFrom``,
level > 0), with best-effort resolution of package-level re-exports
(``from pkg import thing`` where ``thing`` is defined in ``pkg/__init__.py``
rather than being a submodule).

The graph is intentionally free of any database/broker dependency so it
can be reused as-is by a future reviewer agent (see the Phase 2 roadmap).
"""

from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "ModuleInfo",
    "ImportEdge",
    "DependencyGraph",
    "build_dependency_graph",
]

_IGNORED_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
}


@dataclass(frozen=True)
class ModuleInfo:
    """One Python module discovered in the repository."""

    name: str
    path: str
    is_package: bool


@dataclass(frozen=True)
class ImportEdge:
    """One import statement, resolved as best as possible against the
    repository's own modules."""

    importer: str
    imported: str
    import_type: str  # "import" | "from" | "relative"
    line_number: int | None
    is_external: bool


@dataclass
class DependencyGraph:
    """Import graph for one repository checkout.

    ``forward``/``reverse`` adjacency only track *internal* edges (modules
    resolved to a file in this repository) — external/stdlib/third-party
    imports are recorded on ``edges`` (for storage/auditing) but excluded
    from traversal, since blast-radius analysis only cares about modules
    this repo can actually break.
    """

    modules: dict[str, ModuleInfo] = field(default_factory=dict)
    paths: dict[str, str] = field(default_factory=dict)  # path -> module name
    edges: list[ImportEdge] = field(default_factory=list)
    _forward: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    _reverse: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))

    def add_module(self, info: ModuleInfo) -> None:
        self.modules[info.name] = info
        self.paths[info.path] = info.name

    def add_edge(self, edge: ImportEdge) -> None:
        self.edges.append(edge)
        if not edge.is_external:
            self._forward[edge.importer].add(edge.imported)
            self._reverse[edge.imported].add(edge.importer)

    def dependencies_of(self, module_name: str) -> set[str]:
        """Modules that ``module_name`` imports (internal only)."""
        return set(self._forward.get(module_name, ()))

    def dependents_of(self, module_name: str) -> set[str]:
        """Modules that import ``module_name`` (internal only)."""
        return set(self._reverse.get(module_name, ()))

    def module_for_path(self, path: str) -> str | None:
        """Resolve a repo-relative file path to its module name, if any."""
        return self.paths.get(path.replace("\\", "/"))


def _module_name_for(rel_path: Path) -> tuple[str, bool]:
    """Convert a repo-relative ``.py`` path to a dotted module name.

    ``pkg/sub/mod.py`` -> ``("pkg.sub.mod", False)``
    ``pkg/sub/__init__.py`` -> ``("pkg.sub", True)`` — the package's own
    name, since ``__init__.py`` *is* the package for import-resolution
    purposes.
    """
    parts = list(rel_path.with_suffix("").parts)
    is_package = parts[-1] == "__init__"
    if is_package:
        parts = parts[:-1]
    return ".".join(parts), is_package


def _containing_package(module_name: str, is_package: bool) -> str:
    if is_package:
        return module_name
    if "." in module_name:
        return module_name.rsplit(".", 1)[0]
    return ""


def _pop_package(package: str) -> str:
    if "." in package:
        return package.rsplit(".", 1)[0]
    return ""


def _resolve_absolute(target: str, graph: DependencyGraph) -> tuple[str, bool]:
    """Resolve ``import target`` (dotted, level 0)."""
    if target in graph.modules:
        return target, False
    return target, True


def _resolve_from_target(base: str, name: str, graph: DependencyGraph) -> tuple[str, bool]:
    """Resolve one alias of ``from base import name``.

    Tries the submodule first (``base.name``), then falls back to ``base``
    itself — handling the common case where ``name`` is a symbol
    re-exported from ``base/__init__.py`` rather than a submodule.
    """
    candidate = f"{base}.{name}" if base else name
    if candidate in graph.modules:
        return candidate, False
    if base and base in graph.modules:
        return base, False
    return candidate, True


def _resolve_relative_base(current_package: str, level: int) -> str:
    package = current_package
    for _ in range(level - 1):
        package = _pop_package(package)
    return package


def _iter_python_files(repo_root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(repo_root.rglob("*.py")):
        if any(part in _IGNORED_DIRS for part in path.relative_to(repo_root).parts):
            continue
        files.append(path)
    return files


def build_dependency_graph(repo_root: Path) -> DependencyGraph:
    """Build the full import graph for every ``*.py`` file under ``repo_root``.

    Files that fail to parse (syntax errors) are skipped rather than
    aborting the whole analysis — a single broken file shouldn't prevent
    blast-radius analysis for the rest of the repository.
    """
    repo_root = Path(repo_root)
    graph = DependencyGraph()
    py_files = _iter_python_files(repo_root)

    for file in py_files:
        rel = file.relative_to(repo_root)
        name, is_package = _module_name_for(rel)
        graph.add_module(
            ModuleInfo(name=name, path=str(rel).replace("\\", "/"), is_package=is_package)
        )

    for file in py_files:
        rel = file.relative_to(repo_root)
        importer_name, is_package = _module_name_for(rel)
        current_package = _containing_package(importer_name, is_package)

        try:
            source = file.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(file))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    resolved, is_external = _resolve_absolute(alias.name, graph)
                    graph.add_edge(
                        ImportEdge(importer_name, resolved, "import", node.lineno, is_external)
                    )
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    base = _resolve_relative_base(current_package, node.level)
                    if node.module:
                        base = f"{base}.{node.module}" if base else node.module
                    for alias in node.names:
                        resolved, is_external = _resolve_from_target(base, alias.name, graph)
                        graph.add_edge(
                            ImportEdge(
                                importer_name, resolved, "relative", node.lineno, is_external
                            )
                        )
                else:
                    module = node.module or ""
                    for alias in node.names:
                        resolved, is_external = _resolve_from_target(module, alias.name, graph)
                        graph.add_edge(
                            ImportEdge(importer_name, resolved, "from", node.lineno, is_external)
                        )

    return graph
