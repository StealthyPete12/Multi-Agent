from pathlib import Path

from agents.researcher.graph import build_dependency_graph
from agents.researcher.impact import changed_files_to_modules, compute_blast_radius


def _write(root: Path, rel_path: str, content: str) -> None:
    file_path = root / rel_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content)


def _chain_repo(tmp_path: Path) -> Path:
    """database.py <- auth.py <- payments.py <- checkout.py (roadmap example)."""
    _write(tmp_path, "database.py", "")
    _write(tmp_path, "auth.py", "import database\n")
    _write(tmp_path, "payments.py", "import auth\n")
    _write(tmp_path, "checkout.py", "import payments\n")
    return tmp_path


def test_direct_dependents_only_at_depth_one(tmp_path):
    _chain_repo(tmp_path)
    graph = build_dependency_graph(tmp_path)

    result = compute_blast_radius(graph, ["database"], max_depth=1)

    assert result.impacted_modules == ["auth", "database"]
    assert result.impact_count == 2


def test_transitive_dependents_full_chain(tmp_path):
    """The roadmap's worked example: changing database.py ripples through
    auth -> payments -> checkout. Impact count = 4."""
    _chain_repo(tmp_path)
    graph = build_dependency_graph(tmp_path)

    result = compute_blast_radius(graph, ["database"], max_depth=10)

    assert result.impacted_modules == ["auth", "checkout", "database", "payments"]
    assert result.impact_count == 4
    assert result.max_depth_reached == 3
    assert result.chains == {"database": 0, "auth": 1, "payments": 2, "checkout": 3}


def test_depth_limit_truncates_traversal(tmp_path):
    _chain_repo(tmp_path)
    graph = build_dependency_graph(tmp_path)

    result = compute_blast_radius(graph, ["database"], max_depth=2)

    assert result.impacted_modules == ["auth", "database", "payments"]
    assert result.impact_count == 3
    assert result.max_depth_reached == 2


def test_zero_depth_returns_only_changed_modules(tmp_path):
    _chain_repo(tmp_path)
    graph = build_dependency_graph(tmp_path)

    result = compute_blast_radius(graph, ["database"], max_depth=0)

    assert result.impacted_modules == ["database"]
    assert result.impact_count == 1


def test_unchanged_leaf_module_has_no_impact(tmp_path):
    _chain_repo(tmp_path)
    graph = build_dependency_graph(tmp_path)

    result = compute_blast_radius(graph, ["checkout"], max_depth=10)

    assert result.impacted_modules == ["checkout"]
    assert result.impact_count == 1


def test_cycle_does_not_cause_infinite_traversal(tmp_path):
    _write(tmp_path, "a.py", "import b\n")
    _write(tmp_path, "b.py", "import c\n")
    _write(tmp_path, "c.py", "import a\n")
    graph = build_dependency_graph(tmp_path)

    result = compute_blast_radius(graph, ["a"], max_depth=50)

    assert result.impacted_modules == ["a", "b", "c"]
    assert result.impact_count == 3


def test_changed_files_to_modules_maps_paths_and_skips_unknown(tmp_path):
    _chain_repo(tmp_path)
    graph = build_dependency_graph(tmp_path)

    modules = changed_files_to_modules(
        graph, ["database.py", "not_in_repo.py", "README.md"]
    )

    assert modules == ["database"]


def test_multiple_changed_modules_union_their_blast_radii(tmp_path):
    _chain_repo(tmp_path)
    _write(tmp_path, "unrelated.py", "")
    graph = build_dependency_graph(tmp_path)

    result = compute_blast_radius(graph, ["database", "unrelated"], max_depth=10)

    assert result.impacted_modules == ["auth", "checkout", "database", "payments", "unrelated"]
