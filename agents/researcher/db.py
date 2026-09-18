"""Postgres storage for the researcher's dependency graph, and the
recursive-CTE blast-radius query over stored modules/imports.

Reuses the existing ``modules``/``imports`` tables from
``db/migrations/001_init_schema.sql`` (Phase 0) — no schema changes.
Idempotency comes from:

- ``modules``: ``ON CONFLICT (repo, path) DO UPDATE`` (the table already
  has a ``UNIQUE (repo, path)`` constraint).
- ``imports``: delete-then-reinsert the full set of import rows for a
  module inside the same transaction as the module upsert, so re-running
  analysis for the same commit converges to the same rows instead of
  accumulating duplicates.
"""

from __future__ import annotations

import os
import time
from collections import defaultdict
from dataclasses import dataclass, field

import asyncpg
from opentelemetry.trace import SpanKind

from agents.researcher.graph import DependencyGraph
from shared import telemetry
from shared.logging import configure_logging

__all__ = ["Database", "BlastRadiusResult", "DEFAULT_DATABASE_URL"]

log = configure_logging(service_name="researcher")

DEFAULT_DATABASE_URL = "postgresql://swarm:swarm_dev_password@localhost:5432/code_review_swarm"


@dataclass(frozen=True)
class BlastRadiusResult:
    impacted_modules: list[str]
    impact_count: int
    max_depth_reached: int
    chains: dict[str, int] = field(default_factory=dict)


class Database:
    """Thin asyncpg pool wrapper, mirroring ``shared/broker.py::Broker``'s
    connect/close lifecycle so agents manage it the same way."""

    def __init__(self, dsn: str | None = None) -> None:
        self.dsn = dsn or os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        if self._pool is not None:
            return
        self._pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=5)
        telemetry.register_pool_gauges("researcher", lambda: self._pool)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Database.connect() must be awaited before use")
        return self._pool

    async def upsert_module(
        self, conn: asyncpg.Connection, repo: str, path: str, name: str, language: str | None
    ) -> str:
        """Duplicate-safe module upsert. Returns the module's UUID."""
        row = await conn.fetchrow(
            """
            INSERT INTO modules (repo, path, name, language)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (repo, path)
            DO UPDATE SET name = EXCLUDED.name, language = EXCLUDED.language, updated_at = now()
            RETURNING id
            """,
            repo,
            path,
            name,
            language,
        )
        return str(row["id"])

    async def replace_imports(
        self,
        conn: asyncpg.Connection,
        module_id: str,
        imports: list[tuple[str, str, int | None]],
    ) -> None:
        """Idempotently set the full list of imports for one module:
        (imported_name, import_type, line_number) tuples."""
        await conn.execute("DELETE FROM imports WHERE module_id = $1", module_id)
        if imports:
            await conn.executemany(
                """
                INSERT INTO imports (module_id, imported_name, import_type, line_number)
                VALUES ($1, $2, $3, $4)
                """,
                [(module_id, name, itype, line) for name, itype, line in imports],
            )

    async def store_graph(self, repo: str, graph: DependencyGraph) -> dict[str, str]:
        """Persist every module and its import edges for ``repo`` inside
        one transaction, so a failure midway leaves the previous state
        intact rather than a half-written graph."""
        with telemetry.span(
            "database.write",
            kind=SpanKind.CLIENT,
            tracer_name="agents.researcher",
            attributes={"swarm.repo": repo, "db.operation": "store_graph"},
        ) as current_span:
            started = time.monotonic()
            try:
                edges_by_module: dict[str, list[tuple[str, str, int | None]]] = defaultdict(list)
                for edge in graph.edges:
                    edges_by_module[edge.importer].append(
                        (edge.imported, edge.import_type, edge.line_number)
                    )

                module_ids: dict[str, str] = {}
                async with self.pool.acquire() as conn:
                    async with conn.transaction():
                        for module in graph.modules.values():
                            module_id = await self.upsert_module(conn, repo, module.path, module.name, "python")
                            module_ids[module.name] = module_id
                        for module_name, module_id in module_ids.items():
                            await self.replace_imports(conn, module_id, edges_by_module.get(module_name, []))
            except Exception:
                telemetry.get_metrics().db_failures.add(1, {"operation": "store_graph"})
                raise

            duration_ms = (time.monotonic() - started) * 1000
            current_span.set_attribute("swarm.duration_ms", duration_ms)
            telemetry.get_metrics().db_write_duration_ms.record(duration_ms, {"table": "modules", "operation": "store_graph"})
            log.info(
                "graph persisted",
                extra={
                    "repo": repo,
                    "modules": len(module_ids),
                    "edges": len(graph.edges),
                    "duration_ms": duration_ms,
                    "event_type": "database.write",
                },
            )
            return module_ids

    async def blast_radius(
        self, repo: str, changed_module_names: list[str], *, max_depth: int
    ) -> BlastRadiusResult:
        """Recursive-CTE traversal of ``imports``/``modules`` to find every
        module that transitively depends on ``changed_module_names``,
        bounded by ``max_depth`` hops."""
        changed = [m for m in changed_module_names if m]
        if not changed:
            return BlastRadiusResult(impacted_modules=[], impact_count=0, max_depth_reached=0)

        query = """
            WITH RECURSIVE dependents(name, depth) AS (
                SELECT u, 0
                FROM unnest($2::text[]) AS u
                UNION ALL
                SELECT m2.name, d.depth + 1
                FROM dependents d
                JOIN imports i ON i.imported_name = d.name
                JOIN modules m2 ON m2.id = i.module_id AND m2.repo = $1
                WHERE d.depth < $3
            )
            SELECT name, MIN(depth) AS depth
            FROM dependents
            GROUP BY name
            ORDER BY name
        """
        with telemetry.span(
            "researcher.blast_radius_analysis",
            kind=SpanKind.CLIENT,
            tracer_name="agents.researcher",
            attributes={"swarm.repo": repo, "swarm.max_depth": max_depth},
        ) as current_span:
            started = time.monotonic()
            try:
                async with self.pool.acquire() as conn:
                    rows = await conn.fetch(query, repo, changed, max_depth)
            except Exception:
                telemetry.get_metrics().db_failures.add(1, {"operation": "blast_radius"})
                raise
            duration_ms = (time.monotonic() - started) * 1000
            current_span.set_attribute("swarm.duration_ms", duration_ms)
            current_span.set_attribute("swarm.impact_count", len(rows))
            telemetry.get_metrics().blast_radius_duration_ms.record(duration_ms, {"repo": repo})
            log.info(
                "blast radius queried",
                extra={
                    "repo": repo,
                    "changed_modules": len(changed),
                    "max_depth": max_depth,
                    "impact_count": len(rows),
                    "duration_ms": duration_ms,
                    "event_type": "researcher.blast_radius_analysis",
                },
            )

        chains = {row["name"]: row["depth"] for row in rows}
        impacted = sorted(chains)
        max_reached = max(chains.values()) if chains else 0
        return BlastRadiusResult(
            impacted_modules=impacted,
            impact_count=len(impacted),
            max_depth_reached=max_reached,
            chains=chains,
        )

    async def __aenter__(self) -> "Database":
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()
