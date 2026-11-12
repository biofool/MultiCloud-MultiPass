"""Status/lifecycle tests for the intent/actual protocol (intent.py).

Split out of ``test_intent.py`` during the ≤250-line refactor — pure
structural move, no behavior change. Covers expected costs, intent
listing, manual kill, and client_seq enforcement.
"""

import os
import sys

# Ensure the project root and the tests dir (for intent_fixtures) are on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from intent_fixtures import yaml_backend, app  # noqa: E402,F401  (fixtures)


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
