"""Deterministic, model-free risk scoring for a reviewed commit.

This is pure code — no LLM call ever influences a score or severity. The
LLM (see ``prompts.py``/``main.py``) only explains a score that has
already been computed here; if this module and the model narrative ever
disagree, this module is the one that's correct by construction.

Inputs (all already present on ``FindingsReady``, nothing here queries
Postgres or an LLM):

1. Blast radius        -> ``blast_radius.max_depth``
2. Impacted modules     -> ``blast_radius.impact_count``
3. Sensitive path hits  -> ``len(sensitive_hits)``
4. Changed files        -> ``len(changed_files)``
5. Test proximity       -> heuristic path-marker match over changed files
   and impacted modules (Reviewer has no repository checkout of its own,
   so this can't be a real coverage check — see PHASE_3_REPORT.md).

Each input maps to a small integer via a configurable bucket table; the
sum is compared against configurable severity thresholds. Every default
matches the Phase 3 roadmap's suggested weighting exactly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from shared.contracts import FindingsReady

__all__ = [
    "Severity",
    "ScoreBreakdown",
    "ScoringConfig",
    "DEFAULT_TEST_PATH_MARKERS",
    "has_test_proximity",
    "compute_score",
    "get_scoring_config",
    "status_for_severity",
]

Severity = Literal["low", "moderate", "high", "critical"]

# Deterministic mapping from risk severity to the ReviewCompleted.status
# enum: low commits pass automatically, moderate/high need a human look,
# and critical is treated as a hard stop, mirroring how a CI gate would
# react to each tier.
_STATUS_BY_SEVERITY: dict[Severity, Literal["passed", "failed", "needs_review"]] = {
    "low": "passed",
    "moderate": "needs_review",
    "high": "needs_review",
    "critical": "failed",
}


def status_for_severity(severity: Severity) -> Literal["passed", "failed", "needs_review"]:
    return _STATUS_BY_SEVERITY[severity]

DEFAULT_TEST_PATH_MARKERS: tuple[str, ...] = ("test_", "_test.py", "tests/", "/test/", "spec/")


@dataclass(frozen=True)
class ScoreBreakdown:
    """One component score per input, plus the total and derived severity.

    Kept as a dataclass (not just an int) so the Reviewer can persist and
    the LLM narrative can cite *why* a score is what it is, without
    re-deriving anything.
    """

    blast_radius_points: int
    impact_count_points: int
    sensitive_hits_points: int
    changed_files_points: int
    test_proximity_points: int
    total: int
    severity: Severity


@dataclass(frozen=True)
class ScoringConfig:
    """Bucket boundaries and severity thresholds, overridable via env vars
    for the thresholds (the numbers most likely to need tuning in
    production) while keeping the roadmap's bucket boundaries as sensible
    defaults.
    """

    # (upper_bound_inclusive, points) pairs, checked in order; the last
    # entry's upper_bound is effectively "infinity" (None).
    blast_radius_buckets: tuple[tuple[int | None, int], ...] = ((3, 0), (10, 2), (None, 4))
    impact_count_buckets: tuple[tuple[int | None, int], ...] = ((10, 0), (50, 2), (None, 4))
    changed_files_buckets: tuple[tuple[int | None, int], ...] = ((5, 0), (20, 1), (None, 3))
    sensitive_hits_none: int = 0
    sensitive_hits_one: int = 2
    sensitive_hits_multiple: int = 4
    test_proximity_penalty: int = 2
    test_path_markers: tuple[str, ...] = DEFAULT_TEST_PATH_MARKERS

    moderate_threshold: int = 3
    high_threshold: int = 6
    critical_threshold: int = 10


def get_scoring_config() -> ScoringConfig:
    """Build a :class:`ScoringConfig` with severity thresholds read from
    the environment, falling back to the roadmap's defaults."""
    return ScoringConfig(
        moderate_threshold=int(os.environ.get("RISK_SCORE_MODERATE_THRESHOLD", 3)),
        high_threshold=int(os.environ.get("RISK_SCORE_HIGH_THRESHOLD", 6)),
        critical_threshold=int(os.environ.get("RISK_SCORE_CRITICAL_THRESHOLD", 10)),
    )


def _bucket_points(value: int, buckets: tuple[tuple[int | None, int], ...]) -> int:
    for upper_bound, points in buckets:
        if upper_bound is None or value <= upper_bound:
            return points
    return buckets[-1][1]


def has_test_proximity(
    changed_files: list[str],
    impacted_modules: list[str],
    *,
    markers: tuple[str, ...] = DEFAULT_TEST_PATH_MARKERS,
) -> bool:
    """Heuristic: does anything in the changed-file set or the blast
    radius look test-related?

    Not a real coverage check (the Reviewer never checks out the repo) —
    a deliberately simple, deterministic, configurable proxy. See the
    module docstring.
    """
    candidates = [p.replace("\\", "/").lower() for p in (*changed_files, *impacted_modules)]
    return any(marker in path for path in candidates for marker in markers)


def compute_score(
    findings: FindingsReady, *, config: ScoringConfig | None = None
) -> ScoreBreakdown:
    """Deterministically score one ``FindingsReady`` payload.

    Same input always produces the same output — no randomness, no
    external call, no model in the loop.
    """
    cfg = config or get_scoring_config()

    blast_radius_points = _bucket_points(findings.blast_radius.max_depth, cfg.blast_radius_buckets)
    impact_count_points = _bucket_points(findings.blast_radius.impact_count, cfg.impact_count_buckets)
    changed_files_points = _bucket_points(len(findings.changed_files), cfg.changed_files_buckets)

    hits = len(findings.sensitive_hits)
    if hits == 0:
        sensitive_hits_points = cfg.sensitive_hits_none
    elif hits == 1:
        sensitive_hits_points = cfg.sensitive_hits_one
    else:
        sensitive_hits_points = cfg.sensitive_hits_multiple

    proximate = has_test_proximity(
        findings.changed_files, findings.blast_radius.impacted_modules, markers=cfg.test_path_markers
    )
    test_proximity_points = 0 if proximate else cfg.test_proximity_penalty

    total = (
        blast_radius_points
        + impact_count_points
        + sensitive_hits_points
        + changed_files_points
        + test_proximity_points
    )

    if total >= cfg.critical_threshold:
        severity: Severity = "critical"
    elif total >= cfg.high_threshold:
        severity = "high"
    elif total >= cfg.moderate_threshold:
        severity = "moderate"
    else:
        severity = "low"

    return ScoreBreakdown(
        blast_radius_points=blast_radius_points,
        impact_count_points=impact_count_points,
        sensitive_hits_points=sensitive_hits_points,
        changed_files_points=changed_files_points,
        test_proximity_points=test_proximity_points,
        total=total,
        severity=severity,
    )
