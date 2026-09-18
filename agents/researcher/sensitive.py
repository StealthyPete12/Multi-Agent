"""Sensitive-path detection for changed files.

Flags changed files that fall under configurable "sensitive" path
prefixes/substrings (auth, payments, infra, migrations, ...) so a
downstream reviewer/human can prioritize review of commits touching
high-risk areas, without any AI/scoring logic in Phase 2.
"""

from __future__ import annotations

import os

__all__ = ["DEFAULT_SENSITIVE_PATTERNS", "get_sensitive_patterns", "detect_sensitive_hits"]

DEFAULT_SENSITIVE_PATTERNS: tuple[str, ...] = (
    "auth/",
    "payments/",
    "infra/",
    "migrations/",
)


def get_sensitive_patterns() -> list[str]:
    """Read patterns from ``SENSITIVE_PATH_PATTERNS`` (comma-separated),
    falling back to :data:`DEFAULT_SENSITIVE_PATTERNS`."""
    raw = os.environ.get("SENSITIVE_PATH_PATTERNS")
    if raw:
        return [p.strip() for p in raw.split(",") if p.strip()]
    return list(DEFAULT_SENSITIVE_PATTERNS)


def detect_sensitive_hits(
    changed_files: list[str], patterns: list[str] | None = None
) -> list[str]:
    """Return the subset of ``changed_files`` whose path contains any of
    ``patterns`` (substring match, forward-slash normalized)."""
    active_patterns = patterns if patterns is not None else get_sensitive_patterns()
    hits: list[str] = []
    for changed_file in changed_files:
        normalized = changed_file.replace("\\", "/")
        if any(pattern in normalized for pattern in active_patterns):
            hits.append(changed_file)
    return hits
