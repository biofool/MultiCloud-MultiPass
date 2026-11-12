"""Tests for the client-facing intent endpoints required by
cloud_management_client v0.12.0 (issue #1): budget status/admission,
single-intent fetch, kill orders, and exposure reporting.

Split out of ``test_intent.py`` during the ≤250-line refactor — pure
structural move, no behavior change.
"""

import os
import sys

# Ensure the project root and the tests dir (for intent_fixtures) are on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from intent_fixtures import yaml_backend, app  # noqa: E402,F401  (fixtures)


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
