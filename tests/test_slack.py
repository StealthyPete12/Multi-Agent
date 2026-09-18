import pytest

from shared.slack import SlackError, SlackNotifier, build_review_message


def test_build_review_message_includes_all_required_fields():
    payload = build_review_message(
        repo="acme/widgets",
        commit_sha="a" * 40,
        severity="high",
        score=7,
        blast_radius_impact_count=4,
        blast_radius_max_depth=2,
        sensitive_hits=["auth/login.py"],
        narrative="This change touches authentication.",
    )

    blocks = payload["attachments"][0]["blocks"]
    rendered = str(blocks)

    assert "acme/widgets" in rendered
    assert "a" * 40 in rendered
    assert "HIGH" in rendered
    assert "7" in rendered
    assert "4 modules, depth 2" in rendered
    assert "auth/login.py" in rendered
    assert "This change touches authentication." in rendered
    assert f"https://github.com/acme/widgets/commit/{'a' * 40}" in rendered


def test_build_review_message_severity_color():
    critical = build_review_message(
        repo="r",
        commit_sha="s",
        severity="critical",
        score=10,
        blast_radius_impact_count=1,
        blast_radius_max_depth=1,
        sensitive_hits=[],
        narrative="n",
    )
    low = build_review_message(
        repo="r",
        commit_sha="s",
        severity="low",
        score=0,
        blast_radius_impact_count=0,
        blast_radius_max_depth=0,
        sensitive_hits=[],
        narrative="n",
    )

    assert critical["attachments"][0]["color"] != low["attachments"][0]["color"]


def test_build_review_message_no_sensitive_hits_shows_none():
    payload = build_review_message(
        repo="r",
        commit_sha="s",
        severity="low",
        score=0,
        blast_radius_impact_count=0,
        blast_radius_max_depth=0,
        sensitive_hits=[],
        narrative="n",
    )
    rendered = str(payload)
    assert "None" in rendered


def test_build_review_message_respects_explicit_compare_url():
    payload = build_review_message(
        repo="r",
        commit_sha="s",
        severity="low",
        score=0,
        blast_radius_impact_count=0,
        blast_radius_max_depth=0,
        sensitive_hits=[],
        narrative="n",
        compare_url="https://example.com/compare/1",
    )
    assert "https://example.com/compare/1" in str(payload)


def test_slack_notifier_unconfigured_send_is_noop():
    notifier = SlackNotifier(webhook_url="")
    assert notifier.is_configured is False


async def test_slack_notifier_unconfigured_send_returns_true():
    notifier = SlackNotifier(webhook_url="")
    result = await notifier.send({"attachments": []})
    assert result is True


async def test_slack_notifier_send_success(httpx_mock):
    httpx_mock.add_response(url="https://hooks.slack.com/services/test", status_code=200)
    notifier = SlackNotifier(webhook_url="https://hooks.slack.com/services/test")

    result = await notifier.send({"attachments": []})

    assert result is True
    assert notifier.is_configured is True


async def test_slack_notifier_send_failure_raises_slack_error(httpx_mock):
    httpx_mock.add_response(url="https://hooks.slack.com/services/test", status_code=500)
    notifier = SlackNotifier(webhook_url="https://hooks.slack.com/services/test")

    with pytest.raises(SlackError):
        await notifier.send({"attachments": []})
