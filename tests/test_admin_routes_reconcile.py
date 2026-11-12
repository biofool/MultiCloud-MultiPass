"""Tests for the POST /reconcile endpoint in admin_routes.py
(billed-vs-reported reconciliation).

Split out of ``test_admin_routes.py`` during the ≤250-line refactor —
pure structural move, no behavior change. Shared fixtures/factories live
in ``admin_helpers.py``. Every collaborator is mocked — no live
GCP/network calls.
"""

import json
import os
import sys
from unittest.mock import patch

import pytest

# Ensure the tests dir is importable for admin_helpers
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from admin_helpers import (  # noqa: E402
    intent,
    app,  # noqa: F401  (fixture)
    make_account,
    _IN_RECONCILE_WINDOW,
)


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
                          provider="gemini", actual_cost_usd=9.6, actual_calls=100,
                          created_at=_IN_RECONCILE_WINDOW),
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
                          provider="gemini", actual_cost_usd=5.0, actual_calls=100,
                          created_at=_IN_RECONCILE_WINDOW),
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
