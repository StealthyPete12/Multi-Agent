"""LLM-based semantic summary generation for the researcher.

Strictly technical-analysis + summarization, never risk judgment: this
module produces a short natural-language description of *what a commit
changed*, using a small/cheap model. It has no opinion on severity — that
decision stays entirely inside ``agents/reviewer/scoring.py``.

Degrades to an empty string (never raises) when no LLM provider is
configured or the call fails, so the researcher's pipeline never blocks
on LLM availability — matching how the rest of Phase 2 behaves when an
optional dependency (e.g. a stale cache) is unavailable.
"""

from __future__ import annotations

from shared.llm import LLMClient, LLMError
from shared.logging import configure_logging

__all__ = ["generate_semantic_summary", "SUMMARY_MAX_TOKENS", "SUMMARY_SYSTEM_PROMPT"]

log = configure_logging(service_name="researcher")

SUMMARY_MAX_TOKENS = 300

SUMMARY_SYSTEM_PROMPT = (
    "You summarize a single git commit for a code-review pipeline. "
    "Write 2-3 concise sentences describing what changed and why, based "
    "only on the commit message, changed file list, and diff excerpt "
    "given. Do not assess risk, severity, or quality — that is handled "
    "elsewhere. If the diff excerpt is empty, summarize from the commit "
    "message and file list alone."
)


def _build_prompt(commit_message: str, changed_files: list[str], diff_excerpt: str) -> str:
    files_block = "\n".join(f"- {f}" for f in changed_files) or "(none listed)"
    diff_block = diff_excerpt.strip() or "(no diff available)"
    return (
        f"Commit message:\n{commit_message}\n\n"
        f"Changed files:\n{files_block}\n\n"
        f"Diff excerpt (may be truncated):\n{diff_block}"
    )


async def generate_semantic_summary(
    llm_client: LLMClient,
    *,
    commit_message: str,
    changed_files: list[str],
    diff_excerpt: str,
) -> str:
    """Return a 2-3 sentence summary, or ``""`` if the LLM is unavailable
    or the call fails."""
    prompt = _build_prompt(commit_message, changed_files, diff_excerpt)
    try:
        response = await llm_client.complete(
            system=SUMMARY_SYSTEM_PROMPT,
            prompt=prompt,
            max_tokens=SUMMARY_MAX_TOKENS,
        )
    except LLMError as exc:
        log.warning("semantic summary generation skipped", extra={"reason": str(exc)})
        return ""
    return response.text.strip()
