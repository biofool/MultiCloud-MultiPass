"""Tests for secret_loader.py — the parallel GSM path.

Verifies that:
- When USE_SECRET_MANAGER is not "true", get() == os.environ.get() (no GSM call).
- When USE_SECRET_MANAGER=true, get() resolves from GSM with caching.
- Negative caching prevents repeated GSM calls for missing secrets.
- Env var fallback works when GSM is unavailable.
- Trailing newline is stripped but other whitespace is preserved.
"""
from __future__ import annotations

import importlib
import os
import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def loader(monkeypatch):
    """Reload secret_loader with clean state for each test."""
    monkeypatch.delenv("USE_SECRET_MANAGER", raising=False)
    monkeypatch.delenv("SECRET_PROJECT_ID", raising=False)
    monkeypatch.delenv("PROJECT_ID", raising=False)
    if "secret_loader" in sys.modules:
        del sys.modules["secret_loader"]
    import secret_loader
    importlib.reload(secret_loader)
    return secret_loader


class TestDisabled:
    """When USE_SECRET_MANAGER is not 'true', behaviour == os.environ.get."""

    def test_returns_env_var(self, loader, monkeypatch):
        monkeypatch.setenv("MY_SECRET", "env-value")
        assert loader.get("MY_SECRET") == "env-value"

    def test_returns_default_when_unset(self, loader, monkeypatch):
        monkeypatch.delenv("MISSING_SECRET", raising=False)
        assert loader.get("MISSING_SECRET", "fallback") == "fallback"

    def test_no_gsm_call(self, loader, monkeypatch):
        monkeypatch.setenv("MY_SECRET", "env-value")
        with patch.object(loader, "_sm_access") as mock_sm:
            assert loader.get("MY_SECRET") == "env-value"
            mock_sm.assert_not_called()

    def test_is_enabled_false(self, loader):
        assert loader.is_enabled() is False


class TestEnabled:
    """When USE_SECRET_MANAGER=true, resolves from GSM."""

    @pytest.fixture
    def enabled_loader(self, monkeypatch):
        monkeypatch.setenv("USE_SECRET_MANAGER", "true")
        monkeypatch.setenv("SECRET_PROJECT_ID", "test-project")
        if "secret_loader" in sys.modules:
            del sys.modules["secret_loader"]
        import secret_loader
        importlib.reload(secret_loader)
        return secret_loader

    def test_is_enabled_true(self, enabled_loader):
        assert enabled_loader.is_enabled() is True

    def test_resolves_from_gsm(self, enabled_loader, monkeypatch):
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.payload.data = b"gsm-value"
        mock_client.access_secret_version.return_value = mock_resp
        monkeypatch.setattr(enabled_loader, "_sm_client", mock_client)

        assert enabled_loader.get("MY_SECRET") == "gsm-value"
        mock_client.access_secret_version.assert_called_once_with(
            name="projects/test-project/secrets/MY_SECRET/versions/latest"
        )

    def test_positive_cache(self, enabled_loader, monkeypatch):
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.payload.data = b"gsm-value"
        mock_client.access_secret_version.return_value = mock_resp
        monkeypatch.setattr(enabled_loader, "_sm_client", mock_client)

        enabled_loader.get("CACHED_SECRET")
        enabled_loader.get("CACHED_SECRET")
        enabled_loader.get("CACHED_SECRET")
        assert mock_client.access_secret_version.call_count == 1

    def test_negative_cache_skips_gsm(self, enabled_loader, monkeypatch):
        mock_client = MagicMock()
        mock_client.access_secret_version.side_effect = RuntimeError("not found")
        monkeypatch.setattr(enabled_loader, "_sm_client", mock_client)
        monkeypatch.setenv("FALLBACK_SECRET", "env-fallback")

        # First call: GSM fails, falls back to env var
        assert enabled_loader.get("FALLBACK_SECRET") == "env-fallback"
        # Second call: GSM should be skipped (negative cache), env var returned
        assert enabled_loader.get("FALLBACK_SECRET") == "env-fallback"
        assert mock_client.access_secret_version.call_count == 1

    def test_env_fallback_when_gsm_fails(self, enabled_loader, monkeypatch):
        mock_client = MagicMock()
        mock_client.access_secret_version.side_effect = RuntimeError("not found")
        monkeypatch.setattr(enabled_loader, "_sm_client", mock_client)
        monkeypatch.setenv("ENV_FALLBACK", "from-env")

        assert enabled_loader.get("ENV_FALLBACK") == "from-env"

    def test_default_when_all_fail(self, enabled_loader, monkeypatch):
        mock_client = MagicMock()
        mock_client.access_secret_version.side_effect = RuntimeError("not found")
        monkeypatch.setattr(enabled_loader, "_sm_client", mock_client)
        monkeypatch.delenv("NO_WHERE_SECRET", raising=False)

        assert enabled_loader.get("NO_WHERE_SECRET", "default-val") == "default-val"

    def test_invalidate_cache(self, enabled_loader, monkeypatch):
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.payload.data = b"first"
        mock_client.access_secret_version.return_value = mock_resp
        monkeypatch.setattr(enabled_loader, "_sm_client", mock_client)

        assert enabled_loader.get("INVALIDATE_ME") == "first"
        enabled_loader.invalidate_cache("INVALIDATE_ME")
        mock_resp.payload.data = b"second"
        assert enabled_loader.get("INVALIDATE_ME") == "second"
        assert mock_client.access_secret_version.call_count == 2

    def test_full_resource_name_passed_through(self, enabled_loader, monkeypatch):
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.payload.data = b"gsm-value"
        mock_client.access_secret_version.return_value = mock_resp
        monkeypatch.setattr(enabled_loader, "_sm_client", mock_client)

        full_name = "projects/other-project/secrets/MY_KEY/versions/latest"
        enabled_loader.get(full_name)
        mock_client.access_secret_version.assert_called_once_with(name=full_name)

    def test_trailing_newline_stripped(self, enabled_loader, monkeypatch):
        """Only a single trailing newline should be stripped, not all whitespace."""
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.payload.data = b"secret-value\n"
        mock_client.access_secret_version.return_value = mock_resp
        monkeypatch.setattr(enabled_loader, "_sm_client", mock_client)

        assert enabled_loader.get("NEWLINE_SECRET") == "secret-value"

    def test_leading_whitespace_preserved(self, enabled_loader, monkeypatch):
        """Leading/trailing spaces must be preserved (not .strip()'d)."""
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.payload.data = b"  value with spaces  "
        mock_client.access_secret_version.return_value = mock_resp
        monkeypatch.setattr(enabled_loader, "_sm_client", mock_client)

        assert enabled_loader.get("SPACE_SECRET") == "  value with spaces  "
