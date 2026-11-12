"""Tests for the AWS provider (providers/aws.py).

Split out of ``test_providers.py`` during the ≤250-line refactor — pure
structural move, no behavior change.
"""

import os
import sys
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestAwsProvider:
    def test_dry_run_stop(self, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "true")
        from providers.aws import AwsProvider

        provider = AwsProvider()
        result = provider.kill_job(
            {"type": "ec2", "instance_id": "i-0123456789abcdef0", "region": "us-east-1"},
            reason="test",
        )
        assert result.killed is True
        assert result.action == "ec2"
        assert result.detail == "dry_run"

    def test_missing_instance_id(self, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "true")
        from providers.aws import AwsProvider

        provider = AwsProvider()
        result = provider.kill_job({"type": "ec2"}, reason="test")
        assert result.killed is False
        assert "missing instance_id" in result.error

    def test_invalid_action(self, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "true")
        from providers.aws import AwsProvider

        provider = AwsProvider()
        result = provider.kill_job(
            {"type": "ec2", "instance_id": "i-0123456789abcdef0", "action": "reboot"},
            reason="test",
        )
        assert result.killed is False
        assert "invalid action" in result.error

    def test_unknown_kill_type(self, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "true")
        from providers.aws import AwsProvider

        provider = AwsProvider()
        result = provider.kill_job({"type": "unknown"}, reason="test")
        assert result.killed is False
        assert "unknown AWS kill type" in result.error

    def test_live_terminate_calls_boto3(self, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "false")
        from providers.aws import AwsProvider

        mock_client = MagicMock()
        mock_session = MagicMock()
        mock_session.get_client.return_value = mock_client
        with patch.dict("sys.modules", {"aws_session": mock_session}):
            provider = AwsProvider()
            result = provider.kill_job(
                {"type": "ec2", "instance_id": "i-abc", "region": "us-west-2", "action": "terminate"},
                reason="overrun",
            )
        assert result.killed is True
        assert result.detail == "terminate:i-abc"
        mock_client.terminate_instances.assert_called_once_with(InstanceIds=["i-abc"])

    def test_live_kill_failure(self, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "false")
        from providers.aws import AwsProvider

        mock_client = MagicMock()
        mock_client.stop_instances.side_effect = Exception("access denied")
        mock_session = MagicMock()
        mock_session.get_client.return_value = mock_client
        with patch.dict("sys.modules", {"aws_session": mock_session}):
            provider = AwsProvider()
            result = provider.kill_job(
                {"type": "ec2", "instance_id": "i-abc"},
                reason="test",
            )
        assert result.killed is False
        assert "access denied" in result.error

    def test_fetch_billed_costs(self, monkeypatch):
        from datetime import datetime
        from providers.aws import AwsProvider

        mock_client = MagicMock()
        mock_client.get_cost_and_usage.return_value = {
            "ResultsByTime": [{
                "TimePeriod": {"Start": "2026-01-01", "End": "2026-01-02"},
                "Groups": [{
                    "Keys": ["Amazon Elastic Compute Cloud - Compute"],
                    "Metrics": {
                        "UnblendedCost": {"Amount": "19.32", "Unit": "USD"},
                        "UsageQuantity": {"Amount": "24", "Unit": "Hrs"},
                    },
                }],
            }],
        }
        mock_session = MagicMock()
        mock_session.get_client.return_value = mock_client
        with patch.dict("sys.modules", {"aws_session": mock_session}):
            provider = AwsProvider()
            costs = provider.fetch_billed_costs("test-proj", datetime(2026, 1, 1), datetime(2026, 1, 2))

        assert len(costs) == 1
        assert costs[0].provider == "Amazon Elastic Compute Cloud - Compute"
        assert costs[0].cost_usd == 19.32
        assert costs[0].source == "cost_explorer"

    def test_fetch_billed_costs_error_returns_empty(self, monkeypatch):
        from datetime import datetime
        from providers.aws import AwsProvider

        mock_client = MagicMock()
        mock_client.get_cost_and_usage.side_effect = Exception("throttled")
        mock_session = MagicMock()
        mock_session.get_client.return_value = mock_client
        with patch.dict("sys.modules", {"aws_session": mock_session}):
            provider = AwsProvider()
            costs = provider.fetch_billed_costs("test-proj", datetime(2026, 1, 1), datetime(2026, 1, 2))

        assert costs == []
