from agents.watcher.main import branch_from_ref, extract_commit_events


def _push_payload(ref: str = "refs/heads/main", commits=None) -> dict:
    return {
        "ref": ref,
        "repository": {"full_name": "acme/widgets"},
        "compare": "https://github.com/acme/widgets/compare/a...b",
        "commits": commits if commits is not None else [],
    }


def test_branch_from_ref_strips_prefix():
    assert branch_from_ref("refs/heads/main") == "main"
    assert branch_from_ref("refs/heads/feature/x") == "feature/x"


def test_branch_from_ref_passthrough_when_no_prefix():
    assert branch_from_ref("main") == "main"


def test_extract_commit_events_single_commit():
    payload = _push_payload(
        commits=[
            {
                "id": "a" * 40,
                "author": {"name": "jane"},
                "message": "fix: off by one",
                "timestamp": "2024-01-01T00:00:00+00:00",
                "added": ["a.py"],
                "modified": ["b.py"],
                "removed": [],
            }
        ]
    )
    events = extract_commit_events(payload, trace_id="t1")

    assert len(events) == 1
    event = events[0]
    assert event.repo == "acme/widgets"
    assert event.branch == "main"
    assert event.commit_sha == "a" * 40
    assert event.author == "jane"
    assert event.message == "fix: off by one"
    assert event.changed_files == ["a.py", "b.py"]


def test_extract_commit_events_multiple_commits():
    payload = _push_payload(
        commits=[
            {
                "id": "a" * 40,
                "author": {"name": "x"},
                "message": "m1",
                "timestamp": "2024-01-01T00:00:00+00:00",
            },
            {
                "id": "b" * 40,
                "author": {"name": "y"},
                "message": "m2",
                "timestamp": "2024-01-01T00:00:01+00:00",
            },
        ]
    )
    events = extract_commit_events(payload, trace_id="t1")
    assert [e.commit_sha for e in events] == ["a" * 40, "b" * 40]


def test_extract_commit_events_empty_for_branch_delete():
    payload = _push_payload(commits=[])
    assert extract_commit_events(payload, trace_id="t1") == []
