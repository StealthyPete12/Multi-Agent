import uuid

import asyncpg
import pytest

from agents.researcher.db import Database
from agents.researcher.graph import DependencyGraph, ImportEdge, ModuleInfo


def _unique_repo() -> str:
    return f"test/{uuid.uuid4().hex[:8]}"


def _simple_graph() -> DependencyGraph:
    graph = DependencyGraph()
    graph.add_module(ModuleInfo(name="database", path="database.py", is_package=False))
    graph.add_module(ModuleInfo(name="auth", path="auth.py", is_package=False))
    graph.add_edge(ImportEdge("auth", "database", "import", 1, False))
    return graph


async def test_store_graph_is_idempotent_on_rerun(postgres_available):
    if not postgres_available:
        pytest.skip("Postgres not reachable at DATABASE_URL")

    repo = _unique_repo()
    db = Database()
    await db.connect()
    try:
        ids_first = await db.store_graph(repo, _simple_graph())
        ids_second = await db.store_graph(repo, _simple_graph())

        assert ids_first == ids_second

        async with db.pool.acquire() as conn:
            module_count = await conn.fetchval("SELECT count(*) FROM modules WHERE repo = $1", repo)
            import_count = await conn.fetchval(
                """
                SELECT count(*) FROM imports i
                JOIN modules m ON m.id = i.module_id
                WHERE m.repo = $1
                """,
                repo,
            )
        assert module_count == 2
        assert import_count == 1, "re-running store_graph must not duplicate import rows"
    finally:
        async with db.pool.acquire() as conn:
            await conn.execute("DELETE FROM modules WHERE repo = $1", repo)
        await db.close()


async def test_store_graph_replaces_stale_imports(postgres_available):
    """If a module drops an import between runs, the old edge must be
    removed, not left dangling."""
    if not postgres_available:
        pytest.skip("Postgres not reachable at DATABASE_URL")

    repo = _unique_repo()
    db = Database()
    await db.connect()
    try:
        await db.store_graph(repo, _simple_graph())

        graph_without_import = DependencyGraph()
        graph_without_import.add_module(
            ModuleInfo(name="database", path="database.py", is_package=False)
        )
        graph_without_import.add_module(ModuleInfo(name="auth", path="auth.py", is_package=False))
        await db.store_graph(repo, graph_without_import)

        async with db.pool.acquire() as conn:
            import_count = await conn.fetchval(
                """
                SELECT count(*) FROM imports i
                JOIN modules m ON m.id = i.module_id
                WHERE m.repo = $1
                """,
                repo,
            )
        assert import_count == 0
    finally:
        async with db.pool.acquire() as conn:
            await conn.execute("DELETE FROM modules WHERE repo = $1", repo)
        await db.close()


async def test_upsert_module_updates_existing_row_on_conflict(postgres_available):
    if not postgres_available:
        pytest.skip("Postgres not reachable at DATABASE_URL")

    repo = _unique_repo()
    db = Database()
    await db.connect()
    try:
        async with db.pool.acquire() as conn:
            id1 = await db.upsert_module(conn, repo, "app.py", "app", "python")
            id2 = await db.upsert_module(conn, repo, "app.py", "app_renamed", "python")

            assert id1 == id2, "same (repo, path) must upsert in place, not duplicate"

            name = await conn.fetchval("SELECT name FROM modules WHERE id = $1", uuid.UUID(id2))
            assert name == "app_renamed"
    finally:
        async with db.pool.acquire() as conn:
            await conn.execute("DELETE FROM modules WHERE repo = $1", repo)
        await db.close()


async def test_transaction_rolls_back_on_failure(postgres_available):
    """If storing the graph fails partway through, no partial rows should
    be left behind."""
    if not postgres_available:
        pytest.skip("Postgres not reachable at DATABASE_URL")

    repo = _unique_repo()
    db = Database()
    await db.connect()
    try:
        async with db.pool.acquire() as conn:
            with pytest.raises(asyncpg.PostgresError):
                async with conn.transaction():
                    await db.upsert_module(conn, repo, "app.py", "app", "python")
                    # Force a constraint violation: commit_id FK to a
                    # nonexistent commit, inside the same transaction.
                    await conn.execute(
                        "INSERT INTO findings (commit_id, agent_name, severity, category, file_path, message) "
                        "VALUES ($1, 'x', 'low', 'x', 'x', 'x')",
                        uuid.uuid4(),
                    )

        async with db.pool.acquire() as conn:
            count = await conn.fetchval("SELECT count(*) FROM modules WHERE repo = $1", repo)
        assert count == 0, "failed transaction must not leave the module row committed"
    finally:
        async with db.pool.acquire() as conn:
            await conn.execute("DELETE FROM modules WHERE repo = $1", repo)
        await db.close()


async def test_blast_radius_recursive_cte_matches_chain_example(postgres_available):
    """The roadmap's worked example, persisted via store_graph then queried
    back with the recursive CTE: database <- auth <- payments <- checkout."""
    if not postgres_available:
        pytest.skip("Postgres not reachable at DATABASE_URL")

    repo = _unique_repo()
    graph = DependencyGraph()
    for name in ("database", "auth", "payments", "checkout"):
        graph.add_module(ModuleInfo(name=name, path=f"{name}.py", is_package=False))
    graph.add_edge(ImportEdge("auth", "database", "import", 1, False))
    graph.add_edge(ImportEdge("payments", "auth", "import", 1, False))
    graph.add_edge(ImportEdge("checkout", "payments", "import", 1, False))

    db = Database()
    await db.connect()
    try:
        await db.store_graph(repo, graph)

        result = await db.blast_radius(repo, ["database"], max_depth=10)

        assert result.impacted_modules == ["auth", "checkout", "database", "payments"]
        assert result.impact_count == 4
        assert result.max_depth_reached == 3
    finally:
        async with db.pool.acquire() as conn:
            await conn.execute("DELETE FROM modules WHERE repo = $1", repo)
        await db.close()


async def test_blast_radius_respects_max_depth(postgres_available):
    if not postgres_available:
        pytest.skip("Postgres not reachable at DATABASE_URL")

    repo = _unique_repo()
    graph = DependencyGraph()
    for name in ("database", "auth", "payments", "checkout"):
        graph.add_module(ModuleInfo(name=name, path=f"{name}.py", is_package=False))
    graph.add_edge(ImportEdge("auth", "database", "import", 1, False))
    graph.add_edge(ImportEdge("payments", "auth", "import", 1, False))
    graph.add_edge(ImportEdge("checkout", "payments", "import", 1, False))

    db = Database()
    await db.connect()
    try:
        await db.store_graph(repo, graph)

        result = await db.blast_radius(repo, ["database"], max_depth=1)

        assert result.impacted_modules == ["auth", "database"]
        assert result.impact_count == 2
    finally:
        async with db.pool.acquire() as conn:
            await conn.execute("DELETE FROM modules WHERE repo = $1", repo)
        await db.close()


async def test_blast_radius_with_no_changed_modules_is_empty(postgres_available):
    if not postgres_available:
        pytest.skip("Postgres not reachable at DATABASE_URL")

    db = Database()
    await db.connect()
    try:
        result = await db.blast_radius(_unique_repo(), [], max_depth=5)
        assert result.impact_count == 0
        assert result.impacted_modules == []
    finally:
        await db.close()
