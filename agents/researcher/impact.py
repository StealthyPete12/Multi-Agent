"""Blast-radius (dependency-impact) analysis over a :class:`DependencyGraph`.

Given a set of changed files, determines every module that transitively
depends on them by walking the graph's reverse edges (who imports whom),
breadth-first, up to a configurable depth. This is the in-memory
counterpart to ``agents/researcher/db.py``'s recursive-CTE version — kept
dependency-free (no Postgres) so the traversal algorithm itself is
directly unit-testable, and reusable by a future reviewer agent that only
has the graph, not a live database connection.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from agents.researcher.graph import DependencyGraph

__all__ = [
    "BlastRadiusResult",
    "DEFAULT_MAX_DEPTH",
    "changed_files_to_modules",
    "compute_blast_radius",
]

DEFAULT_MAX_DEPTH = int(os.environ.get("BLAST_RADIUS_MAX_DEPTH", 10))


@dataclass(frozen=True)
class BlastRadiusResult:
    """Result of a blast-radius traversal.

    ``chains`` maps each impacted module to the depth at which it was
    first reached (0 = one of the changed modules themselves).
    """

    impacted_modules: list[str]
    impact_count: int
    max_depth_reached: int
    chains: dict[str, int] = field(default_factory=dict)


def changed_files_to_modules(graph: DependencyGraph, changed_files: list[str]) -> list[str]:
    """Map changed file paths to module names, dropping files that aren't
    Python or aren't part of this graph (e.g. non-.py changes)."""
    modules = []
    for changed_file in changed_files:
        module = graph.module_for_path(changed_file)
        if module is not None:
            modules.append(module)
    return modules


def compute_blast_radius(
    graph: DependencyGraph,
    changed_modules: list[str],
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> BlastRadiusResult:
    """Breadth-first traversal of dependents, bounded by ``max_depth``.

    Cycle-safe: a ``visited`` map guarantees each module is only expanded
    once, regardless of how many cycles exist in the graph.
    """
    visited: dict[str, int] = {}
    frontier = {m for m in changed_modules if m}
    for module in frontier:
        visited[module] = 0

    depth = 0
    while frontier and depth < max_depth:
        next_frontier: set[str] = set()
        for module in frontier:
            for dependent in graph.dependents_of(module):
                if dependent not in visited:
                    visited[dependent] = depth + 1
                    next_frontier.add(dependent)
        frontier = next_frontier
        depth += 1

    impacted = sorted(visited)
    max_depth_reached = max(visited.values()) if visited else 0
    return BlastRadiusResult(
        impacted_modules=impacted,
        impact_count=len(impacted),
        max_depth_reached=max_depth_reached,
        chains=visited,
    )
