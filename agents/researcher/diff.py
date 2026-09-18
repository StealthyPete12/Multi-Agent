"""Best-effort truncated diff extraction for the researcher's semantic
summary step.

Kept separate from ``repository.py`` (which owns clone/fetch/checkout) —
this module only reads from an already-checked-out working tree via
``git show``, never mutates the checkout. A repository cloned with
``--depth 1`` has no parent commit object for its tip, so ``git show``
degrades to presenting the commit's files as if newly added rather than
erroring; that's an accepted approximation (see Known limitations in
PHASE_3_REPORT.md), not a bug to work around here.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

__all__ = ["get_truncated_diff", "DEFAULT_DIFF_MAX_CHARS"]

DEFAULT_DIFF_MAX_CHARS = 4000


async def get_truncated_diff(
    repo_path: Path,
    commit_sha: str,
    changed_files: list[str],
    *,
    max_chars: int = DEFAULT_DIFF_MAX_CHARS,
) -> str:
    """Return a size-bounded unified diff for ``commit_sha``, scoped to
    ``changed_files`` when any were given.

    Returns ``""`` (never raises) if git fails for any reason — a shallow
    clone missing history, a bad SHA, no git binary, etc. Semantic-summary
    generation is best-effort context, not a pipeline-critical path.
    """
    args = [
        "git",
        "show",
        "--no-color",
        "--unified=0",
        "--format=",
        commit_sha,
    ]
    if changed_files:
        args += ["--", *changed_files]

    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(repo_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10)
    except (OSError, asyncio.TimeoutError):
        return ""

    if process.returncode != 0:
        return ""

    diff = stdout.decode("utf-8", errors="replace")
    if len(diff) > max_chars:
        diff = diff[:max_chars] + "\n... (truncated)"
    return diff
