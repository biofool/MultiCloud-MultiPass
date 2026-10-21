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


# ---------------------------------------------------------------------------
# Issue #1: new endpoints required by cloud_management_client v0.12.0
# ---------------------------------------------------------------------------

class TestBudgetEndpoint:
    def test_get_budget_authorized(self, app):
        resp = app.get("/api/v1/budget/test-project",
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["budget_configured"] is True
        assert data["budget_amount_usd"] == 100.0
        assert data["spent_this_month_usd"] == 0
        assert data["over_budget"] is False

    def test_get_budget_unauthorized(self, app):
        resp = app.get("/api/v1/budget/test-project",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401

    def test_post_budget_admit(self, app):
        resp = app.post("/api/v1/budget/test-project",
            json={"expected_cost_usd": 10.0},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["admit"] is True
        assert data["deferred"] is False

    def test_post_budget_deferred(self, app):
        # Spend close to budget first
        import intent as intent_mod
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        i = intent_mod.Intent(
            intent_id="int_budget_defer",
            project_id="test-project",
            expected_calls=10,
            expected_cost_usd=5,
            created_at=now_iso,
            updated_at=now_iso,
        )
        intent_mod.save_intent(i)
        a = intent_mod.Actual(
            actual_id="act_defer",
            intent_id="int_budget_defer",
            project_id="test-project",
            actual_calls=10,
            actual_cost_usd=95.0,
            created_at=now_iso,
        )
        intent_mod.save_actual(a)

        # Requesting $10 more should defer (95 + 10 = 105 > 100)
        resp = app.post("/api/v1/budget/test-project",
            json={"expected_cost_usd": 10.0},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["admit"] is False
        assert data["deferred"] is True

    def test_post_budget_denied_over_budget(self, app):
        # Spend over budget
        import intent as intent_mod
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        i = intent_mod.Intent(
            intent_id="int_budget_over",
            project_id="test-project",
            expected_calls=10,
            expected_cost_usd=5,
            created_at=now_iso,
            updated_at=now_iso,
        )
        intent_mod.save_intent(i)
        a = intent_mod.Actual(
            actual_id="act_over",
            intent_id="int_budget_over",
            project_id="test-project",
            actual_calls=10,
            actual_cost_usd=150.0,
            created_at=now_iso,
        )
        intent_mod.save_actual(a)

        resp = app.post("/api/v1/budget/test-project",
            json={"expected_cost_usd": 1.0},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["admit"] is False
        assert data["deferred"] is False


class TestGetSingleIntent:
    def test_get_intent_found(self, app):
        # Declare an intent
        resp = app.post("/api/v1/intent",
            json={"project_id": "test-project", "job_id": "job-get-001",
                  "provider": "gemini", "expected_calls": 100, "expected_cost_usd": 5},
            headers={"Authorization": "Bearer test-token"},
        )
        intent_id = resp.get_json()["intent_id"]

        resp = app.get(f"/api/v1/intent/{intent_id}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["intent_id"] == intent_id
        assert data["job_id"] == "job-get-001"

    def test_get_intent_not_found(self, app):
        resp = app.get("/api/v1/intent/nonexistent")
        assert resp.status_code == 404


class TestKillOrders:
    def test_list_kill_orders_empty(self, app):
        resp = app.get("/api/v1/kill-orders?project_id=test-project",
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["kill_orders"] == []

    def test_list_kill_orders_missing_project(self, app):
        resp = app.get("/api/v1/kill-orders",
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 400

    def test_list_kill_orders_unauthorized(self, app):
        resp = app.get("/api/v1/kill-orders?project_id=test-project",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401


class TestReportExposure:
    def test_report_exposure(self, app):
        resp = app.post("/api/v1/exposure",
            json={"project_id": "test-project", "display_name": "GOOGLE_API_KEY", "dry_run": True},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["acknowledged"] is True
        assert data["rotated"] is False  # not yet implemented

    def test_report_exposure_missing_project(self, app):
        resp = app.post("/api/v1/exposure",
            json={"display_name": "GOOGLE_API_KEY"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 400

    def test_report_exposure_unauthorized(self, app):
        resp = app.post("/api/v1/exposure",
            json={"project_id": "test-project"},
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401


class TestClientSeqEnforcement:
    """Issue #1 part 2: client_seq should be enforced when picking the latest actual."""

    def test_client_seq_picks_latest(self, yaml_backend):
        import intent as intent_mod
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()

        # Save an intent
        i = intent_mod.Intent(
            intent_id="int_seq_test",
            project_id="test-project",
            expected_calls=1000,
            expected_cost_usd=50,
            created_at=now_iso,
            updated_at=now_iso,
        )
        intent_mod.save_intent(i)

        # Save actuals with different client_seq values — the one with
        # the highest client_seq should win, even if it's not the last
        # one saved (simulating a stale replay arriving late).
        a1 = intent_mod.Actual(
            actual_id="act_seq_1", intent_id="int_seq_test", project_id="test-project",
            actual_calls=100, actual_cost_usd=5.0, sequence=0, client_seq=1,
            created_at=now_iso,
        )
        intent_mod.save_actual(a1)

        a2 = intent_mod.Actual(
            actual_id="act_seq_2", intent_id="int_seq_test", project_id="test-project",
            actual_calls=500, actual_cost_usd=25.0, sequence=1, client_seq=5,
            created_at=now_iso,
        )
        intent_mod.save_actual(a2)

        # Stale replay: high sequence (2) but low client_seq (2)
        a3 = intent_mod.Actual(
            actual_id="act_seq_3", intent_id="int_seq_test", project_id="test-project",
            actual_calls=50, actual_cost_usd=2.0, sequence=2, client_seq=2,
            created_at=now_iso,
        )
        intent_mod.save_actual(a3)

        summed = intent_mod.sum_actuals_for_intent("int_seq_test")
        # client_seq=5 (a2) should win, not the stale replay (a3)
        assert summed["actual_calls"] == 500
        assert summed["actual_cost_usd"] == 25.0
        assert summed["report_count"] == 3

    def test_client_seq_backward_compat(self, yaml_backend):
        """Older clients without client_seq should fall back to sequence."""
        import intent as intent_mod
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()

        i = intent_mod.Intent(
            intent_id="int_seq_compat",
            project_id="test-project",
            expected_calls=1000,
            expected_cost_usd=50,
            created_at=now_iso,
            updated_at=now_iso,
        )
        intent_mod.save_intent(i)

        # No client_seq (defaults to 0) — should fall back to sequence
        a1 = intent_mod.Actual(
            actual_id="act_compat_1", intent_id="int_seq_compat", project_id="test-project",
            actual_calls=100, actual_cost_usd=5.0, sequence=0, client_seq=0,
            created_at=now_iso,
        )
        intent_mod.save_actual(a1)
        a2 = intent_mod.Actual(
            actual_id="act_compat_2", intent_id="int_seq_compat", project_id="test-project",
            actual_calls=200, actual_cost_usd=10.0, sequence=1, client_seq=0,
            created_at=now_iso,
        )
        intent_mod.save_actual(a2)

        summed = intent_mod.sum_actuals_for_intent("int_seq_compat")
        # sequence=1 (a2) should win since client_seq is tied at 0
        assert summed["actual_calls"] == 200
        assert summed["actual_cost_usd"] == 10.0


class TestIssue5Security:
    """Regression tests for the 2026-10-12 adversarial review (issue #5)."""

    def _declare_with_kill(self, app):
        resp = app.post("/api/v1/intent",
            json={
                "project_id": "test-project",
                "job_id": "job-sec",
                "expected_calls": 10,
                "kill": {
                    "type": "http_callback",
                    "url": "https://example.com/kill",
                    "headers": {"Authorization": "Bearer callback-secret"},
                },
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        return resp.get_json()["intent_id"]

    def test_report_actual_rejects_cross_tenant_intent(self, app):
        """A token valid for project A must not report (or kill) on B's intent."""
        intent_id = self._declare_with_kill(app)
        resp = app.post("/api/v1/actual",
            json={
                "project_id": "other-project",   # different project, valid global token
                "intent_id": intent_id,          # ...but the intent belongs to test-project
                "actual_calls": 1,
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 404

    def test_intent_get_redacts_kill_credentials(self, app):
        intent_id = self._declare_with_kill(app)
        resp = app.get(f"/api/v1/intent/{intent_id}")
        assert resp.status_code == 200
        kill = resp.get_json()["kill"]
        assert kill == {"type": "http_callback"}

    def test_intents_list_redacts_kill_credentials(self, app):
        self._declare_with_kill(app)
        resp = app.get("/api/v1/intents")
        assert resp.status_code == 200
        for item in resp.get_json()["intents"]:
            assert "headers" not in (item.get("kill") or {})
            assert "url" not in (item.get("kill") or {})

    def test_bad_numeric_fields_return_400_not_500(self, app):
        resp = app.post("/api/v1/intent",
            json={"project_id": "test-project", "expected_calls": "abc"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 400
        intent_id = self._declare_with_kill(app)
        resp = app.post("/api/v1/actual",
            json={
                "project_id": "test-project", "intent_id": intent_id,
                "actual_cost_usd": "not-a-number",
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 400

    def test_failed_kill_marks_kill_failed_not_killed(self, app, monkeypatch):
        import intent as intent_mod
        import intent_kill
        from providers.base import KillResult
        from providers import registry as provider_registry

        monkeypatch.setattr(
            provider_registry, "kill_job",
            lambda desc, reason: KillResult(killed=False, job_id=desc.get("job_id", ""), error="nope"),
        )
        i = intent_mod.Intent(
            intent_id="int_killfail", project_id="test-project",
            job_id="job-x", status="running",
            kill={"type": "http_callback", "url": "https://example.com/kill"},
        )
        intent_mod.save_intent(i)
        result = intent_kill.kill_intent(i, reason="test", rule="test")
        assert result["killed"] is False
        assert intent_mod.get_intent("int_killfail").status == "kill_failed"

    def test_protected_project_kill_blocked(self, app, monkeypatch):
        import intent as intent_mod
        import intent_kill
        import main

        monkeypatch.setattr(main, "SELF_PROJECT_ID", "hub-project")
        i = intent_mod.Intent(
            intent_id="int_selfkill", project_id="hub-project",
            job_id="job-x", status="running",
            kill={"type": "http_callback", "url": "https://example.com/kill"},
        )
        result = intent_kill.kill_intent(i, reason="overrun", rule="test")
        assert result["killed"] is False
        assert "protected" in result["reason"]

    def test_project_month_spend_uses_latest_per_intent(self, app):
        """Cumulative incremental reports must not double-count."""
        from datetime import datetime, timezone
        import intent as intent_mod
        from intent_detection import project_month_spend

        now_iso = datetime.now(timezone.utc).isoformat()
        for intent_id, seqs in (
            ("int_a", [(1, 10.0), (2, 20.0)]),   # cumulative: latest is 20
            ("int_b", [(1, 5.0)]),
        ):
            intent_mod.save_intent(intent_mod.Intent(
                intent_id=intent_id, project_id="test-project",
                created_at=now_iso, updated_at=now_iso,
            ))
            for seq, cost in seqs:
                intent_mod.save_actual(intent_mod.Actual(
                    actual_id=f"act_{intent_id}_{seq}", intent_id=intent_id,
                    project_id="test-project", actual_cost_usd=cost,
                    sequence=seq - 1, client_seq=seq, created_at=now_iso,
                ))
        assert project_month_spend("test-project") == 25.0


class TestHttpCallbackSsrf:
    def test_metadata_ip_blocked(self):
        from providers.http_callback import _host_is_public
        ok, _ = _host_is_public("http://169.254.169.254/latest/meta-data")
        assert ok is False

    def test_loopback_blocked(self):
        from providers.http_callback import _host_is_public
        ok, _ = _host_is_public("http://127.0.0.1:8080/admin")
        assert ok is False

    def test_public_ip_allowed(self, monkeypatch):
        import socket as _socket
        import providers.http_callback as mod
        monkeypatch.setattr(
            _socket, "getaddrinfo",
            lambda host, port: [(2, 1, 6, "", ("93.184.216.34", 0))],
        )
        ok, _ = mod._host_is_public("https://example.com/kill")
        assert ok is True
