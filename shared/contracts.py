"""Versioned message contracts for the event-driven code review swarm.

Every message that crosses a RabbitMQ queue is a JSON-serialized
:class:`Envelope` wrapping one typed, versioned payload:

- :class:`CommitDetected`   — emitted by the ingestion service when a new
  commit is observed on a watched repository.
- :class:`FindingsReady`    — emitted by an analysis agent once it has
  finished reviewing a commit and produced zero or more findings.
- :class:`ReviewCompleted`  — emitted by the aggregator once all findings
  for a commit have been collected and a final report was generated.

All models are strict (unknown fields are rejected, types are not
silently coerced) and carry an explicit ``schema_version`` so that
consumers can detect and handle contract drift across deployments.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, ClassVar, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "EventType",
    "ContractModel",
    "Finding",
    "BlastRadius",
    "CommitDetected",
    "FindingsReady",
    "ReviewCompleted",
    "Envelope",
    "make_envelope",
    "parse_envelope",
    "PAYLOAD_REGISTRY",
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


class EventType(str, Enum):
    """Discriminator carried on every envelope, used for routing keys and
    for picking the correct payload model when deserializing."""

    COMMIT_DETECTED = "commit.detected"
    FINDINGS_READY = "findings.ready"
    REVIEW_COMPLETED = "review.completed"


class ContractModel(BaseModel):
    """Base class for every payload contract.

    ``strict=True`` disables type coercion (e.g. ``"3"`` is no longer a
    valid ``int``) and ``extra="forbid"`` rejects unknown fields, so a
    producer/consumer version mismatch fails loudly instead of silently
    dropping data.
    """

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class Finding(ContractModel):
    """A single issue raised by an analysis agent about a commit."""

    file_path: str
    line_number: int | None = None
    severity: Literal["info", "low", "medium", "high", "critical"]
    category: str
    message: str
    agent_name: str
    details: dict[str, Any] = Field(default_factory=dict)


class CommitDetected(ContractModel):
    """Published by the ingestion service when a commit needs review."""

    SCHEMA_VERSION: ClassVar[str] = "1.0"

    schema_version: Literal["1.0"] = "1.0"
    repo: str
    commit_sha: str
    branch: str
    author: str
    message: str
    committed_at: datetime
    changed_files: list[str] = Field(default_factory=list)


class BlastRadius(ContractModel):
    """Result of a repository-analysis agent's dependency-impact traversal
    for the commit's changed files (see ``agents/researcher/impact.py``)."""

    impacted_modules: list[str] = Field(default_factory=list)
    impact_count: int = Field(ge=0, default=0)
    max_depth: int = Field(ge=0, default=0)


class FindingsReady(ContractModel):
    """Published by an analysis agent once it has finished reviewing a
    commit, whether or not it produced any findings.

    Phase 2 adds repository-intelligence fields (``repo``, ``changed_files``,
    ``blast_radius``, ``sensitive_hits``) alongside the original Phase 1
    fields. ``semantic_summary`` is reserved for a future LLM-based agent —
    Phase 2 always publishes it as ``""``.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0"

    schema_version: Literal["1.0"] = "1.0"
    commit_sha: str
    agent_name: str
    findings: list[Finding] = Field(default_factory=list)
    started_at: datetime
    completed_at: datetime
    repo: str
    changed_files: list[str] = Field(default_factory=list)
    blast_radius: BlastRadius = Field(default_factory=BlastRadius)
    sensitive_hits: list[str] = Field(default_factory=list)
    semantic_summary: str = ""


class ReviewCompleted(ContractModel):
    """Published by the reviewer once a commit's findings have been scored,
    narrated, persisted, and (if configured) posted to Slack.

    ``severity``/``score`` are the deterministic risk-scoring output from
    ``agents/reviewer/scoring.py`` — never LLM-influenced (see that
    module's docstring). ``status`` is a pure function of ``severity``
    (``agents/reviewer/scoring.py::status_for_severity``).
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0"

    schema_version: Literal["1.0"] = "1.0"
    commit_sha: str
    repo: str
    report_id: str
    status: Literal["passed", "failed", "needs_review"]
    severity: Literal["low", "moderate", "high", "critical"]
    score: int = Field(ge=0)
    summary: str
    total_findings: int = Field(ge=0)
    completed_at: datetime


PayloadT = TypeVar("PayloadT", bound=ContractModel)

# Maps each EventType to the payload model that carries it. Kept next to
# the models so `parse_envelope` can dispatch without the caller having to
# know the payload type up front (e.g. a consumer bound to a fanout queue
# that carries more than one event type).
PAYLOAD_REGISTRY: dict[EventType, type[ContractModel]] = {
    EventType.COMMIT_DETECTED: CommitDetected,
    EventType.FINDINGS_READY: FindingsReady,
    EventType.REVIEW_COMPLETED: ReviewCompleted,
}


class Envelope(BaseModel, Generic[PayloadT]):
    """Transport wrapper placed around every payload contract.

    Carries routing/tracing metadata that is identical across all event
    types, so agents can log and correlate messages without knowing the
    payload shape.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    event_id: str = Field(default_factory=_new_id)
    event_type: EventType
    schema_version: str = "1.0"
    trace_id: str = Field(default_factory=_new_id)
    correlation_id: str | None = None
    source: str
    occurred_at: datetime = Field(default_factory=_utcnow)
    payload: PayloadT

    def to_json(self) -> str:
        """Serialize to a JSON string ready to publish as a message body."""
        return self.model_dump_json()

    def to_bytes(self) -> bytes:
        """Serialize to UTF-8 bytes, the form RabbitMQ message bodies take."""
        return self.to_json().encode("utf-8")

    @classmethod
    def from_json(cls, data: str | bytes) -> "Envelope[PayloadT]":
        """Deserialize a JSON string/bytes into this concrete envelope type.

        Use on a parametrized alias, e.g.::

            Envelope[CommitDetected].from_json(body)
        """
        return cls.model_validate_json(data)


def make_envelope(
    payload: PayloadT,
    *,
    event_type: EventType,
    source: str,
    trace_id: str | None = None,
    correlation_id: str | None = None,
) -> Envelope[PayloadT]:
    """Convenience constructor for producers.

    Fills in ``event_id``/``occurred_at`` and generates a ``trace_id`` when
    one isn't supplied (e.g. the first hop of a new pipeline run).
    """

    return Envelope[type(payload)](  # type: ignore[valid-type]
        event_type=event_type,
        source=source,
        trace_id=trace_id or _new_id(),
        correlation_id=correlation_id,
        payload=payload,
    )


def parse_envelope(data: str | bytes) -> Envelope[ContractModel]:
    """Deserialize an envelope without knowing its payload type ahead of
    time, by peeking at ``event_type`` and looking up the registered
    payload model.

    Raises ``ValueError`` if ``event_type`` is missing or unregistered, or
    ``pydantic.ValidationError`` if the payload fails validation.
    """

    raw = json.loads(data)
    raw_event_type = raw.get("event_type")
    try:
        event_type = EventType(raw_event_type)
    except ValueError as exc:
        raise ValueError(f"unknown event_type: {raw_event_type!r}") from exc

    payload_cls = PAYLOAD_REGISTRY[event_type]
    # Re-validate from the original JSON text (not the parsed dict) so
    # strict mode still applies JSON-mode coercion for wire types like
    # datetime/enum strings, which python-mode `model_validate` rejects.
    return Envelope[payload_cls].model_validate_json(data)  # type: ignore[valid-type]
