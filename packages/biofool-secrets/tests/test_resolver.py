"""Resolver chain tests — all remote access mocked."""

from __future__ import annotations

import logging

import biofool_secrets
from biofool_secrets import gsm, proton, resolver


def test_env_wins_over_remote(monkeypatch):
    monkeypatch.setenv("MY_KEY", "env-value")
    called = []
    monkeypatch.setattr(gsm, "access", lambda n, p=None: called.append(n) or "sm-value")
    assert resolver.resolve_secret("MY_KEY") == "env-value"
    assert called == []  # no remote call when env resolves


def test_alias_env_order(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "via-alias")
    assert resolver.resolve_secret("BRAVE_SEARCH_API_KEY") == "via-alias"


def test_gsm_fallback_per_candidate(monkeypatch):
    monkeypatch.setenv("SECRETS_BACKEND", "auto")
    store = {"BRAVE_ANSWERS_API_KEY": "sm-value"}
    monkeypatch.setattr(gsm, "access", lambda n, p=None: store.get(n))
    assert resolver.resolve_secret("BRAVE_SEARCH_API_KEY") == "sm-value"


def test_secrets_backend_env_blocks_remote(monkeypatch):
    monkeypatch.setenv("SECRETS_BACKEND", "env")
    called = []
    monkeypatch.setattr(gsm, "access", lambda n, p=None: called.append(n) or "x")
    monkeypatch.setattr(proton, "get_secret", lambda n, vault=None: called.append(n) or "x")
    monkeypatch.setenv("REAL_KEY", "v")
    assert resolver.resolve_secret("REAL_KEY") == "v"
    assert resolver.resolve_secret("MISSING") == ""
    assert called == []


def test_use_secret_manager_false_forces_env(monkeypatch):
    monkeypatch.setenv("USE_SECRET_MANAGER", "false")
    assert resolver.backend() == "env"


def test_unrecognized_backend_fails_closed(monkeypatch, caplog):
    # A typo'd kill-switch must never silently enable remote lookups.
    import logging
    monkeypatch.setenv("SECRETS_BACKEND", "evn")
    with caplog.at_level(logging.WARNING):
        assert resolver.backend() == "env"
    assert "secrets_backend_unrecognized" in caplog.text


def test_unrecognized_use_secret_manager_fails_closed(monkeypatch, caplog):
    import logging
    monkeypatch.setenv("USE_SECRET_MANAGER", "banana")
    with caplog.at_level(logging.WARNING):
        assert resolver.backend() == "env"
    assert "use_secret_manager_unrecognized" in caplog.text


def test_default_returned_on_miss():
    assert resolver.resolve_secret("NOPE", "fallback") == "fallback"


def test_project_scoped_aliases(monkeypatch):
    monkeypatch.setenv("SECRETS_BACKEND", "auto")
    seen = []
    monkeypatch.setattr(gsm, "access", lambda n, p=None: seen.append(n) or None)
    resolver.resolve_secret("GEMINI_API_KEY", project="AIRichardMoon")
    assert "coaching-gemini-api-key" in seen
    # Without the project scope the coaching-* id is never tried
    seen.clear()
    resolver.resolve_secret("GEMINI_API_KEY")
    assert "coaching-gemini-api-key" not in seen


def test_project_env_var_selects_scope(monkeypatch):
    monkeypatch.setenv("SECRETS_BACKEND", "auto")
    monkeypatch.setenv("BIOFOOL_SECRETS_PROJECT", "AIRichardMoon")
    seen = []
    monkeypatch.setattr(gsm, "access", lambda n, p=None: seen.append(n) or None)
    resolver.resolve_secret("SMTP_FROM")
    assert "coaching-smtp-from" in seen


def test_negative_cache(monkeypatch):
    monkeypatch.setenv("SECRETS_BACKEND", "auto")
    calls = []
    monkeypatch.setattr(gsm, "access", lambda n, p=None: calls.append(n) or None)
    resolver.resolve_secret("MISSING")
    resolver.resolve_secret("MISSING")
    assert calls == ["MISSING"]  # second call hit negative cache


def test_prefer_remote_puts_gsm_first(monkeypatch):
    monkeypatch.setenv("SECRETS_BACKEND", "auto")
    monkeypatch.setenv("SECRETS_PREFER_REMOTE", "true")
    monkeypatch.setenv("MY_KEY", "env-value")
    monkeypatch.setattr(gsm, "access", lambda n, p=None: "sm-value")
    assert resolver.resolve_secret("MY_KEY") == "sm-value"


def test_prefer_remote_kwarg_overrides_env(monkeypatch):
    """CloudManagement's enabled mode passes prefer_remote=True explicitly —
    the kwarg must win over the env var in both directions (#129)."""
    monkeypatch.setenv("SECRETS_BACKEND", "auto")
    monkeypatch.setenv("MY_KEY", "env-value")
    monkeypatch.setattr(gsm, "access", lambda n, p=None: "sm-value")
    assert resolver.resolve_secret("MY_KEY", prefer_remote=True) == "sm-value"
    monkeypatch.setenv("SECRETS_PREFER_REMOTE", "true")
    assert resolver.resolve_secret("MY_KEY", prefer_remote=False) == "env-value"


def test_deprecated_alias_resolves_canonical(monkeypatch, tmp_path, caplog):
    registry = tmp_path / "dep.yaml"
    registry.write_text(
        "aliases:\n  - alias: OLD_NAME\n    canonical: NEW_NAME\n"
        "    deprecated_since: '2026-01-01'\n    sunset_on: '2027-01-01'\n"
    )
    monkeypatch.setenv("BIOFOOL_SECRETS_DEPRECATED_FILE", str(registry))
    monkeypatch.setenv("NEW_NAME", "v")
    with caplog.at_level(logging.WARNING):
        assert resolver.resolve_secret("OLD_NAME") == "v"
    assert "deprecated_secret_resolved" in caplog.text


def test_list_names_blocked_in_env_backend(monkeypatch):
    monkeypatch.setenv("SECRETS_BACKEND", "env")
    monkeypatch.setattr(gsm, "list_names", lambda p=None: ["A"])
    assert resolver.list_secret_names() == []


def test_fetch_secret_backend_gated_and_cached(monkeypatch):
    """fetch_secret bypasses aliases but honors the backend switch and the
    positive/negative caches (story_graph key-discovery compat)."""
    monkeypatch.setenv("SECRETS_BACKEND", "env")
    monkeypatch.setattr(gsm, "access", lambda n, p=None: "v")
    assert resolver.fetch_secret("X") is None
    monkeypatch.setenv("SECRETS_BACKEND", "gcloud")
    calls = []
    monkeypatch.setattr(gsm, "access", lambda n, p=None: calls.append(n) or "v")
    assert resolver.fetch_secret("X") == "v"
    assert resolver.fetch_secret("X") == "v"
    assert calls == ["X"]


def test_resolution_report_never_returns_value(monkeypatch):
    monkeypatch.setenv("MY_KEY", "supersecret")
    report = resolver.resolution_report("MY_KEY")
    assert report["resolved_from"] == "env:MY_KEY"
    assert "supersecret" not in str(report)


def test_get_alias_matches_resolve(monkeypatch):
    monkeypatch.setenv("X1", "v1")
    assert biofool_secrets.get("X1") == "v1"
