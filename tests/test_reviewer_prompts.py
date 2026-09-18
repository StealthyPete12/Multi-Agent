from datetime import datetime, timezone

from agents.reviewer.prompts import build_fallback_narrative, build_narrative_prompt
from agents.reviewer.scoring import compute_score
from shared.contracts import BlastRadius, FindingsReady


def _findings(**overrides) -> FindingsReady:
    now = datetime.now(timezone.utc)
    defaults = dict(
        commit_sha="a" * 40,
        agent_name="researcher",
        findings=[],
        started_at=now,
        completed_at=now,
        repo="acme/widgets",
        changed_files=["auth/login.py"],
        blast_radius=BlastRadius(impacted_modules=["auth.login", "checkout"], impact_count=2, max_depth=2),
        sensitive_hits=["auth/login.py"],
        semantic_summary="Refactors login validation.",
    )
    defaults.update(overrides)
    return FindingsReady(**defaults)


def test_build_narrative_prompt_includes_all_required_evidence():
    findings = _findings()
    breakdown = compute_score(findings)

    prompt = build_narrative_prompt(findings, breakdown)

    assert findings.repo in prompt
    assert findings.commit_sha in prompt
    assert breakdown.severity.upper() in prompt
    assert str(breakdown.total) in prompt
    assert "auth.login" in prompt
    assert "auth/login.py" in prompt
    assert "Refactors login validation." in prompt


def test_build_narrative_prompt_handles_empty_optional_fields():
    findings = _findings(
        changed_files=[], sensitive_hits=[], semantic_summary="",
        blast_radius=BlastRadius(impacted_modules=[], impact_count=0, max_depth=0),
    )
    breakdown = compute_score(findings)

    prompt = build_narrative_prompt(findings, breakdown)

    assert "(none)" in prompt
    assert "(not available)" in prompt


def test_build_fallback_narrative_cites_score_and_severity():
    findings = _findings()
    breakdown = compute_score(findings)

    narrative = build_fallback_narrative(findings, breakdown)

    assert breakdown.severity.upper() in narrative
    assert str(breakdown.total) in narrative
    assert findings.repo in narrative
    assert findings.commit_sha in narrative
    assert "sensitive path hit" in narrative


def test_build_fallback_narrative_low_risk_has_no_elevated_reasons():
    findings = _findings(
        changed_files=["tests/test_widget.py"],
        sensitive_hits=[],
        blast_radius=BlastRadius(impacted_modules=[], impact_count=0, max_depth=0),
    )
    breakdown = compute_score(findings)

    narrative = build_fallback_narrative(findings, breakdown)

    assert "no elevated-risk factors" in narrative
