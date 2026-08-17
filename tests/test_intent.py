"""Tests for the intent/actual API usage reporting protocol (intent.py)."""

import json
import os
import sys
import tempfile
from unittest.mock import patch, MagicMock

import pytest

# Ensure the project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import intent
import registry


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


class TestDeclareIntent:
    def test_valid_intent(self, app):
        resp = app.post("/api/v1/intent",
            json={
                "project_id": "test-project",
                "source_repo": "your-org/test",
                "job_id": "job-001",
                "provider": "google_places",
                "api": "places.text_search",
                "expected_calls": 500,
                "expected_cost_usd": 17.50,
                "rate_limit_rpm": 100,
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["approved"] is True
        assert "intent_id" in data
        assert data["budget_remaining_usd"] > 0

    def test_missing_project_id(self, app):
        resp = app.post("/api/v1/intent",
            json={"job_id": "job-001"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 400

    def test_unauthorized(self, app):
        resp = app.post("/api/v1/intent",
            json={"project_id": "test-project", "job_id": "job-001"},
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401

    def test_no_auth_header(self, app):
        resp = app.post("/api/v1/intent",
            json={"project_id": "test-project", "job_id": "job-001"},
        )
        assert resp.status_code == 401

    def test_intent_denied_when_budget_exceeded(self, app):
        # Report enough actuals to exceed the $100 budget
        intent_id = "int_budget_test"
        # Manually save an intent + actual that exceeds budget
        import intent as intent_mod
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        i = intent_mod.Intent(
            intent_id=intent_id,
            project_id="test-project",
            provider="gemini",
            expected_calls=10,
            expected_cost_usd=5,
            status="completed",
            created_at=now_iso,
            updated_at=now_iso,
        )
        intent_mod.save_intent(i)
        a = intent_mod.Actual(
            actual_id="act_1",
            intent_id=intent_id,
            project_id="test-project",
            actual_calls=10,
            actual_cost_usd=150.0,
            status="completed",
            created_at=now_iso,
        )
        intent_mod.save_actual(a)

        # Now declare a new intent — should be denied
        resp = app.post("/api/v1/intent",
            json={
                "project_id": "test-project",
                "job_id": "job-002",
                "provider": "gemini",
                "expected_calls": 10,
                "expected_cost_usd": 5,
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["approved"] is False


class TestReportActual:
    def test_valid_actual(self, app):
        # First declare an intent
        resp = app.post("/api/v1/intent",
            json={
                "project_id": "test-project",
                "job_id": "job-actual-001",
                "provider": "google_places",
                "expected_calls": 500,
                "expected_cost_usd": 17.50,
            },
            headers={"Authorization": "Bearer test-token"},
        )
        intent_id = resp.get_json()["intent_id"]

        # Report actual
        resp = app.post("/api/v1/actual",
            json={
                "intent_id": intent_id,
                "project_id": "test-project",
                "actual_calls": 487,
                "actual_cost_usd": 17.05,
                "status": "completed",
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["overrun_detected"] is False
        assert data["status"] == "completed"

    def test_actual_triggers_overrun_kill(self, app):
        # Declare intent with low expected calls
        resp = app.post("/api/v1/intent",
            json={
                "project_id": "test-project",
                "job_id": "job-overrun-001",
                "provider": "google_places",
                "expected_calls": 100,
                "expected_cost_usd": 3.50,
                "kill": {"type": "http_callback", "url": "http://localhost:9999/kill", "method": "POST"},
            },
            headers={"Authorization": "Bearer test-token"},
        )
        intent_id = resp.get_json()["intent_id"]

        # Report actual that exceeds 1.2x threshold (120 calls > 100 * 1.2 = 120)
        # Need > 120 to trigger
        resp = app.post("/api/v1/actual",
            json={
                "intent_id": intent_id,
                "project_id": "test-project",
                "actual_calls": 150,
                "actual_cost_usd": 5.25,
                "status": "running",
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["overrun_detected"] is True
        assert "kill_result" in data

    def test_actual_intent_not_found(self, app):
        resp = app.post("/api/v1/actual",
            json={
                "intent_id": "nonexistent",
                "project_id": "test-project",
                "actual_calls": 10,
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 404

    def test_incremental_actuals(self, app):
        # Declare intent
        resp = app.post("/api/v1/intent",
            json={
                "project_id": "test-project",
                "job_id": "job-incremental-001",
                "provider": "gemini",
                "expected_calls": 1000,
                "expected_cost_usd": 50,
            },
            headers={"Authorization": "Bearer test-token"},
        )
        intent_id = resp.get_json()["intent_id"]

        # Send incremental report 1 (cumulative: 200 calls so far)
        resp = app.post("/api/v1/actual",
            json={
                "intent_id": intent_id,
                "project_id": "test-project",
                "actual_calls": 200,
                "actual_cost_usd": 10,
                "status": "running",
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200

        # Send incremental report 2 (cumulative: 500 calls so far, not 300 more)
        resp = app.post("/api/v1/actual",
            json={
                "intent_id": intent_id,
                "project_id": "test-project",
                "actual_calls": 500,
                "actual_cost_usd": 25,
                "status": "running",
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200

        # Verify latest actual is used (not summed)
        import intent as intent_mod
        summed = intent_mod.sum_actuals_for_intent(intent_id)
        assert summed["actual_calls"] == 500
        assert summed["actual_cost_usd"] == 25.0
        assert summed["report_count"] == 2


class TestExpectedCosts:
    def test_get_expected_costs_empty(self, app):
        resp = app.get("/api/v1/expected-costs/test-project",
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["project_id"] == "test-project"
        assert data["providers"] == {}

    def test_get_expected_costs_unauthorized(self, app):
        resp = app.get("/api/v1/expected-costs/test-project",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401

    def test_get_expected_costs_no_auth_header(self, app):
        resp = app.get("/api/v1/expected-costs/test-project")
        assert resp.status_code == 401

    def test_save_and_get_expected_costs(self, yaml_backend):
        import intent as intent_mod
        ec = intent_mod.ExpectedCost(
            project_id="test-project",
            provider="google_places",
            unit_cost_usd=0.035,
            free_tier_remaining_calls=6250,
            expected_remaining_monthly_usd=45.0,
        )
        intent_mod.save_expected_cost(ec)

        costs = intent_mod.list_expected_costs(project_id="test-project")
        assert len(costs) == 1
        assert costs[0].provider == "google_places"
        assert costs[0].unit_cost_usd == 0.035


class TestListIntents:
    def test_list_all_intents(self, app):
        # Declare two intents
        for i in range(2):
            app.post("/api/v1/intent",
                json={
                    "project_id": "test-project",
                    "job_id": f"job-list-{i}",
                    "provider": "gemini",
                    "expected_calls": 100,
                    "expected_cost_usd": 5,
                },
                headers={"Authorization": "Bearer test-token"},
            )
        resp = app.get("/api/v1/intents")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["count"] >= 2

    def test_list_intents_by_project(self, app):
        resp = app.get("/api/v1/intents/test-project",
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert all(i["project_id"] == "test-project" for i in data["intents"])

    def test_list_intents_by_project_unauthorized(self, app):
        resp = app.get("/api/v1/intents/test-project",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401

    def test_list_intents_by_project_no_auth_header(self, app):
        resp = app.get("/api/v1/intents/test-project")
        assert resp.status_code == 401


class TestManualKill:
    def test_manual_kill(self, app):
        # Declare an intent with a kill descriptor
        resp = app.post("/api/v1/intent",
            json={
                "project_id": "test-project",
                "job_id": "job-kill-001",
                "provider": "google_places",
                "expected_calls": 100,
                "expected_cost_usd": 3.50,
                "kill": {"type": "http_callback", "url": "http://localhost:9999/kill", "method": "POST"},
            },
            headers={"Authorization": "Bearer test-token"},
        )
        intent_id = resp.get_json()["intent_id"]

        # Manual kill (with auth)
        resp = app.post(f"/api/v1/kill/{intent_id}",
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "kill_id" in data

    def test_manual_kill_unauthorized(self, app):
        # Declare an intent
        resp = app.post("/api/v1/intent",
            json={
                "project_id": "test-project",
                "job_id": "job-kill-unauth",
                "provider": "google_places",
                "expected_calls": 100,
                "expected_cost_usd": 3.50,
                "kill": {"type": "http_callback", "url": "http://localhost:9999/kill", "method": "POST"},
            },
            headers={"Authorization": "Bearer test-token"},
        )
        intent_id = resp.get_json()["intent_id"]

        # Manual kill without auth — should be 401
        resp = app.post(f"/api/v1/kill/{intent_id}")
        assert resp.status_code == 401

    def test_manual_kill_not_found(self, app):
        resp = app.post("/api/v1/kill/nonexistent",
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 404
