"""Tests for the intent/actual API usage reporting protocol (intent.py).

Split during the ≤250-line refactor: the ``yaml_backend``/``app`` fixtures
moved to ``intent_fixtures.py`` (re-exported here so the long-standing
``from test_intent import yaml_backend, app`` in test_ciso_findings.py and
test_intent_sev_fixes.py keeps working), and the query/budget/security
test classes moved to ``test_intent_status.py``,
``test_intent_client_api.py``, and ``test_intent_security.py``.
"""

import os
import sys

# Ensure the project root and the tests dir (for intent_fixtures) are on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from intent_fixtures import yaml_backend, app  # noqa: E402,F401  (fixtures, re-exported)


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
