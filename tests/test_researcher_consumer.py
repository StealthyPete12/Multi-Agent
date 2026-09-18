from datetime import datetime, timezone
from pathlib import Path

import pytest

from agents.researcher.db import BlastRadiusResult
from agents.researcher.graph import DependencyGraph
from agents.researcher.main import analyze_commit, classify_researcher_failure, handle_message
from agents.researcher.repository import CommitNotFoundError, GitCommandError
from shared.contracts import CommitDetected, EventType, FindingsReady, make_envelope
from shared.errors import FatalError, PoisonMessageError, RetryableError
from shared.retry import HEADER_ATTEMPT, MAX_RETRY_ATTEMPTS


class FakeMessage:
    """Minimal stand-in for aio_pika's IncomingMessage — enough surface
    (`body`, `headers`, `ack`/`nack`) for handle_message's manual-ack
    contract."""

    def __init__(self, body: bytes, headers: dict | None = None) -> None:
        self.body = body
        self.headers = headers or {}
        self.acked = False
        self.nacked: list[bool] = []  # records the `requeue` value of each nack() call

    async def ack(self) -> None:
        self.acked = True

    async def nack(self, requeue: bool = False) -> None:
        self.nacked.append(requeue)


class FakeRepositoryCache:
    def __init__(self, repo_path: Path, *, raises: Exception | None = None) -> None:
        self.repo_path = repo_path
        self.ensure_calls: list[tuple[str, str]] = []
        self._raises = raises

    async def ensure(self, repo: str, commit_sha: str) -> Path:
        self.ensure_calls.append((repo, commit_sha))
        if self._raises is not None:
            raise self._raises
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


class FakeRetryLadder:
    def __init__(self) -> None:
        self.scheduled: list[dict] = []
        self.dlq: list[dict] = []
        self.raw_dlq: list[dict] = []

    async def schedule_retry(self, envelope, *, routing_key, reason, attempt, original_queue):
        self.scheduled.append(
            {
                "envelope": envelope,
                "routing_key": routing_key,
                "reason": reason,
                "attempt": attempt,
                "original_queue": original_queue,
            }
        )

    async def send_to_dlq(self, envelope, *, reason, attempt, original_queue):
        self.dlq.append(
            {"envelope": envelope, "reason": reason, "attempt": attempt, "original_queue": original_queue}
        )

    async def send_raw_to_dlq(self, raw_body, *, reason, original_queue):
        self.raw_dlq.append({"raw_body": raw_body, "reason": reason, "original_queue": original_queue})


class FakeIdempotency:
    def __init__(self, *, already_claimed: bool = False) -> None:
        self.claimed: list[str] = []
        self.released: list[str] = []
        self._already_claimed = already_claimed

    async def claim(self, *, event_id, event_type, trace_id):
        self.claimed.append(event_id)
        return not self._already_claimed

    async def release(self, event_id):
        self.released.append(event_id)


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


def _handle_kwargs(broker, repo_cache, database, retry_ladder=None, idempotency=None):
    return dict(
        broker=broker,
        repo_cache=repo_cache,
        database=database,
        retry_ladder=retry_ladder or FakeRetryLadder(),
        idempotency=idempotency or FakeIdempotency(),
    )


async def test_handle_message_valid_commit_publishes_findings_ready(tmp_path):
    _write_sample_repo(tmp_path)
    envelope = make_envelope(
        _sample_commit(), event_type=EventType.COMMIT_DETECTED, source="test"
    )
    broker = FakeBroker()
    repo_cache = FakeRepositoryCache(tmp_path)
    database = FakeDatabase()
    idempotency = FakeIdempotency()
    message = FakeMessage(envelope.to_bytes())

    await handle_message(message, **_handle_kwargs(broker, repo_cache, database, idempotency=idempotency))

    assert len(broker.published) == 1
    findings_envelope, routing_key = broker.published[0]
    assert routing_key == EventType.FINDINGS_READY.value
    assert findings_envelope.event_type is EventType.FINDINGS_READY
    assert findings_envelope.trace_id == envelope.trace_id
    assert findings_envelope.payload.commit_sha == envelope.payload.commit_sha
    assert findings_envelope.payload.repo == "acme/widgets"
    assert message.acked is True
    assert idempotency.claimed == [envelope.event_id]
    assert idempotency.released == []


async def test_handle_message_rejects_invalid_contract_routes_to_dlq():
    bad_body = b'{"event_type": "commit.detected", "source": "x", "payload": {}}'
    broker = FakeBroker()
    repo_cache = FakeRepositoryCache(Path("/does/not/matter"))
    database = FakeDatabase()
    retry_ladder = FakeRetryLadder()
    message = FakeMessage(bad_body)

    await handle_message(message, **_handle_kwargs(broker, repo_cache, database, retry_ladder=retry_ladder))

    assert broker.published == []
    assert message.acked is True
    assert len(retry_ladder.raw_dlq) == 1
    assert retry_ladder.raw_dlq[0]["raw_body"] == bad_body


async def test_handle_message_skips_duplicate_delivery(tmp_path):
    _write_sample_repo(tmp_path)
    envelope = make_envelope(_sample_commit(), event_type=EventType.COMMIT_DETECTED, source="test")
    broker = FakeBroker()
    repo_cache = FakeRepositoryCache(tmp_path)
    database = FakeDatabase()
    idempotency = FakeIdempotency(already_claimed=True)
    message = FakeMessage(envelope.to_bytes())

    await handle_message(message, **_handle_kwargs(broker, repo_cache, database, idempotency=idempotency))

    assert broker.published == []
    assert message.acked is True
    assert repo_cache.ensure_calls == []  # no work was done at all


async def test_handle_message_retryable_failure_schedules_retry(tmp_path):
    envelope = make_envelope(_sample_commit(), event_type=EventType.COMMIT_DETECTED, source="test")
    broker = FakeBroker()
    repo_cache = FakeRepositoryCache(tmp_path, raises=GitCommandError("clone failed: connection reset"))
    database = FakeDatabase()
    retry_ladder = FakeRetryLadder()
    idempotency = FakeIdempotency()
    message = FakeMessage(envelope.to_bytes())

    await handle_message(
        message, **_handle_kwargs(broker, repo_cache, database, retry_ladder, idempotency)
    )

    assert message.acked is True
    assert len(retry_ladder.scheduled) == 1
    assert retry_ladder.scheduled[0]["attempt"] == 1
    assert retry_ladder.scheduled[0]["routing_key"] == EventType.COMMIT_DETECTED.value
    assert idempotency.released == [envelope.event_id]  # released so a retry can reclaim
    assert retry_ladder.dlq == []


async def test_handle_message_retryable_failure_exhausted_goes_to_dlq(tmp_path):
    envelope = make_envelope(_sample_commit(), event_type=EventType.COMMIT_DETECTED, source="test")
    broker = FakeBroker()
    repo_cache = FakeRepositoryCache(tmp_path, raises=GitCommandError("still failing"))
    database = FakeDatabase()
    retry_ladder = FakeRetryLadder()
    idempotency = FakeIdempotency()
    # Already at the last rung — the next failure should exhaust the ladder.
    message = FakeMessage(envelope.to_bytes(), headers={HEADER_ATTEMPT: MAX_RETRY_ATTEMPTS})

    await handle_message(
        message, **_handle_kwargs(broker, repo_cache, database, retry_ladder, idempotency)
    )

    assert message.acked is True
    assert retry_ladder.scheduled == []
    assert len(retry_ladder.dlq) == 1
    assert retry_ladder.dlq[0]["attempt"] == MAX_RETRY_ATTEMPTS + 1


async def test_handle_message_poison_failure_routes_to_dlq_immediately(tmp_path):
    envelope = make_envelope(_sample_commit(), event_type=EventType.COMMIT_DETECTED, source="test")
    broker = FakeBroker()
    repo_cache = FakeRepositoryCache(tmp_path, raises=CommitNotFoundError("commit not found"))
    database = FakeDatabase()
    retry_ladder = FakeRetryLadder()
    idempotency = FakeIdempotency()
    message = FakeMessage(envelope.to_bytes())

    await handle_message(
        message, **_handle_kwargs(broker, repo_cache, database, retry_ladder, idempotency)
    )

    assert message.acked is True
    assert retry_ladder.scheduled == []
    assert len(retry_ladder.dlq) == 1
    assert idempotency.released == [envelope.event_id]


async def test_handle_message_fatal_failure_nacks_with_requeue_and_raises(tmp_path):
    envelope = make_envelope(_sample_commit(), event_type=EventType.COMMIT_DETECTED, source="test")
    broker = FakeBroker()
    repo_cache = FakeRepositoryCache(tmp_path, raises=ValueError("unexpected bug"))
    database = FakeDatabase()
    retry_ladder = FakeRetryLadder()
    idempotency = FakeIdempotency()
    message = FakeMessage(envelope.to_bytes())

    with pytest.raises(FatalError):
        await handle_message(
            message, **_handle_kwargs(broker, repo_cache, database, retry_ladder, idempotency)
        )

    assert message.acked is False
    assert message.nacked == [True]
    assert retry_ladder.scheduled == []
    assert retry_ladder.dlq == []


def test_classify_researcher_failure_commit_not_found_is_poison():
    assert classify_researcher_failure(CommitNotFoundError("x")) is PoisonMessageError


def test_classify_researcher_failure_git_command_error_is_retryable():
    assert classify_researcher_failure(GitCommandError("x")) is RetryableError


def test_classify_researcher_failure_unknown_is_fatal():
    assert classify_researcher_failure(ValueError("x")) is FatalError
