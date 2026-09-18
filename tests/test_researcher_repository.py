import os
import subprocess
from pathlib import Path

from agents.researcher.repository import RepositoryCache

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _run_git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=path, env=_GIT_ENV, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _init_source_repo(path: Path) -> list[str]:
    """A local git repo with two commits, used as a clone source without
    any network access."""
    path.mkdir(parents=True, exist_ok=True)
    _run_git(path, "init", "-q", "-b", "main")

    (path / "database.py").write_text("VERSION = 1\n")
    _run_git(path, "add", ".")
    _run_git(path, "commit", "-q", "-m", "commit1")
    sha1 = _run_git(path, "rev-parse", "HEAD")

    (path / "database.py").write_text("VERSION = 2\n")
    _run_git(path, "add", ".")
    _run_git(path, "commit", "-q", "-m", "commit2")
    sha2 = _run_git(path, "rev-parse", "HEAD")

    return [sha1, sha2]


def _file_url(path: Path) -> str:
    """A `file://` URL, which (unlike a bare local path) git actually
    honors `--depth 1` for — needed to exercise real shallow-clone
    behavior in these tests."""
    return f"file://{path}"


async def test_clone_creates_local_checkout_at_requested_commit(tmp_path):
    source = tmp_path / "source"
    sha1, sha2 = _init_source_repo(source)
    cache = RepositoryCache(cache_dir=tmp_path / "cache")

    checkout = await cache.ensure(_file_url(source), sha2)

    assert checkout.exists()
    assert (checkout / "database.py").read_text() == "VERSION = 2\n"
    assert cache.is_cached(_file_url(source))


async def test_local_path_naming_is_deterministic_per_repo_slug(tmp_path):
    cache = RepositoryCache(cache_dir=tmp_path / "cache")

    assert cache.local_path("acme/widgets") == tmp_path / "cache" / "acme__widgets"


async def test_clone_url_passthrough_for_local_and_remote_urls(tmp_path):
    cache = RepositoryCache(cache_dir=tmp_path / "cache", clone_base_url="https://github.com/")

    assert cache.clone_url("acme/widgets") == "https://github.com/acme/widgets.git"
    assert cache.clone_url("/abs/local/repo") == "/abs/local/repo"
    assert cache.clone_url("https://example.com/x.git") == "https://example.com/x.git"


async def test_second_ensure_reuses_existing_clone_without_recloning(tmp_path):
    source = tmp_path / "source"
    _sha1, sha2 = _init_source_repo(source)
    cache = RepositoryCache(cache_dir=tmp_path / "cache")

    clone_calls = []
    original_clone = cache._clone

    async def spy_clone(url, path):
        clone_calls.append(url)
        await original_clone(url, path)

    cache._clone = spy_clone  # type: ignore[method-assign]

    await cache.ensure(_file_url(source), sha2)
    await cache.ensure(_file_url(source), sha2)

    assert len(clone_calls) == 1, "second ensure() should reuse the cached clone"


async def test_checkout_of_older_commit_triggers_unshallow_fetch(tmp_path):
    """A shallow (--depth 1) clone only has the tip commit; checking out an
    older commit must fall back to an unshallow fetch and retry."""
    source = tmp_path / "source"
    sha1, sha2 = _init_source_repo(source)
    cache = RepositoryCache(cache_dir=tmp_path / "cache")

    # First ensure() shallow-clones at the tip (sha2).
    await cache.ensure(_file_url(source), sha2)

    fetch_calls = []
    original_fetch = cache._fetch

    async def spy_fetch(path, *, unshallow=False):
        fetch_calls.append(unshallow)
        await original_fetch(path, unshallow=unshallow)

    cache._fetch = spy_fetch  # type: ignore[method-assign]

    checkout = await cache.ensure(_file_url(source), sha1)

    assert (checkout / "database.py").read_text() == "VERSION = 1\n"
    assert True in fetch_calls, "expected an unshallow fetch to recover the older commit"


async def test_stale_cache_triggers_fetch_before_checkout(tmp_path):
    source = tmp_path / "source"
    _sha1, sha2 = _init_source_repo(source)
    cache = RepositoryCache(cache_dir=tmp_path / "cache", stale_after_seconds=0)

    await cache.ensure(_file_url(source), sha2)

    fetch_calls = []
    original_fetch = cache._fetch

    async def spy_fetch(path, *, unshallow=False):
        fetch_calls.append(unshallow)
        await original_fetch(path, unshallow=unshallow)

    cache._fetch = spy_fetch  # type: ignore[method-assign]

    assert cache.is_stale(_file_url(source)) is True
    await cache.ensure(_file_url(source), sha2)

    assert len(fetch_calls) == 1


async def test_new_commit_pushed_upstream_is_picked_up_on_refresh(tmp_path):
    source = tmp_path / "source"
    _sha1, sha2 = _init_source_repo(source)
    cache = RepositoryCache(cache_dir=tmp_path / "cache", stale_after_seconds=0)

    await cache.ensure(_file_url(source), sha2)

    (source / "database.py").write_text("VERSION = 3\n")
    _run_git(source, "add", ".")
    _run_git(source, "commit", "-q", "-m", "commit3")
    sha3 = _run_git(source, "rev-parse", "HEAD")

    checkout = await cache.ensure(_file_url(source), sha3)

    assert (checkout / "database.py").read_text() == "VERSION = 3\n"
