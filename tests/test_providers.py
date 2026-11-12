"""Tests for the provider abstraction (providers/)."""

import os
import sys
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers.base import BilledCost, KillResult, CostProvider
from providers.http_callback import HttpCallbackProvider
from providers.registry import kill_job, _get_provider


class TestHttpCallbackProvider:
    def test_dry_run_kill(self, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "true")
        provider = HttpCallbackProvider()

        result = provider.kill_job(
            {"type": "http_callback", "url": "http://example.com/kill", "job_id": "job-1"},
            reason="test",
        )
        assert result.killed is True
        assert result.action == "http_callback"
        assert result.detail == "dry_run"

    def test_missing_url(self):
        provider = HttpCallbackProvider()
        result = provider.kill_job({"type": "http_callback", "job_id": "job-1"}, reason="test")
        assert result.killed is False
        assert "no url" in result.error

    @patch("requests.request")
    def test_live_kill_success(self, mock_request, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "false")
        # Keep the unit test offline — the SSRF host check does real DNS.
        monkeypatch.setattr("providers.http_callback._host_is_public", lambda url: (True, ""))

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raw.read.return_value = b'{"killed": true}'
        mock_resp.raise_for_status = MagicMock()
        # The provider consumes the response as a context manager.
        mock_request.return_value.__enter__.return_value = mock_resp

        provider = HttpCallbackProvider()
        result = provider.kill_job(
            {"type": "http_callback", "url": "http://example.com/kill", "job_id": "job-1", "headers": {}},
            reason="overrun",
        )
        assert result.killed is True

    @patch("requests.request")
    def test_live_kill_failure(self, mock_request, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "false")
        monkeypatch.setattr("providers.http_callback._host_is_public", lambda url: (True, ""))

        mock_request.side_effect = Exception("connection refused")

        provider = HttpCallbackProvider()
        result = provider.kill_job(
            {"type": "http_callback", "url": "http://example.com/kill", "job_id": "job-1"},
            reason="overrun",
        )
        assert result.killed is False
        assert "connection refused" in result.error


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


class TestProviderRegistry:
    def test_get_gcp_provider(self):
        provider = _get_provider("gcp")
        assert provider.cloud == "gcp"

    def test_get_openstack_provider(self):
        provider = _get_provider("openstack")
        assert provider.cloud == "openstack"

    def test_get_aws_provider(self):
        provider = _get_provider("aws")
        assert provider.cloud == "aws"

    def test_get_http_callback_provider(self):
        provider = _get_provider("http_callback")
        assert provider.cloud == "generic"

    def test_get_unknown_falls_back(self):
        provider = _get_provider("unknown_cloud")
        assert provider.cloud == "generic"

    def test_kill_job_routes_http_callback(self, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "true")
        import importlib
        import providers.http_callback
        import providers.registry
        importlib.reload(providers.http_callback)
        importlib.reload(providers.registry)

        result = kill_job(
            {"type": "http_callback", "url": "http://example.com/kill", "job_id": "job-1"},
            reason="test",
        )
        assert result.killed is True

    def test_kill_job_routes_gcp_cloud_run(self, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "true")
        import importlib
        import providers.gcp
        import providers.registry
        importlib.reload(providers.gcp)
        importlib.reload(providers.registry)

        result = kill_job(
            {"type": "cloud_run", "project_id": "test-proj", "service": "test-svc", "region": "us-central1"},
            reason="test",
        )
        assert result.killed is True
        assert result.action == "cloud_run"

    def test_kill_job_routes_gcp_cloud_scheduler(self, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "true")
        import importlib
        import providers.gcp
        import providers.registry
        importlib.reload(providers.gcp)
        importlib.reload(providers.registry)

        result = kill_job(
            {"type": "cloud_scheduler", "project_id": "test-proj", "job": "test-job", "location": "us-central1"},
            reason="test",
        )
        assert result.killed is True
        assert result.action == "cloud_scheduler"

    def test_kill_job_routes_aws_ec2(self, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "true")
        import importlib
        import providers.aws
        import providers.registry
        importlib.reload(providers.aws)
        importlib.reload(providers.registry)

        result = kill_job(
            {"type": "ec2", "instance_id": "i-abc", "region": "us-east-1"},
            reason="test",
        )
        assert result.killed is True
        assert result.action == "ec2"

    def test_kill_job_routes_gcp_gce(self, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "true")
        import importlib
        import providers.gcp
        import providers.registry
        importlib.reload(providers.gcp)
        importlib.reload(providers.registry)

        result = kill_job(
            {"type": "gce", "project_id": "test-proj", "instance": "test-vm", "zone": "us-central1-a"},
            reason="test",
        )
        assert result.killed is True
        assert result.action == "gce"


class TestGcpProviderValidation:
    def test_cloud_run_missing_project(self, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "true")
        import importlib
        import providers.gcp
        importlib.reload(providers.gcp)
        provider = providers.gcp.GcpProvider()

        result = provider.kill_job(
            {"type": "cloud_run", "service": "test-svc"},
            reason="test",
        )
        assert result.killed is False
        assert "missing" in result.error

    def test_cloud_run_missing_service(self, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "true")
        import importlib
        import providers.gcp
        importlib.reload(providers.gcp)
        provider = providers.gcp.GcpProvider()

        result = provider.kill_job(
            {"type": "cloud_run", "project_id": "test-proj"},
            reason="test",
        )
        assert result.killed is False
        assert "missing" in result.error

    def test_unknown_kill_type(self, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "true")
        import importlib
        import providers.gcp
        importlib.reload(providers.gcp)
        provider = providers.gcp.GcpProvider()

        result = provider.kill_job({"type": "unknown"}, reason="test")
        assert result.killed is False
        assert "unknown" in result.error


class TestRegistryExtension:
    def test_account_multi_cloud_fields(self):
        import registry
        acct = registry.Account(
            project_id="test-os",
            cloud="openstack",
            openstack_project="454-123",
            openstack_regions=["your-region-1"],
            budget_amount_usd=20,
        )
        d = acct.to_dict()
        assert d["cloud"] == "openstack"
        assert d["openstack_project"] == "454-123"
        assert d["openstack_regions"] == ["your-region-1"]

        # Round-trip
        acct2 = registry.Account.from_dict(d)
        assert acct2.cloud == "openstack"
        assert acct2.openstack_project == "454-123"

    def test_account_gcp_with_jobs(self):
        import registry
        acct = registry.Account(
            project_id="test-gcp",
            cloud="gcp",
            gcp_project_id="my-gcp-project",
            jobs=[{"job_id_prefix": "scrape-", "kill": {"type": "http_callback", "url": "http://x"}}],
        )
        d = acct.to_dict()
        assert d["gcp_project_id"] == "my-gcp-project"
        assert len(d["jobs"]) == 1

        acct2 = registry.Account.from_dict(d)
        assert acct2.gcp_project_id == "my-gcp-project"
        assert acct2.jobs[0]["job_id_prefix"] == "scrape-"

    def test_account_defaults_to_gcp(self):
        import registry
        acct = registry.Account(project_id="test")
        assert acct.cloud == "gcp"
        assert acct.gcp_project_id == ""

    def test_account_aws_fields(self):
        import registry
        acct = registry.Account(
            project_id="test-aws",
            cloud="aws",
            aws_account_id="123456789012",
            aws_region="us-east-1",
            aws_access_key_secret="AWS_ACCESS_KEY_ID",
            budget_amount_usd=600,
            jobs=[{"job_id_prefix": "gpu-", "kill": {"type": "ec2", "instance_id": "i-abc", "action": "stop"}}],
        )
        d = acct.to_dict()
        assert d["cloud"] == "aws"
        assert d["aws_account_id"] == "123456789012"
        assert d["aws_region"] == "us-east-1"

        acct2 = registry.Account.from_dict(d)
        assert acct2.cloud == "aws"
        assert acct2.aws_account_id == "123456789012"
        assert acct2.aws_access_key_secret == "AWS_ACCESS_KEY_ID"
        assert acct2.jobs[0]["kill"]["type"] == "ec2"
