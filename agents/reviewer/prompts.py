"""Prompt construction for the Reviewer's narrative-generation LLM call.

The model only ever explains a decision that ``scoring.py`` already made
deterministically — it receives the finished score/severity as fact, not
as something to (re)compute. Nothing here lets the model override or
second-guess the score.
"""

from __future__ import annotations

from agents.reviewer.scoring import ScoreBreakdown
from shared.contracts import FindingsReady

__all__ = [
    "NARRATIVE_MAX_TOKENS",
    "NARRATIVE_SYSTEM_PROMPT",
    "build_narrative_prompt",
    "build_fallback_narrative",
]

NARRATIVE_MAX_TOKENS = 500

NARRATIVE_SYSTEM_PROMPT = (
    "You are a senior code reviewer writing a concise, evidence-based "
    "executive explanation for a teammate about to review a commit. "
    "You are given a risk severity and score that have ALREADY been "
    "computed by deterministic rules — never state a different severity "
    "or score, never imply the score is wrong, and never invent numbers "
    "not given to you. Write in plain, professional prose (no headers, "
    "no markdown), covering: (1) why the score is what it is, citing the "
    "specific factors given; (2) what systems/modules may be affected; "
    "(3) suggested focus areas for the human reviewer. Keep it readable "
    "and concise — a few short paragraphs at most."
)


def build_narrative_prompt(findings: FindingsReady, breakdown: ScoreBreakdown) -> str:
    impacted_preview = ", ".join(findings.blast_radius.impacted_modules[:15]) or "(none)"
    sensitive_preview = ", ".join(findings.sensitive_hits) or "(none)"

    return (
        f"Repository: {findings.repo}\n"
        f"Commit: {findings.commit_sha}\n"
        f"Severity: {breakdown.severity.upper()}\n"
        f"Total risk score: {breakdown.total}\n\n"
        "Score breakdown (deterministic, already final):\n"
        f"- Blast radius depth points: {breakdown.blast_radius_points}\n"
        f"- Impacted module count points: {breakdown.impact_count_points}\n"
        f"- Sensitive path points: {breakdown.sensitive_hits_points}\n"
        f"- Changed file count points: {breakdown.changed_files_points}\n"
        f"- Test proximity points: {breakdown.test_proximity_points}\n\n"
        "Blast radius data:\n"
        f"- Max depth reached: {findings.blast_radius.max_depth}\n"
        f"- Impact count: {findings.blast_radius.impact_count}\n"
        f"- Impacted modules (sample): {impacted_preview}\n\n"
        f"Sensitive path hits: {sensitive_preview}\n\n"
        f"Changed files ({len(findings.changed_files)}): "
        f"{', '.join(findings.changed_files) or '(none)'}\n\n"
        f"Semantic summary of the commit (from an earlier, smaller model):\n"
        f"{findings.semantic_summary or '(not available)'}\n"
    )


def build_fallback_narrative(findings: FindingsReady, breakdown: ScoreBreakdown) -> str:
    """Deterministic, template-based narrative used when no LLM provider
    is configured or the call fails — so a review is never missing an
    explanation just because the model was unavailable.

    Same evidence, same structure as the model prompt asks for; just no
    prose polish.
    """
    reasons: list[str] = []
    if breakdown.blast_radius_points:
        reasons.append(f"blast radius reached depth {findings.blast_radius.max_depth}")
    if breakdown.impact_count_points:
        reasons.append(f"{findings.blast_radius.impact_count} modules impacted")
    if breakdown.sensitive_hits_points:
        reasons.append(f"{len(findings.sensitive_hits)} sensitive path hit(s)")
    if breakdown.changed_files_points:
        reasons.append(f"{len(findings.changed_files)} files changed")
    if breakdown.test_proximity_points:
        reasons.append("no test proximity detected near the changed files")
    reason_text = "; ".join(reasons) if reasons else "no elevated-risk factors were detected"

    return (
        f"Severity {breakdown.severity.upper()} (score {breakdown.total}) for "
        f"{findings.repo}@{findings.commit_sha}: {reason_text}. "
        f"Impacted modules: {', '.join(findings.blast_radius.impacted_modules[:15]) or '(none)'}. "
        f"Sensitive paths: {', '.join(findings.sensitive_hits) or '(none)'}. "
        "Suggested focus: review the impacted modules above and, if any sensitive "
        "paths are listed, verify access-control and data-handling changes closely. "
        "(Generated without an LLM narrative — no provider was available.)"
    )
