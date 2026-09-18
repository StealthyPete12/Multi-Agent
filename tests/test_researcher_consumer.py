from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from agents.researcher.db import BlastRadiusResult
from agents.researcher.graph import DependencyGraph
from agents.researcher.main import analyze_commit, handle_message
from shared.contracts import CommitDetected, EventType, FindingsReady, make_envelope


class FakeMessage:
    """Minimal stand-in for aio_pika's IncomingMessage — enough surface
    (`body`, async `process()`) for handle_message's contract."""

    def __init__(self, body: bytes) -> None:
        self.body = body

    @asynccontextmanager
    async def process(self, ignore_processed: bool = True, requeue: bool = False):
        yield


class FakeRepositoryCache:
    def __init__(self, repo_path: Path) -> None:
        self.repo_path = repo_path
        self.ensure_calls: list[tuple[str, str]] = []

    async def ensure(self, repo: str, commit_sha: str) -> Path:
        self.ensure_calls.append((repo, commit_sha))
        return self.repo_path


class FakeDatabase:
    def __init__(self, blast_radius_result: BlastRadiusResult | None = None) -> None:
        self.stored: list[tuple[str, DependencyGraph]] = []
        self._blast_radius_result = blast_radius_result or BlastRadiusResult(
            impacted_modules=[], impact_count=0, max_depth_reached=0
        )

    async def store_graph(self, repo: str, graph: DependencyGraph) -> dict[str, str]:
        self.stored.append((repo, graph))
        return {name: f"id-{name}" for name in graph.modules}

    async def blast_radius(self, repo: str, changed_module_names, *, max_depth: int) -> BlastRadiusResult:
        return self._blast_radius_result


class FakeBroker:
    def __init__(self) -> None:
        self.published: list[tuple[object, str]] = []

    async def publish(self, envelope, *, routing_key: str) -> None:
        self.published.append((envelope, routing_key))


def _write_sample_repo(root: Path) -> None:
    (root / "auth.py").write_text("import database\n")
    (root / "database.py").write_text("")


def _sample_commit(**overrides) -> CommitDetected:
    defaults = dict(
        repo="acme/widgets",
        commit_sha="a" * 40,
        branch="main",
        author="jane",
        message="fix: bug",
        committed_at=datetime.now(timezone.utc),
        changed_files=["database.py"],
    )
    defaults.update(overrides)
    return CommitDetected(**defaults)


async def test_analyze_commit_builds_findings_ready_with_blast_radius(tmp_path):
    _write_sample_repo(tmp_path)
    repo_cache = FakeRepositoryCache(tmp_path)
    database = FakeDatabase(
        blast_radius_result=BlastRadiusResult(
            impacted_modules=["auth", "database"], impact_count=2, max_depth_reached=1
        )
    )

    findings = await analyze_commit(
        _sample_commit(), repo_cache=repo_cache, database=database
    )

    assert isinstance(findings, FindingsReady)
    assert findings.repo == "acme/widgets"
    assert findings.commit_sha == "a" * 40
    assert findings.findings == []
    assert findings.semantic_summary == ""
    assert findings.blast_radius.impact_count == 2
    assert findings.blast_radius.impacted_modules == ["auth", "database"]
    assert repo_cache.ensure_calls == [("acme/widgets", "a" * 40)]
    assert len(database.stored) == 1
    assert database.stored[0][0] == "acme/widgets"


async def test_analyze_commit_detects_sensitive_paths(tmp_path):
    (tmp_path / "auth").mkdir()
    (tmp_path / "auth" / "login.py").write_text("")
    repo_cache = FakeRepositoryCache(tmp_path)
    database = FakeDatabase()

    findings = await analyze_commit(
        _sample_commit(changed_files=["auth/login.py"]),
        repo_cache=repo_cache,
        database=database,
    )

    assert findings.sensitive_hits == ["auth/login.py"]


async def test_handle_message_valid_commit_publishes_findings_ready(tmp_path):
    _write_sample_repo(tmp_path)
    envelope = make_envelope(
        _sample_commit(), event_type=EventType.COMMIT_DETECTED, source="test"
    )
    broker = FakeBroker()
    repo_cache = FakeRepositoryCache(tmp_path)
    database = FakeDatabase()

    await handle_message(
        FakeMessage(envelope.to_bytes()), broker=broker, repo_cache=repo_cache, database=database
    )

    assert len(broker.published) == 1
    findings_envelope, routing_key = broker.published[0]
    assert routing_key == EventType.FINDINGS_READY.value
    assert findings_envelope.event_type is EventType.FINDINGS_READY
    assert findings_envelope.trace_id == envelope.trace_id
    assert findings_envelope.payload.commit_sha == envelope.payload.commit_sha
    assert findings_envelope.payload.repo == "acme/widgets"


async def test_handle_message_rejects_invalid_contract():
    bad_body = b'{"event_type": "commit.detected", "source": "x", "payload": {}}'
    broker = FakeBroker()
    repo_cache = FakeRepositoryCache(Path("/does/not/matter"))
    database = FakeDatabase()

    with pytest.raises(ValidationError):
        await handle_message(
            FakeMessage(bad_body), broker=broker, repo_cache=repo_cache, database=database
        )

    assert broker.published == []
