import pytest

from tools.seed_commit import build_commit, parse_args


def test_build_commit_explicit_fields():
    args = parse_args(
        [
            "--repo",
            "acme/widgets",
            "--author",
            "jane",
            "--message",
            "fix: bug",
            "--sha",
            "deadbeef",
            "--changed-files",
            "a.py,b.py",
        ]
    )
    commit = build_commit(args)

    assert commit.repo == "acme/widgets"
    assert commit.commit_sha == "deadbeef"
    assert commit.author == "jane"
    assert commit.changed_files == ["a.py", "b.py"]


def test_build_commit_random_generates_valid_payload():
    args = parse_args(["--random"])
    commit = build_commit(args)

    assert commit.repo.startswith("acme/")
    assert len(commit.commit_sha) == 40
    assert commit.branch == "main"


def test_build_commit_requires_fields_without_random():
    args = parse_args(["--branch", "main"])
    with pytest.raises(SystemExit):
        build_commit(args)


def test_parse_args_defaults():
    args = parse_args(["--random"])
    assert args.branch == "main"
    assert args.count == 1
