from datetime import datetime, timezone

from agents.reviewer.scoring import (
    ScoringConfig,
    compute_score,
    get_scoring_config,
    has_test_proximity,
    status_for_severity,
)
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
        changed_files=["tests/test_widget.py"],
        blast_radius=BlastRadius(impacted_modules=[], impact_count=0, max_depth=0),
        sensitive_hits=[],
        semantic_summary="",
    )
    defaults.update(overrides)
    return FindingsReady(**defaults)


# --- has_test_proximity -----------------------------------------------------


def test_has_test_proximity_true_for_test_file_changed():
    assert has_test_proximity(["tests/test_widget.py"], [])


def test_has_test_proximity_true_for_impacted_test_module():
    assert has_test_proximity(["widget.py"], ["tests.test_widget"])


def test_has_test_proximity_false_when_nothing_test_related():
    assert not has_test_proximity(["src/widget.py"], ["src.other"])


# --- compute_score scenarios -------------------------------------------------


def test_scenario_a_low_impact_commit_scores_low():
    findings = _findings(
        changed_files=["tests/test_widget.py"],
        blast_radius=BlastRadius(impacted_modules=[], impact_count=0, max_depth=0),
        sensitive_hits=[],
    )

    breakdown = compute_score(findings)

    assert breakdown.total == 0
    assert breakdown.severity == "low"


def test_scenario_b_large_blast_radius_scores_high():
    findings = _findings(
        changed_files=["tests/test_widget.py"],
        blast_radius=BlastRadius(
            impacted_modules=[f"pkg.module_{i}" for i in range(60)],
            impact_count=60,
            max_depth=9,
        ),
        sensitive_hits=[],
    )

    breakdown = compute_score(findings)

    # blast_radius (depth 9 -> 4-10 bucket -> +2) + impact_count (60 -> 51+ -> +4)
    assert breakdown.blast_radius_points == 2
    assert breakdown.impact_count_points == 4
    assert breakdown.total >= 6
    assert breakdown.severity in ("high", "critical")


def test_scenario_c_sensitive_path_escalates_severity():
    baseline = _findings(
        changed_files=["tests/test_widget.py", "src/widget.py"],
        blast_radius=BlastRadius(impacted_modules=[], impact_count=0, max_depth=0),
        sensitive_hits=[],
    )
    with_sensitive_hit = _findings(
        changed_files=["tests/test_widget.py", "auth/login.py"],
        blast_radius=BlastRadius(impacted_modules=[], impact_count=0, max_depth=0),
        sensitive_hits=["auth/login.py"],
    )

    baseline_score = compute_score(baseline)
    escalated_score = compute_score(with_sensitive_hit)

    assert escalated_score.sensitive_hits_points > baseline_score.sensitive_hits_points
    assert escalated_score.total > baseline_score.total


def test_multiple_sensitive_hits_score_higher_than_one():
    one_hit = _findings(sensitive_hits=["auth/login.py"])
    two_hits = _findings(sensitive_hits=["auth/login.py", "payments/charge.py"])

    assert compute_score(two_hits).sensitive_hits_points > compute_score(one_hit).sensitive_hits_points


def test_no_test_proximity_adds_penalty():
    no_tests = _findings(changed_files=["src/widget.py"])
    with_tests = _findings(changed_files=["tests/test_widget.py"])

    assert compute_score(no_tests).test_proximity_points == 2
    assert compute_score(with_tests).test_proximity_points == 0


def test_changed_files_bucket_thresholds():
    few = _findings(changed_files=["tests/test_a.py"] * 3)
    many = _findings(changed_files=[f"tests/test_{i}.py" for i in range(25)])

    assert compute_score(few).changed_files_points == 0
    assert compute_score(many).changed_files_points == 3


def test_compute_score_is_deterministic():
    findings = _findings(sensitive_hits=["auth/login.py"])
    first = compute_score(findings)
    second = compute_score(findings)
    assert first == second


def test_get_scoring_config_reads_env_overrides(monkeypatch):
    monkeypatch.setenv("RISK_SCORE_MODERATE_THRESHOLD", "1")
    monkeypatch.setenv("RISK_SCORE_HIGH_THRESHOLD", "2")
    monkeypatch.setenv("RISK_SCORE_CRITICAL_THRESHOLD", "3")

    cfg = get_scoring_config()

    assert cfg.moderate_threshold == 1
    assert cfg.high_threshold == 2
    assert cfg.critical_threshold == 3


def test_custom_scoring_config_changes_severity_mapping():
    # sensitive hit (+2) + no test proximity (+2) = 4 points.
    findings = _findings(changed_files=["auth/login.py"], sensitive_hits=["auth/login.py"])
    lenient = ScoringConfig(moderate_threshold=100, high_threshold=200, critical_threshold=300)

    assert compute_score(findings, config=lenient).severity == "low"
    assert compute_score(findings).severity == "moderate"


def test_status_for_severity_mapping():
    assert status_for_severity("low") == "passed"
    assert status_for_severity("moderate") == "needs_review"
    assert status_for_severity("high") == "needs_review"
    assert status_for_severity("critical") == "failed"
