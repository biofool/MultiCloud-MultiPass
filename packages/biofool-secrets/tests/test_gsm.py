"""GSM layer tests — SDK/CLI seams mocked; no real secret access."""

from __future__ import annotations

import subprocess

from biofool_secrets import gsm


def _run_result(cmd, rc=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(cmd, rc, stdout=stdout, stderr=stderr)


def test_access_gcloud_permission_denied_warns(monkeypatch, caplog):
    """A non-not-found gcloud failure must WARN — auth/permission problems
    must not masquerade as 'secret missing' (bootstrap_env lineage check)."""
    import logging

    monkeypatch.setattr(gsm, "_sdk", lambda: None)
    monkeypatch.setattr(gsm, "_gcloud", lambda: "gcloud")

    def fake_run(cmd, **kw):
        return _run_result(cmd, rc=1, stderr="ERROR: PERMISSION_DENIED: nope")

    monkeypatch.setattr(gsm.subprocess, "run", fake_run)
    with caplog.at_level(logging.WARNING, logger="biofool_secrets.gsm"):
        assert gsm.access("SOME_KEY", "proj") is None
    assert any("PERMISSION_DENIED" in r.message for r in caplog.records)


def test_access_gcloud_not_found_is_quiet(monkeypatch, caplog):
    import logging

    monkeypatch.setattr(gsm, "_sdk", lambda: None)
    monkeypatch.setattr(gsm, "_gcloud", lambda: "gcloud")

    def fake_run(cmd, **kw):
        return _run_result(cmd, rc=1, stderr="ERROR: NOT_FOUND: Secret does not exist")

    monkeypatch.setattr(gsm.subprocess, "run", fake_run)
    with caplog.at_level(logging.WARNING, logger="biofool_secrets.gsm"):
        assert gsm.access("MISSING", "proj") is None
    assert not any(r.levelname == "WARNING" for r in caplog.records)


def test_push_secret_gcloud_success(monkeypatch):
    calls = []
    monkeypatch.setattr(gsm, "_gcloud", lambda: "gcloud")

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _run_result(cmd)

    monkeypatch.setattr(gsm.subprocess, "run", fake_run)
    monkeypatch.setattr(gsm, "_sdk", lambda: None)
    ok, detail = gsm.push_secret("K", "v", project="proj")
    assert (ok, detail) == (True, "imported")
    assert ["create", "K"] == calls[0][2:4] or "create" in calls[0]
    assert "versions" in calls[1] and "add" in calls[1]


def test_push_secret_gcloud_add_failure_reports(monkeypatch):
    monkeypatch.setattr(gsm, "_gcloud", lambda: "gcloud")

    def fake_run(cmd, **kw):
        if "versions" in cmd:
            return _run_result(cmd, rc=1, stderr="permission denied")
        return _run_result(cmd)

    monkeypatch.setattr(gsm.subprocess, "run", fake_run)
    ok, detail = gsm.push_secret("K", "v", project="proj")
    assert ok is False
    assert "permission denied" in detail


def test_push_secret_falls_back_to_sdk_when_no_gcloud(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.creates = []
            self.versions = []

        def create_secret(self, request):
            self.creates.append(request)

        def add_secret_version(self, request):
            self.versions.append(request)

    client = FakeClient()
    monkeypatch.setattr(gsm, "_gcloud", lambda: None)
    monkeypatch.setattr(gsm, "_sdk", lambda: client)
    ok, detail = gsm.push_secret("AIKIDO_API", "tok", project="proj")
    assert (ok, detail) == (True, "imported")
    assert client.creates[0]["secret_id"] == "AIKIDO_API"
    assert client.versions[0]["payload"]["data"] == b"tok"


def test_push_secret_sdk_fallback_on_gcloud_oserror(monkeypatch):
    """WSL: a Windows gcloud that execve rejects (EACCES) → SDK path."""
    class FakeClient:
        def create_secret(self, request):
            raise Exception("already exists")

        def add_secret_version(self, request):
            self.added = request

    client = FakeClient()
    monkeypatch.setattr(gsm, "_gcloud", lambda: "gcloud")
    monkeypatch.setattr(gsm, "_sdk", lambda: client)

    def fake_run(cmd, **kw):
        raise PermissionError(13, "Permission denied", "gcloud")

    monkeypatch.setattr(gsm.subprocess, "run", fake_run)
    ok, detail = gsm.push_secret("K", "v", project="proj")
    assert (ok, detail) == (True, "imported")
    assert client.added["payload"]["data"] == b"v"


def test_push_secret_no_tooling_reports(monkeypatch):
    monkeypatch.setattr(gsm, "_gcloud", lambda: None)
    monkeypatch.setattr(gsm, "_sdk", lambda: None)
    ok, detail = gsm.push_secret("K", "v", project="proj")
    assert ok is False
    assert "google-cloud-secretmanager" in detail


def test_push_secret_never_logs_value(monkeypatch, caplog):
    import logging

    monkeypatch.setattr(gsm, "_gcloud", lambda: "gcloud")

    def fake_run(cmd, **kw):
        if "versions" in cmd:
            return _run_result(cmd, rc=1, stderr="denied")
        return _run_result(cmd)

    monkeypatch.setattr(gsm.subprocess, "run", fake_run)
    with caplog.at_level(logging.DEBUG, logger="biofool_secrets.gsm"):
        gsm.push_secret("K", "the-secret-value", project="proj")
    assert "the-secret-value" not in caplog.text
