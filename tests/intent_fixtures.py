"""Shared fixtures for the intent/actual protocol tests.

Split out of ``test_intent.py`` during the ≤250-line file refactor —
pure structural move, no behavior change. Consumer modules import these
fixtures by name (``from intent_fixtures import yaml_backend, app``);
``test_intent.py`` re-exports them so the long-standing
``from test_intent import yaml_backend, app`` in test_ciso_findings.py
and test_intent_sev_fixes.py keeps working.
"""

import os
import sys

import pytest

# Ensure the project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import intent  # noqa: E402
import registry  # noqa: E402


@pytest.fixture
def yaml_backend(tmp_path, monkeypatch):
    """Use YAML file backend in a temp directory."""
    accounts_file = tmp_path / "accounts.yaml"
    intents_file = str(tmp_path / "api_intents.yaml")
    actuals_file = str(tmp_path / "api_actuals.yaml")
    expected_costs_file = str(tmp_path / "expected_costs.yaml")
    kill_events_file = str(tmp_path / "kill_events.yaml")

    monkeypatch.setenv("USE_FIRESTORE", "false")
    monkeypatch.setenv("ACCOUNTS_FILE", str(accounts_file))
    monkeypatch.setenv("INTENTS_FILE", intents_file)
    monkeypatch.setenv("ACTUALS_FILE", actuals_file)
    monkeypatch.setenv("EXPECTED_COSTS_FILE", expected_costs_file)
    monkeypatch.setenv("KILL_EVENTS_FILE", kill_events_file)
    monkeypatch.setenv("CLOUDMANAGEMENT_REPORT_TOKEN", "test-token")

    # Re-import to pick up env changes
    import importlib
    importlib.reload(registry)
    importlib.reload(intent)

    # Register a test account
    acct = registry.Account(
        project_id="test-project",
        billing_account_id="01AB-23CD-EF45",
        owner_email="test@example.com",
        budget_amount_usd=100.0,
        quota_rpm_cap=6000,
        report_token_secret="CLOUDMANAGEMENT_REPORT_TOKEN",
    )
    registry.register_account(acct)

    return intents_file


@pytest.fixture
def app(yaml_backend):
    """Create a Flask test client with the intent blueprint."""
    import main
    main.app.config["TESTING"] = True
    return main.app.test_client()
