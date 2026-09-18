"""Local repository cache: clone-or-reuse a git checkout at a given commit.

One local clone per repository is kept under ``REPO_CACHE_DIR`` and reused
across commits/runs (``git fetch`` + ``git checkout <sha>``) instead of
re-cloning from scratch every time. Clones are shallow (``--depth 1``)
where possible; a checkout of a commit not present in the shallow history
triggers an automatic unshallow fetch and retry.

All git invocations run as async subprocesses so the researcher consumer
never blocks its event loop while cloning/fetching.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from opentelemetry.trace import SpanKind

from shared import telemetry
from shared.logging import configure_logging

__all__ = ["RepositoryCache", "GitCommandError", "CommitNotFoundError"]

log = configure_logging(service_name="researcher")

DEFAULT_CACHE_DIR = "/tmp/swarm-repo-cache"
DEFAULT_CLONE_BASE_URL = "https://github.com/"
DEFAULT_STALE_SECONDS = 300
_STALE_MARKER = ".swarm-last-fetch"


class GitCommandError(RuntimeError):
    """A git subprocess exited non-zero."""


class CommitNotFoundError(GitCommandError):
    """`git checkout <sha>` failed because the commit isn't in the local
    history (e.g. a shallow clone that hasn't fetched it yet)."""


class RepositoryCache:
    """Clones and reuses local git checkouts, keyed by repository slug."""

    def __init__(
        self,
        cache_dir: str | os.PathLike[str] | None = None,
        *,
        clone_base_url: str | None = None,
        stale_after_seconds: int | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir or os.environ.get("REPO_CACHE_DIR", DEFAULT_CACHE_DIR))
        self.clone_base_url = clone_base_url or os.environ.get(
            "REPO_CLONE_BASE_URL", DEFAULT_CLONE_BASE_URL
        )
        self.stale_after_seconds = (
            stale_after_seconds
            if stale_after_seconds is not None
            else int(os.environ.get("REPO_STALE_SECONDS", DEFAULT_STALE_SECONDS))
        )
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def local_path(self, repo: str) -> Path:
        """Deterministic on-disk path for a repo slug, e.g. ``acme/widgets``
        -> ``<cache_dir>/acme__widgets``."""
        return self.cache_dir / repo.replace("/", "__")

    def clone_url(self, repo: str) -> str:
        """Build the clone URL for a repo slug, unless ``repo`` already
        looks like a URL/local path (used directly, e.g. in tests)."""
        if repo.startswith(("http://", "https://", "git@", "file://", "/", ".")):
            return repo
        return f"{self.clone_base_url.rstrip('/')}/{repo}.git"

    def is_cached(self, repo: str) -> bool:
        path = self.local_path(repo)
        return (path / ".git").exists()

    def is_stale(self, repo: str) -> bool:
        marker = self.local_path(repo) / _STALE_MARKER
        if not marker.exists():
            return True
        return (time.time() - marker.stat().st_mtime) > self.stale_after_seconds

    async def ensure(
        self, repo: str, commit_sha: str, *, clone_url: str | None = None
    ) -> Path:
        """Return a local checkout of ``repo`` at ``commit_sha``, cloning or
        refreshing as needed. Reuses an existing clone whenever possible."""
        path = self.local_path(repo)
        url = clone_url or self.clone_url(repo)

        with telemetry.span(
            "repository.ensure",
            kind=SpanKind.INTERNAL,
            tracer_name="agents.researcher",
            attributes={"swarm.repo": repo, "swarm.commit_sha": commit_sha},
        ):
            if self.is_cached(repo):
                log.info("repository cache hit", extra={"repo": repo, "path": str(path), "event_type": "repository.cache_hit"})
                telemetry.get_metrics().repo_cache_hits.add(1, {"repo": repo})
                if self.is_stale(repo):
                    log.info("repository cache stale, refreshing", extra={"repo": repo, "event_type": "repository.refresh"})
                    await self._fetch(path)
            else:
                log.info("repository cache miss, cloning", extra={"repo": repo, "url": url, "event_type": "repository.cache_miss"})
                await self._clone(url, path)

            try:
                await self._checkout(path, commit_sha)
            except CommitNotFoundError:
                log.info(
                    "commit not in local history, unshallowing",
                    extra={"repo": repo, "commit_sha": commit_sha, "event_type": "repository.unshallow"},
                )
                await self._fetch(path, unshallow=True)
                await self._checkout(path, commit_sha)

            self._touch(path)
            return path

    async def _run_git(self, *args: str, cwd: Path | None = None) -> tuple[int, str, str]:
        import asyncio

        process = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(cwd) if cwd else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return process.returncode or 0, stdout.decode("utf-8", errors="replace"), stderr.decode(
            "utf-8", errors="replace"
        )

    async def _clone(self, url: str, path: Path) -> None:
        with telemetry.span(
            "repository.clone", kind=SpanKind.INTERNAL, tracer_name="agents.researcher", attributes={"swarm.url": url}
        ) as current_span:
            path.parent.mkdir(parents=True, exist_ok=True)
            started = time.monotonic()
            code, _, stderr = await self._run_git("clone", "--depth", "1", url, str(path))
            duration_ms = (time.monotonic() - started) * 1000
            current_span.set_attribute("swarm.duration_ms", duration_ms)
            if code != 0:
                raise GitCommandError(f"git clone failed for {url}: {stderr.strip()}")
            log.info(
                "repository cloned",
                extra={
                    "url": url,
                    "path": str(path),
                    "duration_ms": duration_ms,
                    "shallow": True,
                    "event_type": "repository.clone",
                },
            )
            telemetry.get_metrics().repo_clones.add(1, {"url": url})
            telemetry.get_metrics().repo_clone_duration_ms.record(duration_ms, {"url": url})

    async def _fetch(self, path: Path, *, unshallow: bool = False) -> None:
        with telemetry.span(
            "repository.refresh",
            kind=SpanKind.INTERNAL,
            tracer_name="agents.researcher",
            attributes={"swarm.path": str(path), "swarm.unshallow": unshallow},
        ) as current_span:
            started = time.monotonic()
            args = ["fetch", "origin"]
            if unshallow:
                args.append("--unshallow")
            code, _, stderr = await self._run_git(*args, cwd=path)
            duration_ms = (time.monotonic() - started) * 1000
            current_span.set_attribute("swarm.duration_ms", duration_ms)
            if code != 0 and unshallow and "already a complete repository" in stderr:
                # Already fully fetched (e.g. a non-shallow test fixture repo);
                # not an error.
                code = 0
            if code != 0:
                raise GitCommandError(f"git fetch failed for {path}: {stderr.strip()}")
            log.info(
                "repository fetched",
                extra={
                    "path": str(path),
                    "duration_ms": duration_ms,
                    "unshallow": unshallow,
                    "event_type": "repository.refresh",
                },
            )
            telemetry.get_metrics().repo_refresh_duration_ms.record(duration_ms, {"unshallow": str(unshallow)})

    async def _checkout(self, path: Path, commit_sha: str) -> None:
        started = time.monotonic()
        code, _, stderr = await self._run_git("checkout", "--detach", commit_sha, cwd=path)
        duration_ms = (time.monotonic() - started) * 1000
        if code != 0:
            raise CommitNotFoundError(
                f"git checkout {commit_sha} failed in {path}: {stderr.strip()}"
            )
        log.info(
            "repository checked out",
            extra={"path": str(path), "commit_sha": commit_sha, "duration_ms": duration_ms},
        )

    def _touch(self, path: Path) -> None:
        (path / _STALE_MARKER).write_text(str(time.time()))
