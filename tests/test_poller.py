"""
Tests for the real-time quota-spike poller.

Run locally:
  cd ~/projects/CloudManagement
  pytest tests/ -v

Cloud Monitoring calls are mocked — no live GCP calls.
"""

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import poller  # noqa: E402
import registry  # noqa: E402


def make_account(**kwargs):
    defaults = dict(
        project_id="proj-a",
        billing_account_id="01AB-23CD-EF45",
        owner_email="a@example.com",
        allowlist=False,
        budget_amount_usd=10,
        quota_rpm_cap=0,
    )
    defaults.update(kwargs)
    return registry.Account(**defaults)


class TestCheckAccount:
    @patch("poller._sum_metric")
    def test_quota_exceeded_trips_immediately(self, mock_sum):
        # First call = quota_exceeded metric -> nonzero
        mock_sum.side_effect = [3.0]
        acct = make_account()
        trip = poller.check_account(acct)
        assert trip is not None
        assert trip["rule"] == "quota_exceeded"

    @patch("poller._sum_metric")
    def test_baseline_ratio_trip(self, mock_sum):
        # quota_exceeded=0, recent usage=100 over 5 min, baseline=60 over 60 min (rate 1/min)
        # recent_rate = 100/5 = 20, baseline_rate = 60/60 = 1 -> ratio 20x > default 5x
        mock_sum.side_effect = [0.0, 100.0, 60.0]
        acct = make_account()
        trip = poller.check_account(acct)
        assert trip is not None
        assert trip["rule"] == "baseline_ratio"

    @patch("poller._sum_metric")
    def test_no_trip_when_within_baseline(self, mock_sum):
        # recent_rate = 10/5 = 2, baseline_rate = 300/60 = 5 -> below multiplier
        mock_sum.side_effect = [0.0, 10.0, 300.0]
        acct = make_account()
        trip = poller.check_account(acct)
        assert trip is None

    @patch("poller._sum_metric")
    def test_absolute_cap_trip_with_no_baseline(self, mock_sum):
        # No history: baseline_rate = 0 -> baseline rule skipped; absolute cap applies
        mock_sum.side_effect = [0.0, 500.0, 0.0]
        acct = make_account(quota_rpm_cap=50)
        trip = poller.check_account(acct)
        assert trip is not None
        assert trip["rule"] == "absolute_cap"

    @patch("poller._sum_metric")
    def test_no_trip_when_no_cap_and_no_baseline(self, mock_sum):
        mock_sum.side_effect = [0.0, 10.0, 0.0]
        acct = make_account(quota_rpm_cap=0)
        trip = poller.check_account(acct)
        assert trip is None


class TestPollAllAccounts:
    @patch("poller.check_account")
    @patch("registry.list_accounts")
    def test_allowlisted_accounts_skipped(self, mock_list, mock_check):
        mock_list.return_value = [make_account(project_id="proj-safe", allowlist=True)]
        calls = []
        poller.poll_all_accounts(lambda pid, reason: calls.append((pid, reason)) or [])
        mock_check.assert_not_called()
        assert calls == []

    @patch("registry.list_accounts")
    def test_trip_calls_execute_killswitch(self, mock_list):
        mock_list.return_value = [make_account(project_id="proj-risky")]
        calls = []

        def fake_execute(project_id, reason):
            calls.append((project_id, reason))
            return [{"action": "scale_to_zero", "target": "svc1"}]

        with patch("poller.check_account", return_value={"rule": "quota_exceeded", "project": "proj-risky", "value": 3.0}):
            trips = poller.poll_all_accounts(fake_execute)

        assert calls == [("proj-risky", "quota_spike")]
        assert len(trips) == 1
        assert trips[0]["actions_taken"] == 1

    @patch("registry.list_accounts")
    def test_no_trip_no_action(self, mock_list):
        mock_list.return_value = [make_account()]
        with patch("poller.check_account", return_value=None):
            trips = poller.poll_all_accounts(lambda pid, reason: [])
        assert trips == []
