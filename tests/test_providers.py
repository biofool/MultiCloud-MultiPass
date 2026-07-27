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

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b'{"killed": true}'
        mock_resp.json.return_value = {"killed": True}
        mock_resp.raise_for_status = MagicMock()
        mock_request.return_value = mock_resp

        provider = HttpCallbackProvider()
        result = provider.kill_job(
            {"type": "http_callback", "url": "http://example.com/kill", "job_id": "job-1", "headers": {}},
            reason="overrun",
        )
        assert result.killed is True

    @patch("requests.request")
    def test_live_kill_failure(self, mock_request, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "false")

        mock_request.side_effect = Exception("connection refused")

        provider = HttpCallbackProvider()
        result = provider.kill_job(
            {"type": "http_callback", "url": "http://example.com/kill", "job_id": "job-1"},
            reason="overrun",
        )
        assert result.killed is False
        assert "connection refused" in result.error


class TestProviderRegistry:
    def test_get_gcp_provider(self):
        provider = _get_provider("gcp")
        assert provider.cloud == "gcp"

    def test_get_openstack_provider(self):
        provider = _get_provider("openstack")
        assert provider.cloud == "openstack"

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
