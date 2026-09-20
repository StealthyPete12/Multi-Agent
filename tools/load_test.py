#!/usr/bin/env python3
"""Phase 5 performance test: publish N synthetic commits through the real
watcher -> researcher -> reviewer pipeline and measure end-to-end latency
and throughput.

Unlike ``tools/seed_commit.py`` (fire-and-forget), this tool creates N
real commits in a local git fixture repo (so the researcher does real
clone/AST/blast-radius work, not a no-op), publishes one ``commit.detected``
per commit, then polls the ``reports`` table until every commit's report
has landed (or a deadline elapses), and reports latency percentiles +
throughput.

Requires the researcher and reviewer to already be running (this tool
only publishes and observes — it doesn't start/stop the pipeline).

Usage::

    python -m tools.load_test --count 100
    python -m tools.load_test --count 100 --repo-dir /tmp/loadtest-repo \\
        --report-out /tmp/load_test_report.md
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import asyncpg

from shared.broker import Broker
from shared.contracts import CommitDetected, EventType, make_envelope
from shared.logging import configure_logging

log = configure_logging(service_name="load_test")

DEFAULT_DATABASE_URL = "postgresql://swarm:swarm_dev_password@localhost:5432/code_review_swarm"


def _run_git(*args: str, cwd: Path) -> None:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr}")


def build_fixture_repo(repo_dir: Path, *, count: int) -> tuple[str, list[str]]:
    """Create (or reuse) a local git repo with a small import graph, then
    add ``count`` real commits on top of it, each touching a different
    file so the researcher's blast-radius query has real work to do.
    Returns (repo_slug, list_of_commit_shas) in commit order.

    ``repo_slug`` is the fixture directory's own absolute path: passing it
    straight through as ``CommitDetected.repo`` makes
    ``RepositoryCache.clone_url()`` use it directly as a local-filesystem
    clone source (any string starting with ``/`` is used as-is, no
    ``REPO_CLONE_BASE_URL`` prefix/``.git`` suffix applied — see
    agents/researcher/repository.py), so this tool needs no env
    configuration on the researcher side beyond it already running.
    """
    repo_dir.mkdir(parents=True, exist_ok=True)
    if not (repo_dir / ".git").exists():
        _run_git("init", "-q", "-b", "main", cwd=repo_dir)
        _run_git("config", "user.email", "loadtest@swarm.test", cwd=repo_dir)
        _run_git("config", "user.name", "Load Test", cwd=repo_dir)
        (repo_dir / "database.py").write_text("def connect():\n    return 'db'\n")
        (repo_dir / "auth").mkdir(exist_ok=True)
        (repo_dir / "auth" / "__init__.py").write_text("")
        (repo_dir / "auth" / "login.py").write_text(
            "import database\n\ndef login(u):\n    database.connect()\n"
        )
        (repo_dir / "src").mkdir(exist_ok=True)
        (repo_dir / "src" / "__init__.py").write_text("")
        (repo_dir / "src" / "app.py").write_text(
            "from auth import login\n\ndef handle(u):\n    return login.login(u)\n"
        )
        _run_git("add", "-A", cwd=repo_dir)
        _run_git("commit", "-q", "-m", "initial commit", cwd=repo_dir)

    shas: list[str] = []
    for i in range(count):
        marker_file = repo_dir / "src" / f"change_{i}.py"
        marker_file.write_text(f"# load test commit {i}\nimport database\n")
        _run_git("add", "-A", cwd=repo_dir)
        _run_git("commit", "-q", "-m", f"load test commit {i}", cwd=repo_dir)
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True, text=True
        )
        shas.append(result.stdout.strip())

    return str(repo_dir), shas


@dataclass
class Sample:
    commit_sha: str
    published_at: float  # time.monotonic()
    published_wall: datetime
    completed_at: float | None = None


@dataclass
class LoadTestResult:
    samples: list[Sample] = field(default_factory=list)
    wall_start: float = 0.0
    wall_end: float = 0.0

    @property
    def latencies_seconds(self) -> list[float]:
        return [s.completed_at - s.published_at for s in self.samples if s.completed_at is not None]

    @property
    def completed_count(self) -> int:
        return sum(1 for s in self.samples if s.completed_at is not None)


async def publish_commits(repo_slug: str, shas: list[str]) -> LoadTestResult:
    broker = Broker()
    await broker.connect()
    result = LoadTestResult()
    try:
        for i, sha in enumerate(shas):
            payload = CommitDetected(
                repo=repo_slug,
                commit_sha=sha,
                branch="main",
                author="loadtest@swarm.test",
                message=f"load test commit {i}",
                committed_at=datetime.now(UTC),
                changed_files=[f"src/change_{i}.py", "database.py"],
            )
            envelope = make_envelope(
                payload, event_type=EventType.COMMIT_DETECTED, source="load_test"
            )
            published_at = time.monotonic()
            await broker.publish(envelope, routing_key=EventType.COMMIT_DETECTED.value)
            result.samples.append(
                Sample(commit_sha=sha, published_at=published_at, published_wall=datetime.now(UTC))
            )
    finally:
        await broker.close()
    return result


async def wait_for_completion(
    repo_slug: str, result: LoadTestResult, *, timeout_seconds: float
) -> None:
    pool = await asyncpg.create_pool(DEFAULT_DATABASE_URL, min_size=1, max_size=5)
    pending = {s.commit_sha: s for s in result.samples}
    deadline = time.monotonic() + timeout_seconds
    try:
        while pending and time.monotonic() < deadline:
            rows = await pool.fetch(
                "SELECT commit_sha FROM reports WHERE repo = $1 AND commit_sha = ANY($2::text[])",
                repo_slug,
                list(pending.keys()),
            )
            now = time.monotonic()
            for row in rows:
                sha = row["commit_sha"]
                if sha in pending:
                    pending[sha].completed_at = now
                    del pending[sha]
            if pending:
                await asyncio.sleep(0.5)
    finally:
        await pool.close()


def percentile(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    k = (len(ordered) - 1) * p
    f, c = int(k), min(int(k) + 1, len(ordered) - 1)
    if f == c:
        return ordered[f]
    return ordered[f] + (ordered[c] - ordered[f]) * (k - f)


def render_report(result: LoadTestResult, *, count: int, timeout_seconds: float) -> str:
    latencies = result.latencies_seconds
    completed = result.completed_count
    wall_seconds = result.wall_end - result.wall_start
    throughput = completed / wall_seconds if wall_seconds > 0 else 0.0

    lines = [
        "# Phase 5 Load Test Report",
        "",
        f"- Commits submitted: {count}",
        f"- Commits completed (review.completed persisted): {completed}/{count}",
        f"- Timeout budget: {timeout_seconds:.0f}s",
        f"- Wall time (first publish -> last completion): {wall_seconds:.2f}s",
        f"- Throughput: {throughput:.2f} commits/sec",
        "",
    ]
    if latencies:
        lines += [
            "## Latency (publish -> review.completed persisted)",
            "",
            f"- Average: {statistics.mean(latencies) * 1000:.1f} ms",
            f"- p50: {percentile(latencies, 0.50) * 1000:.1f} ms",
            f"- p95: {percentile(latencies, 0.95) * 1000:.1f} ms",
            f"- p99: {percentile(latencies, 0.99) * 1000:.1f} ms",
            f"- Min: {min(latencies) * 1000:.1f} ms",
            f"- Max: {max(latencies) * 1000:.1f} ms",
        ]
    else:
        lines.append("No commits completed within the timeout budget.")
    return "\n".join(lines) + "\n"


async def run(args: argparse.Namespace) -> str:
    repo_dir = Path(args.repo_dir)
    repo_slug, shas = build_fixture_repo(repo_dir, count=args.count)
    log.info("fixture repo ready", extra={"repo_dir": str(repo_dir), "commits": len(shas)})

    result = await publish_commits(repo_slug, shas)
    result.wall_start = min(s.published_at for s in result.samples)
    log.info("published all commits", extra={"count": len(result.samples)})

    await wait_for_completion(repo_slug, result, timeout_seconds=args.timeout)
    result.wall_end = max((s.completed_at or s.published_at) for s in result.samples)

    report = render_report(result, count=args.count, timeout_seconds=args.timeout)
    print(report)
    if args.report_out:
        Path(args.report_out).write_text(report)
        log.info("report written", extra={"path": args.report_out})
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--count", type=int, default=100, help="number of synthetic commits (default: 100)"
    )
    parser.add_argument(
        "--repo-dir",
        default="/tmp/swarm-loadtest-fixture",
        help="local git fixture repo directory (created/reused)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        help="max seconds to wait for completions (default: 180)",
    )
    parser.add_argument("--report-out", help="write the markdown report to this path")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    asyncio.run(run(args))


if __name__ == "__main__":
    main(sys.argv[1:])
