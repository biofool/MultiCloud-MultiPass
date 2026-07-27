"""
Tests for the dashboard blueprint.

Run locally:
  cd ~/projects/CloudManagement
  pytest tests/ -v
"""

import json
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

# Mock GCP client libraries before importing
for _mod in ("google.cloud.run_v2", "google.cloud.scheduler_v1",
             "google.cloud.compute_v1", "google.cloud.billing_v1",
             "google.cloud.bigquery"):
    sys.modules[_mod] = MagicMock()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import main


@pytest.fixture
def client():
    main._processed_messages.clear()
    main.DRY_RUN = True
    main.ALLOWLIST = set()
    main.app.config["TESTING"] = True
    with main.app.test_client() as client:
        yield client


# ---------------------------------------------------------------------------
# Tests: dashboard page
# ---------------------------------------------------------------------------

class TestDashboardPage:
    def test_dashboard_renders(self, client):
        resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert b"GCP Cost Dashboard" in resp.data
        assert b"Kill Switch" in resp.data

    def test_dashboard_shows_budget(self, client):
        resp = client.get("/dashboard")
        assert b"$5" in resp.data  # default budget

    def test_dashboard_shows_warning_when_no_bq(self, client):
        resp = client.get("/dashboard")
        assert b"BQ_BILLING_TABLE" in resp.data or b"budget" in resp.data


# ---------------------------------------------------------------------------
# Tests: API endpoints (with mocked BigQuery)
# ---------------------------------------------------------------------------

class TestDashboardAPI:
    def test_summary_no_bq_table(self, client):
        """Summary should return zeros when BQ_BILLING_TABLE is not set."""
        with patch("dashboard.BQ_BILLING_TABLE", ""):
            resp = client.get("/api/summary")
            assert resp.status_code == 200
            data = json.loads(resp.data)
            assert data["mtd_cost"] == 0
            assert data["budget_configured"] is False

    @patch("dashboard._query_bq")
    def test_summary_with_data(self, mock_bq, client):
        mock_bq.return_value = [{"mtd_cost": 3.50, "currency": "USD", "project_count": 2}]
        with patch("dashboard.BQ_BILLING_TABLE", "project.dataset.table"):
            # Clear cache
            import dashboard
            dashboard._cache.clear()
            resp = client.get("/api/summary")
            assert resp.status_code == 200
            data = json.loads(resp.data)
            assert data["mtd_cost"] == 3.5
            assert data["pct_of_budget"] == 70.0
            assert data["remaining"] == 1.5
            assert data["project_count"] == 2
            assert data["budget_configured"] is True

    @patch("dashboard._query_bq")
    def test_daily_endpoint(self, mock_bq, client):
        from datetime import date
        mock_bq.return_value = [
            {"usage_date": date(2025, 1, 1), "daily_cost": 0.50},
            {"usage_date": date(2025, 1, 2), "daily_cost": 1.20},
        ]
        import dashboard
        dashboard._cache.clear()
        resp = client.get("/api/daily")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert len(data["labels"]) == 2
        assert data["values"] == [0.5, 1.2]

    @patch("dashboard._query_bq")
    def test_services_endpoint(self, mock_bq, client):
        mock_bq.return_value = [
            {"service_name": "Cloud Run", "total_cost": 2.00},
            {"service_name": "BigQuery", "total_cost": 1.50},
        ]
        import dashboard
        dashboard._cache.clear()
        resp = client.get("/api/services")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["labels"] == ["Cloud Run", "BigQuery"]
        assert data["values"] == [2.0, 1.5]

    @patch("dashboard._query_bq")
    def test_projects_endpoint(self, mock_bq, client):
        mock_bq.return_value = [
            {"project_id": "proj-a", "total_cost": 3.00},
            {"project_id": "proj-b", "total_cost": 1.00},
        ]
        import dashboard
        dashboard._cache.clear()
        resp = client.get("/api/projects")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert len(data["projects"]) == 2
        assert data["projects"][0]["project_id"] == "proj-a"

    @patch("dashboard._query_bq")
    def test_spike_endpoint(self, mock_bq, client):
        mock_bq.return_value = [{"yesterday_cost": 2.0, "daily_avg_7d": 1.0, "pct_of_baseline": 200.0}]
        import dashboard
        dashboard._cache.clear()
        resp = client.get("/api/spike")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["pct_of_baseline"] == 200.0

    @patch("dashboard._query_bq")
    def test_bq_error_returns_empty(self, mock_bq, client):
        mock_bq.return_value = []
        import dashboard
        dashboard._cache.clear()
        resp = client.get("/api/services")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["labels"] == []

    def test_accounts_endpoint_empty_registry(self, client):
        import dashboard
        dashboard._cache.clear()
        with patch("registry.list_accounts", return_value=[]):
            resp = client.get("/api/accounts")
            assert resp.status_code == 200
            data = json.loads(resp.data)
            assert data["accounts"] == []

    def test_accounts_endpoint_with_spend(self, client):
        import dashboard
        import registry as registry_mod
        dashboard._cache.clear()
        accounts = [
            registry_mod.Account(project_id="proj-a", billing_account_id="01AB-23CD-EF45", owner_email="a@example.com", budget_amount_usd=10),
        ]
        with patch("registry.list_accounts", return_value=accounts), \
             patch("dashboard.HUB_PROJECT_ID", "hub-project"), \
             patch("dashboard._query_bq", return_value=[{"project_id": "proj-a", "mtd_cost": 5.0, "currency": "USD"}]):
            resp = client.get("/api/accounts")
            assert resp.status_code == 200
            data = json.loads(resp.data)
            assert len(data["accounts"]) == 1
            assert data["accounts"][0]["mtd_cost"] == 5.0
            assert data["accounts"][0]["pct_of_budget"] == 50.0

    def test_api_caching(self, client):
        """Second call within TTL should return cached result without re-querying."""
        import dashboard
        dashboard._cache.clear()

        call_count = 0
        def counting_query(sql):
            nonlocal call_count
            call_count += 1
            return [{"service_name": "Test", "total_cost": 1.0}]

        with patch("dashboard._query_bq", side_effect=counting_query):
            with patch("dashboard.BQ_BILLING_TABLE", "project.dataset.table"):
                client.get("/api/services")
                client.get("/api/services")
                assert call_count == 1  # only queried once
