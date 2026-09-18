from pathlib import Path

from agents.researcher.graph import build_dependency_graph


def _write(root: Path, rel_path: str, content: str) -> None:
    file_path = root / rel_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content)


def test_absolute_import_resolved_within_repo(tmp_path):
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/database.py", "")
    _write(tmp_path, "pkg/auth.py", "import pkg.database\n")

    graph = build_dependency_graph(tmp_path)

    assert graph.dependencies_of("pkg.auth") == {"pkg.database"}
    assert graph.dependents_of("pkg.database") == {"pkg.auth"}


def test_from_import_submodule(tmp_path):
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/database.py", "")
    _write(tmp_path, "pkg/auth.py", "from pkg import database\n")

    graph = build_dependency_graph(tmp_path)

    assert graph.dependencies_of("pkg.auth") == {"pkg.database"}


def test_from_import_of_external_package_marked_external(tmp_path):
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/auth.py", "import os\nfrom collections import OrderedDict\n")

    graph = build_dependency_graph(tmp_path)

    assert graph.dependencies_of("pkg.auth") == set()
    external_targets = {e.imported for e in graph.edges if e.is_external}
    assert "os" in external_targets
    assert "collections.OrderedDict" in external_targets or "collections" in external_targets


def test_relative_import_from_sibling_module(tmp_path):
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/database.py", "")
    _write(tmp_path, "pkg/auth.py", "from . import database\n")

    graph = build_dependency_graph(tmp_path)

    assert graph.dependencies_of("pkg.auth") == {"pkg.database"}


def test_relative_import_dotted_module(tmp_path):
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/database.py", "")
    _write(tmp_path, "pkg/auth.py", "from .database import connect\n")

    graph = build_dependency_graph(tmp_path)

    assert graph.dependencies_of("pkg.auth") == {"pkg.database"}


def test_relative_import_two_levels_up(tmp_path):
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/database.py", "")
    _write(tmp_path, "pkg/sub/__init__.py", "")
    _write(tmp_path, "pkg/sub/service.py", "from .. import database\n")

    graph = build_dependency_graph(tmp_path)

    assert graph.dependencies_of("pkg.sub.service") == {"pkg.database"}


def test_init_py_reexport_resolves_to_package(tmp_path):
    """`from pkg import helper` where `helper` is defined directly inside
    pkg/__init__.py (not a submodule) should resolve to the package
    itself, not dangle as external."""
    _write(tmp_path, "pkg/__init__.py", "def helper():\n    pass\n")
    _write(tmp_path, "app.py", "from pkg import helper\n")

    graph = build_dependency_graph(tmp_path)

    assert graph.dependencies_of("app") == {"pkg"}
    assert graph.modules["pkg"].is_package is True


def test_package_level_absolute_import(tmp_path):
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "app.py", "import pkg\n")

    graph = build_dependency_graph(tmp_path)

    assert graph.dependencies_of("app") == {"pkg"}


def test_import_cycle_does_not_crash_graph_construction(tmp_path):
    _write(tmp_path, "a.py", "import b\n")
    _write(tmp_path, "b.py", "import a\n")

    graph = build_dependency_graph(tmp_path)

    assert graph.dependencies_of("a") == {"b"}
    assert graph.dependencies_of("b") == {"a"}
    assert graph.dependents_of("a") == {"b"}
    assert graph.dependents_of("b") == {"a"}


def test_ignored_directories_are_skipped(tmp_path):
    _write(tmp_path, "app.py", "")
    _write(tmp_path, ".git/hooks/pre-commit.py", "import os\n")
    _write(tmp_path, "__pycache__/app.cpython-311.py", "")
    _write(tmp_path, ".venv/lib/site.py", "")

    graph = build_dependency_graph(tmp_path)

    assert set(graph.modules) == {"app"}


def test_syntax_error_file_is_skipped_not_fatal(tmp_path):
    _write(tmp_path, "broken.py", "def f(:\n")
    _write(tmp_path, "app.py", "import broken\n")

    graph = build_dependency_graph(tmp_path)

    # broken.py is still registered as a module (it exists as a file), but
    # since it fails to parse it contributes no import edges of its own —
    # the whole analysis doesn't abort because of one broken file.
    assert "broken" in graph.modules
    assert graph.dependencies_of("broken") == set()
    assert graph.dependencies_of("app") == {"broken"}


def test_module_for_path_maps_changed_file_to_module_name(tmp_path):
    _write(tmp_path, "pkg/database.py", "")

    graph = build_dependency_graph(tmp_path)

    assert graph.module_for_path("pkg/database.py") == "pkg.database"
    assert graph.module_for_path("pkg/nonexistent.py") is None
