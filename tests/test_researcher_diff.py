import subprocess
from pathlib import Path

from agents.researcher.diff import get_truncated_diff


def _run(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_repo_with_two_commits(root: Path) -> tuple[str, str]:
    _run(["init", "-q", "-b", "main"], root)
    _run(["config", "user.email", "test@example.com"], root)
    _run(["config", "user.name", "Test"], root)
    (root / "a.py").write_text("x = 1\n")
    _run(["add", "."], root)
    _run(["commit", "-q", "-m", "initial"], root)

    (root / "a.py").write_text("x = 2\n")
    (root / "b.py").write_text("y = 1\n")
    _run(["add", "."], root)
    _run(["commit", "-q", "-m", "second"], root)

    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()
    return sha, "a.py"


async def test_get_truncated_diff_returns_diff_for_real_commit(tmp_path):
    sha, changed_file = _init_repo_with_two_commits(tmp_path)

    diff = await get_truncated_diff(tmp_path, sha, [changed_file])

    assert "a.py" in diff
    assert "-x = 1" in diff
    assert "+x = 2" in diff
    assert "b.py" not in diff  # scoped to changed_files


async def test_get_truncated_diff_all_files_when_none_specified(tmp_path):
    sha, _ = _init_repo_with_two_commits(tmp_path)

    diff = await get_truncated_diff(tmp_path, sha, [])

    assert "a.py" in diff
    assert "b.py" in diff


async def test_get_truncated_diff_truncates_long_output(tmp_path):
    sha, changed_file = _init_repo_with_two_commits(tmp_path)

    diff = await get_truncated_diff(tmp_path, sha, [changed_file], max_chars=10)

    assert len(diff) <= 10 + len("\n... (truncated)")
    assert diff.endswith("... (truncated)")


async def test_get_truncated_diff_returns_empty_on_bad_sha(tmp_path):
    _init_repo_with_two_commits(tmp_path)

    diff = await get_truncated_diff(tmp_path, "0" * 40, [])

    assert diff == ""


async def test_get_truncated_diff_returns_empty_on_missing_repo(tmp_path):
    missing = tmp_path / "does-not-exist"

    diff = await get_truncated_diff(missing, "0" * 40, [])

    assert diff == ""
