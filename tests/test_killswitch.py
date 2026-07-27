"""
Test suite for the GCP Cost Kill Switch service.

Run locally:
  cd ~/projects/CloudManagement
  pip install pytest flask
  pytest tests/ -v

These tests do NOT call any GCP APIs — they test parsing, threshold logic,
dedup, and config validation. GCP API calls are mocked.
"""

import json
import base64
import os
import sys
import time
from unittest.mock import patch, MagicMock

import pytest

# Mock GCP client libraries before importing main — tests don't call real APIs
for _mod in ("google.cloud.run_v2", "google.cloud.scheduler_v1",
             "google.cloud.compute_v1", "google.cloud.billing_v1",
             "google.cloud.api_keys_v2", "google.cloud.container_v1"):
    sys.modules[_mod] = MagicMock()

# Ensure main.py is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_envelope(data: dict, msg_id: str = "test-123") -> dict:
    """Build a Pub/Sub push envelope."""
    encoded = base64.b64encode(json.dumps(data).encode()).decode()
    return {
        "message": {
            "data": encoded,
            "messageId": msg_id,
            "publishTime": "2025-01-01T00:00:00Z",
        },
        "subscription": "projects/test/subscriptions/killswitch-push",
    }


def make_alert(
    threshold: float = 100,
    actual: float = 5.0,
    forecast: float = 5.0,
    budget: float = 5.0,
    alert_type: str = "budget",
    projects: list[str] | None = None,
) -> dict:
    return {
        "alertType": alert_type,
        "thresholdPercent": threshold,
        "actualCost": actual,
        "forecastCost": forecast,
        "budgetAmount": budget,
        "currency": "USD",
        "budgetName": "test-budget",
        "projectIds": projects or ["dev-project-1"],
    }


# ---------------------------------------------------------------------------
# Tests: alert parsing
# ---------------------------------------------------------------------------

class TestParsePubsubMessage:
    def test_valid_budget_alert(self):
        envelope = make_envelope(make_alert(threshold=100, actual=5.0))
        alert = main.parse_pubsub_message(envelope)
        assert alert is not None
        assert alert.threshold_percent == 100
        assert alert.actual_spend == 5.0
        assert alert.budget_name == "test-budget"
        assert alert.project_ids == ["dev-project-1"]

    def test_forecast_alert(self):
        data = make_alert(alert_type="forecast", threshold=90, actual=4.0, forecast=4.5)
        envelope = make_envelope(data)
        alert = main.parse_pubsub_message(envelope)
        assert alert is not None
        assert alert.is_forecast
        assert not alert.is_actual

    def test_malformed_json(self):
        envelope = {
            "message": {
                "data": base64.b64encode(b"not json").decode(),
                "messageId": "bad-1",
            }
        }
        alert = main.parse_pubsub_message(envelope)
        assert alert is None

    def test_missing_data_field(self):
        envelope = {"message": {}}
        alert = main.parse_pubsub_message(envelope)
        assert alert is None

    def test_missing_message_key(self):
        alert = main.parse_pubsub_message({})
        assert alert is None

    def test_single_project_string(self):
        data = make_alert()
        data["projectIds"] = "single-project"
        envelope = make_envelope(data)
        alert = main.parse_pubsub_message(envelope)
        assert alert is not None
        assert alert.project_ids == ["single-project"]

    def test_no_projects(self):
        data = make_alert()
        data["projectIds"] = []
        envelope = make_envelope(data)
        alert = main.parse_pubsub_message(envelope)
        assert alert is not None
        assert alert.project_ids == []


# ---------------------------------------------------------------------------
# Tests: threshold evaluation
# ---------------------------------------------------------------------------

class TestShouldTakeAction:
    def test_actual_100_percent(self):
        alert = main.BudgetAlert(
            alert_type="budget", budget_name="t", threshold_percent=100,
            actual_spend=5, forecasted_spend=5, budget_amount=5,
            currency="USD", project_ids=["p1"],
        )
        assert main.should_take_action(alert)

    def test_actual_below_100(self):
        alert = main.BudgetAlert(
            alert_type="budget", budget_name="t", threshold_percent=50,
            actual_spend=2, forecasted_spend=3, budget_amount=5,
            currency="USD", project_ids=["p1"],
        )
        assert not main.should_take_action(alert)

    def test_forecast_90_percent(self):
        alert = main.BudgetAlert(
            alert_type="forecast", budget_name="t", threshold_percent=90,
            actual_spend=4, forecasted_spend=4.5, budget_amount=5,
            currency="USD", project_ids=["p1"],
        )
        assert main.should_take_action(alert)

    def test_forecast_below_90(self):
        alert = main.BudgetAlert(
            alert_type="forecast", budget_name="t", threshold_percent=80,
            actual_spend=3, forecasted_spend=4, budget_amount=5,
            currency="USD", project_ids=["p1"],
        )
        assert not main.should_take_action(alert)

    def test_actual_50_with_forecast_over_budget(self):
        alert = main.BudgetAlert(
            alert_type="budget", budget_name="t", threshold_percent=50,
            actual_spend=2.5, forecasted_spend=6, budget_amount=5,
            currency="USD", project_ids=["p1"],
        )
        assert main.should_take_action(alert)

    def test_emergency_150_percent(self):
        alert = main.BudgetAlert(
            alert_type="budget", budget_name="t", threshold_percent=150,
            actual_spend=7.5, forecasted_spend=8, budget_amount=5,
            currency="USD", project_ids=["p1"],
        )
        assert main.should_take_action(alert)


# ---------------------------------------------------------------------------
# Tests: dedup
# ---------------------------------------------------------------------------

class TestDedup:
    def test_first_message_not_duplicate(self):
        main._processed_messages.clear()
        assert not main._is_duplicate("msg-1")

    def test_second_same_message_is_duplicate(self):
        main._processed_messages.clear()
        main._is_duplicate("msg-2")
        assert main._is_duplicate("msg-2")

    def test_different_messages_not_duplicate(self):
        main._processed_messages.clear()
        main._is_duplicate("msg-a")
        assert not main._is_duplicate("msg-b")

    def test_expired_messages_cleaned(self):
        main._processed_messages.clear()
        main._processed_messages["old"] = time.time() - 999
        assert not main._is_duplicate("new")
        assert "old" not in main._processed_messages


# ---------------------------------------------------------------------------
# Tests: process_alert integration
# ---------------------------------------------------------------------------

class TestProcessAlert:
    def setup_method(self):
        main._processed_messages.clear()
        main.DRY_RUN = True
        main.ALLOWLIST = set()

    def test_malformed_envelope_returns_400(self):
        status, msg = main.process_alert({})
        assert status == 400

    def test_below_threshold_returns_200(self):
        envelope = make_envelope(make_alert(threshold=30, actual=1.0, budget=5.0))
        status, msg = main.process_alert(envelope)
        assert status == 200
        assert "Below" in msg

    def test_allowlisted_project_skipped(self):
        main.ALLOWLIST = {"dev-project-1"}
        envelope = make_envelope(make_alert(threshold=100, actual=5.0, projects=["dev-project-1"]))
        status, msg = main.process_alert(envelope)
        assert status == 200
        assert "allowlist" in msg.lower()

    def test_duplicate_returns_200(self):
        envelope = make_envelope(make_alert(threshold=100, actual=5.0), msg_id="dup-1")
        main.process_alert(envelope)
        status, msg = main.process_alert(envelope)
        assert status == 200
        assert "Duplicate" in msg

    @patch("main.disable_cloud_run_services")
    def test_action_taken_on_100_percent(self, mock_disable):
        mock_disable.return_value = ["projects/dev/locations/us-central1/services/svc1"]
        envelope = make_envelope(make_alert(threshold=100, actual=5.0, projects=["dev-project-1"]))
        status, msg = main.process_alert(envelope)
        assert status == 200
        assert "1 actions" in msg
        mock_disable.assert_called_once_with("dev-project-1")

    @patch("main.disable_cloud_run_services")
    def test_dry_run_does_not_call_api(self, mock_disable):
        mock_disable.return_value = []
        main.DRY_RUN = True
        envelope = make_envelope(make_alert(threshold=100, actual=5.0))
        status, msg = main.process_alert(envelope)
        assert "dry-run" in msg


# ---------------------------------------------------------------------------
# Tests: Flask endpoints
# ---------------------------------------------------------------------------

class TestFlaskEndpoints:
    def setup_method(self):
        main._processed_messages.clear()
        main.DRY_RUN = True
        main.ALLOWLIST = set()
        self.app = main.app.test_client()

    def test_health(self):
        resp = self.app.get("/health")
        assert resp.status_code == 200

    def test_info(self):
        resp = self.app.get("/")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["service"] == "cloudmanagement"

    def test_post_malformed(self):
        resp = self.app.post("/", data="not json", content_type="application/json")
        assert resp.status_code == 400

    def test_post_valid_alert(self):
        envelope = make_envelope(make_alert(threshold=30, actual=1.0, budget=5.0))
        resp = self.app.post("/", json=envelope)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Tests: billing shutoff guard
# ---------------------------------------------------------------------------

class TestBillingShutoffGuard:
    def test_disabled_by_default(self):
        main.ENABLE_BILLING_SHUTOFF = False
        result = main.disable_billing("test-project")
        assert result is False

    @patch("main.billing_v1.CloudBillingClient")
    def test_dry_run_logs_only(self, mock_client_cls):
        main.ENABLE_BILLING_SHUTOFF = True
        main.DRY_RUN = True
        result = main.disable_billing("test-project")
        assert result is True
        mock_client_cls.return_value.update_project_billing_info.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: new destructive actions (disabled-by-default guards)
# ---------------------------------------------------------------------------

class TestApiKeyRevokeGuard:
    def test_disabled_by_default(self):
        main.ENABLE_API_KEY_REVOKE = False
        assert main.revoke_api_keys("test-project") == []


class TestGkeScaleDownGuard:
    def test_disabled_by_default(self):
        main.ENABLE_GKE_SCALE_DOWN = False
        assert main.scale_down_gke_clusters("test-project") == []


# ---------------------------------------------------------------------------
# Tests: execute_killswitch orchestrator
# ---------------------------------------------------------------------------

class TestExecuteKillswitch:
    def setup_method(self):
        main.DRY_RUN = True
        main.ENABLE_API_KEY_REVOKE = False
        main.ENABLE_GKE_SCALE_DOWN = False
        main.ENABLE_BILLING_SHUTOFF = False

    @patch("main.disable_cloud_run_services")
    def test_aggregates_actions_from_all_functions(self, mock_disable):
        mock_disable.return_value = ["projects/dev/locations/us-central1/services/svc1"]
        actions = main.execute_killswitch("dev-project-1", reason="quota_spike")
        assert len(actions) == 1
        assert actions[0]["action"] == "scale_to_zero"
        mock_disable.assert_called_once_with("dev-project-1")

    @patch("main.disable_cloud_run_services")
    def test_no_actions_when_nothing_to_do(self, mock_disable):
        mock_disable.return_value = []
        actions = main.execute_killswitch("dev-project-1", reason="budget_alert")
        assert actions == []


# ---------------------------------------------------------------------------
# Tests: is_project_protected (env allowlist + registry allowlist)
# ---------------------------------------------------------------------------

class TestIsProjectProtected:
    def test_env_allowlist_protects(self):
        main.ALLOWLIST = {"prod-project"}
        with patch("registry.is_allowlisted", return_value=False):
            assert main.is_project_protected("prod-project") is True

    def test_registry_allowlist_protects(self):
        main.ALLOWLIST = set()
        with patch("registry.is_allowlisted", return_value=True):
            assert main.is_project_protected("dev-project") is True

    def test_unprotected_when_neither(self):
        main.ALLOWLIST = set()
        with patch("registry.is_allowlisted", return_value=False):
            assert main.is_project_protected("dev-project") is False
