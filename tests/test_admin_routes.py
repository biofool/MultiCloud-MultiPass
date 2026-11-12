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

Split during the ≤250-line refactor: shared fixtures/factories moved to
``admin_helpers.py`` and the /reconcile tests moved to
``test_admin_routes_reconcile.py``.
"""

import json
import os
import sys
from unittest.mock import patch

import pytest

# Ensure the tests dir is importable for admin_helpers
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from admin_helpers import (  # noqa: E402
    main,
    app,  # noqa: F401  (fixture)
    make_intent,
)


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
