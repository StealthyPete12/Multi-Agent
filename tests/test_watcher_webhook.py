import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from agents.watcher import main as watcher_main

SECRET = "test-secret"


def _sig(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _push_body(branch: str = "main") -> bytes:
    return json.dumps(
        {
            "ref": f"refs/heads/{branch}",
            "repository": {"full_name": "acme/widgets"},
            "compare": "https://github.com/acme/widgets/compare/a...b",
            "commits": [
                {
                    "id": "a" * 40,
                    "author": {"name": "jane"},
                    "message": "fix: bug",
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "added": ["a.py"],
                    "modified": [],
                    "removed": [],
                }
            ],
        }
    ).encode()


class FakeBroker:
    """Stand-in for shared.broker.Broker so these tests don't need a real
    RabbitMQ connection — they only exercise the webhook's HTTP-facing
    behavior (signature check, event/branch filtering, extraction)."""

    def __init__(self) -> None:
        self.published: list[tuple[object, str]] = []

    async def connect(self) -> None:
        pass

    async def declare_queue(self, *args, **kwargs) -> None:
        pass

    async def publish(self, envelope, *, routing_key: str) -> None:
        self.published.append((envelope, routing_key))

    async def close(self) -> None:
        pass


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(watcher_main, "GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(watcher_main, "WATCHED_BRANCHES", ["main"])
    monkeypatch.setattr(watcher_main, "Broker", FakeBroker)

    with TestClient(watcher_main.app) as test_client:
        yield test_client


def test_rejects_missing_signature(client):
    resp = client.post("/webhook/github", content=_push_body(), headers={"X-GitHub-Event": "push"})
    assert resp.status_code == 401


def test_rejects_invalid_signature(client):
    resp = client.post(
        "/webhook/github",
        content=_push_body(),
        headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": "sha256=deadbeef"},
    )
    assert resp.status_code == 401


def test_ignores_non_push_event(client):
    body = _push_body()
    resp = client.post(
        "/webhook/github",
        content=body,
        headers={"X-GitHub-Event": "ping", "X-Hub-Signature-256": _sig(SECRET, body)},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


def test_ignores_unwatched_branch(client):
    body = _push_body(branch="feature/x")
    resp = client.post(
        "/webhook/github",
        content=body,
        headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": _sig(SECRET, body)},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


def test_accepts_valid_push_and_publishes(client):
    body = _push_body()
    resp = client.post(
        "/webhook/github",
        content=body,
        headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": _sig(SECRET, body)},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "accepted"
    assert len(data["published_event_ids"]) == 1

    broker: FakeBroker = watcher_main.app.state.broker
    assert len(broker.published) == 1
    envelope, routing_key = broker.published[0]
    assert routing_key == "commit.detected"
    assert envelope.payload.repo == "acme/widgets"
    assert envelope.trace_id == data["trace_id"]


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
