import pytest

from agents.researcher.sensitive import detect_sensitive_hits, get_sensitive_patterns


def test_default_patterns_flag_auth_and_payments():
    changed = ["src/auth/login.py", "src/payments/charge.py", "src/widgets/core.py"]

    hits = detect_sensitive_hits(changed)

    assert hits == ["src/auth/login.py", "src/payments/charge.py"]


def test_no_hits_when_nothing_sensitive_changed():
    changed = ["src/widgets/core.py", "README.md"]

    assert detect_sensitive_hits(changed) == []


def test_migrations_and_infra_patterns():
    changed = ["db/migrations/002_add_col.sql", "infra/terraform/main.tf"]

    hits = detect_sensitive_hits(changed)

    assert hits == changed


def test_windows_style_paths_are_normalized():
    changed = ["src\\auth\\login.py"]

    assert detect_sensitive_hits(changed) == changed


def test_custom_patterns_override_defaults(monkeypatch: pytest.MonkeyPatch):
    hits = detect_sensitive_hits(["billing/invoice.py", "auth/login.py"], patterns=["billing/"])

    assert hits == ["billing/invoice.py"]


def test_env_var_overrides_default_patterns(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SENSITIVE_PATH_PATTERNS", "billing/, secrets/")

    patterns = get_sensitive_patterns()

    assert patterns == ["billing/", "secrets/"]


def test_env_var_absent_falls_back_to_defaults(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SENSITIVE_PATH_PATTERNS", raising=False)

    patterns = get_sensitive_patterns()

    assert "auth/" in patterns
    assert "payments/" in patterns
