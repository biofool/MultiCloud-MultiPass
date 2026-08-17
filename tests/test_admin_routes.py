"""Tests for admin_routes.py (the /poll, /poll-intents, /reconcile, and
service-info "/" GET endpoints).

admin_routes.py was split out of the original monolithic main.py during
the file-layout refactor (see TEST_PLAN.md) and registered as a Flask
blueprint the same way as dashboard/intent/inventory. No existing test
file exercised these endpoints before this file was added — main.py's
own tests (test_killswitch.py) only cover the POST "/" budget-alert path
and "/health".

These tests mock every collaborator (poller, intent, registry, and the
providers.registry reconciliation layer) — no live GCP/network calls.
"""

import json
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

# Mock GCP client libraries before importing main — same as test_killswitch.py.
for _mod in ("google.cloud.run_v2", "google.cloud.scheduler_v1",
             "google.cloud.compute_v1", "google.cloud.billing_v1",
             "google.cloud.api_keys_v2", "google.cloud.container_v1"):
    sys.modules[_mod] = MagicMock()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import main  # noqa: E402
import registry  # noqa: E402
import intent  # noqa: E402


@pytest.fixture
def app():
    main.app.config["TESTING"] = True
    return main.app.test_client()


def make_account(**kwargs):
    defaults = dict(
        project_id="proj-a",
        billing_account_id="01AB-23CD-EF45",
        owner_email="a@example.com",
        allowlist=False,
        budget_amount_usd=10,
    )
    defaults.update(kwargs)
    return registry.Account(**defaults)


def make_intent(**kwargs):
    defaults = dict(
        intent_id="intent-1",
        project_id="proj-a",
        status="running",
    )
    defaults.update(kwargs)
    return intent.Intent(**defaults)


# ---------------------------------------------------------------------------
# GET / — service info
# ---------------------------------------------------------------------------

class TestInfoEndpoint:
    def test_info_returns_service_metadata(self, app):
        resp = app.get("/")
        assert resp.status_code == 200
        body = json.loads(resp.data)
        assert body["service"] == "cloudmanagement"
        assert "dry_run" in body
        assert "allowlist" in body

    def test_info_lists_expected_endpoints(self, app):
        resp = app.get("/")
        body = json.loads(resp.data)
        endpoints = body["endpoints"]
        for path_desc in (
            "budget_alert", "health", "poll_quota", "poll_intents",
            "reconcile", "declare_intent", "report_actual",
            "expected_costs", "list_intents", "dashboard", "inventory",
        ):
            assert path_desc in endpoints


# ---------------------------------------------------------------------------
# POST /poll — quota-spike poller
# ---------------------------------------------------------------------------

class TestPollEndpoint:
    @patch("poller.poll_all_accounts")
    def test_poll_reports_trips(self, mock_poll, app):
        mock_poll.return_value = [{"project": "proj-a", "rule": "quota_exceeded", "actions_taken": 1}]
        resp = app.post("/poll")
        assert resp.status_code == 200
        body = json.loads(resp.data)
        assert body["checked"] is True
        assert body["trips"] == [{"project": "proj-a", "rule": "quota_exceeded", "actions_taken": 1}]
        assert "dry_run" in body
        # execute_killswitch (main's) is passed through as the callback
        assert mock_poll.call_args.args[0] is main.execute_killswitch

    @patch("poller.poll_all_accounts")
    def test_poll_no_trips(self, mock_poll, app):
        mock_poll.return_value = []
        resp = app.post("/poll")
        assert resp.status_code == 200
        body = json.loads(resp.data)
        assert body["trips"] == []


# ---------------------------------------------------------------------------
# POST /poll-intents — intent/actual overrun + budget poller
# ---------------------------------------------------------------------------

class TestPollIntentsEndpoint:
    @patch("intent.list_intents")
    def test_no_running_intents(self, mock_list, app):
        mock_list.return_value = []
        resp = app.post("/poll-intents")
        assert resp.status_code == 200
        body = json.loads(resp.data)
        assert body == {"checked": True, "overruns": [], "dry_run": main.DRY_RUN}

    @patch("intent.kill_intent")
    @patch("intent.check_project_budget")
    @patch("intent.check_intent_overrun")
    @patch("intent.list_intents")
    def test_overrun_triggers_kill(self, mock_list, mock_overrun, mock_budget, mock_kill, app):
        it = make_intent(project_id="proj-a")
        mock_list.return_value = [it]
        mock_overrun.return_value = {"rule": "variance_exceeded", "intent_id": it.intent_id}
        mock_kill.return_value = {"killed": True}

        resp = app.post("/poll-intents")

        assert resp.status_code == 200
        body = json.loads(resp.data)
        assert len(body["overruns"]) == 1
        assert body["overruns"][0]["kill_result"] == {"killed": True}
        mock_kill.assert_called_once_with(it, reason="variance_exceeded", rule="variance_exceeded")
        mock_budget.assert_not_called()

    @patch("intent.kill_intent")
    @patch("intent.check_project_budget")
    @patch("intent.check_intent_overrun")
    @patch("intent.list_intents")
    def test_self_project_overrun_is_blocked(self, mock_list, mock_overrun, mock_budget, mock_kill, app, monkeypatch):
        monkeypatch.setattr(main, "SELF_PROJECT_ID", "hub-project")
        it = make_intent(project_id="hub-project")
        mock_list.return_value = [it]
        mock_overrun.return_value = {"rule": "variance_exceeded", "intent_id": it.intent_id}

        resp = app.post("/poll-intents")

        body = json.loads(resp.data)
        assert body["overruns"][0]["kill_result"] == {"killed": False, "reason": "self_project_blocked"}
        mock_kill.assert_not_called()

    @patch("intent.kill_intent")
    @patch("intent.check_project_budget")
    @patch("intent.check_intent_overrun")
    @patch("intent.list_intents")
    def test_no_overrun_but_budget_exceeded_triggers_kill(self, mock_list, mock_overrun, mock_budget, mock_kill, app):
        it = make_intent(project_id="proj-a")
        mock_list.return_value = [it]
        mock_overrun.return_value = None
        mock_budget.return_value = {"rule": "budget_exceeded", "project_id": "proj-a"}
        mock_kill.return_value = {"killed": True}

        resp = app.post("/poll-intents")

        body = json.loads(resp.data)
        assert len(body["overruns"]) == 1
        assert body["overruns"][0]["kill_result"] == {"killed": True}
        mock_kill.assert_called_once_with(it, reason="budget_exceeded", rule="budget_exceeded")

    @patch("intent.kill_intent")
    @patch("intent.check_project_budget")
    @patch("intent.check_intent_overrun")
    @patch("intent.list_intents")
    def test_no_overrun_and_no_budget_issue_is_silent(self, mock_list, mock_overrun, mock_budget, mock_kill, app):
        mock_list.return_value = [make_intent()]
        mock_overrun.return_value = None
        mock_budget.return_value = None

        resp = app.post("/poll-intents")

        body = json.loads(resp.data)
        assert body["overruns"] == []
        mock_kill.assert_not_called()


# ---------------------------------------------------------------------------
# POST /reconcile — billed-vs-reported reconciliation
# ---------------------------------------------------------------------------

class TestReconcileEndpoint:
    @patch("registry.list_accounts")
    def test_allowlisted_account_is_skipped(self, mock_accounts, app):
        mock_accounts.return_value = [make_account(project_id="safe", allowlist=True)]

        resp = app.post("/reconcile")

        assert resp.status_code == 200
        body = json.loads(resp.data)
        assert body["reconciled"] is True
        assert body["results"] == []

    @patch("providers.registry.fetch_billed_costs")
    @patch("registry.list_accounts")
    def test_no_billing_data_reported(self, mock_accounts, mock_billed, app):
        mock_accounts.return_value = [make_account(project_id="proj-a")]
        mock_billed.return_value = []

        resp = app.post("/reconcile")

        body = json.loads(resp.data)
        assert body["results"] == [{"project_id": "proj-a", "billed_count": 0, "note": "no billing data"}]

    @patch("intent.save_expected_cost")
    @patch("intent.list_actuals")
    @patch("providers.registry.fetch_billed_costs")
    @patch("registry.list_accounts")
    def test_small_variance_does_not_recalibrate(self, mock_accounts, mock_billed, mock_actuals, mock_save, app):
        from providers.base import BilledCost

        mock_accounts.return_value = [make_account(project_id="proj-a")]
        mock_billed.return_value = [BilledCost(project_id="proj-a", provider="gemini", cost_usd=10.0)]
        mock_actuals.return_value = [
            intent.Actual(actual_id="a1", intent_id="i1", project_id="proj-a",
                          provider="gemini", actual_cost_usd=9.6, actual_calls=100),
        ]

        resp = app.post("/reconcile")

        body = json.loads(resp.data)
        variances = body["results"][0]["variances"]
        assert variances[0]["variance"] < 0.15
        mock_save.assert_not_called()

    @patch("intent.save_expected_cost")
    @patch("intent.list_actuals")
    @patch("providers.registry.fetch_billed_costs")
    @patch("registry.list_accounts")
    def test_large_variance_recalibrates_expected_cost(self, mock_accounts, mock_billed, mock_actuals, mock_save, app):
        from providers.base import BilledCost

        mock_accounts.return_value = [make_account(project_id="proj-a")]
        mock_billed.return_value = [BilledCost(project_id="proj-a", provider="gemini", cost_usd=10.0)]
        mock_actuals.return_value = [
            intent.Actual(actual_id="a1", intent_id="i1", project_id="proj-a",
                          provider="gemini", actual_cost_usd=5.0, actual_calls=100),
        ]

        resp = app.post("/reconcile")

        body = json.loads(resp.data)
        variances = body["results"][0]["variances"]
        assert variances[0]["variance"] > 0.15
        mock_save.assert_called_once()
        saved_ec = mock_save.call_args.args[0]
        assert saved_ec.project_id == "proj-a"
        assert saved_ec.provider == "gemini"
        assert saved_ec.calibration_delta == round((10.0 - 5.0) / 100, 6)
