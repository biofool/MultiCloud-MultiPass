"""
Test suite for the GCP Cost Kill Switch service — Flask endpoints,
action guards, execute_killswitch orchestration, and allowlist checks.

Split out of ``test_killswitch.py`` during the ≤250-line refactor — pure
structural move, no behavior change. Shared mocks/payload builders live
in ``killswitch_helpers.py``.
"""

import json
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

# Ensure the tests dir is importable for killswitch_helpers
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from killswitch_helpers import main, make_envelope, make_alert  # noqa: E402


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


# ---------------------------------------------------------------------------
# Tests: a failed action stays out of the returned list (issue #18)
# ---------------------------------------------------------------------------

class TestFailedActionNotReported:
    def test_failed_stop_instance_not_in_actions(self):
        # killswitch_actions.compute_v1 is whichever mock sys.modules held
        # when main was first imported — patch that binding, not a re-import.
        import killswitch_actions
        compute_v1 = killswitch_actions.compute_v1
        client = MagicMock()
        inst = MagicMock(status=compute_v1.Instance.Status.RUNNING)
        inst.name = "vm-1"  # name= is a reserved Mock kwarg, not an attribute
        client.list.return_value = [inst]
        client.stop.side_effect = Exception("api down")
        with patch.object(main, "STOP_COMPUTE_INSTANCES", True), \
             patch.object(main, "DRY_RUN", False), \
             patch.object(compute_v1, "InstancesClient", return_value=client):
            assert main.stop_compute_instances("p1", zones=["z1"]) == []
            client.stop.side_effect = None
            assert main.stop_compute_instances("p1", zones=["z1"]) == ["z1/vm-1"]
