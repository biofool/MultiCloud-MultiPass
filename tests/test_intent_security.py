"""Security regression tests for the intent/actual protocol (issue #5
adversarial review) and the http_callback provider's SSRF guard.

Split out of ``test_intent.py`` during the ≤250-line refactor — pure
structural move, no behavior change.
"""

import os
import sys

# Ensure the project root and the tests dir (for intent_fixtures) are on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from intent_fixtures import yaml_backend, app  # noqa: E402,F401  (fixtures)


class TestIssue5Security:
    """Regression tests for the 2026-10-12 adversarial review (issue #5)."""

    def _declare_with_kill(self, app):
        resp = app.post("/api/v1/intent",
            json={
                "project_id": "test-project",
                "job_id": "job-sec",
                "expected_calls": 10,
                "kill": {
                    "type": "http_callback",
                    "url": "https://example.com/kill",
                    "headers": {"Authorization": "Bearer callback-secret"},
                },
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        return resp.get_json()["intent_id"]

    def test_report_actual_rejects_cross_tenant_intent(self, app):
        """A token valid for project A must not report (or kill) on B's intent."""
        intent_id = self._declare_with_kill(app)
        resp = app.post("/api/v1/actual",
            json={
                "project_id": "other-project",   # different project, valid global token
                "intent_id": intent_id,          # ...but the intent belongs to test-project
                "actual_calls": 1,
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 404

    def test_intent_get_redacts_kill_credentials(self, app):
        intent_id = self._declare_with_kill(app)
        resp = app.get(f"/api/v1/intent/{intent_id}")
        assert resp.status_code == 200
        kill = resp.get_json()["kill"]
        assert kill == {"type": "http_callback"}

    def test_intents_list_redacts_kill_credentials(self, app):
        self._declare_with_kill(app)
        resp = app.get("/api/v1/intents")
        assert resp.status_code == 200
        for item in resp.get_json()["intents"]:
            assert "headers" not in (item.get("kill") or {})
            assert "url" not in (item.get("kill") or {})

    def test_bad_numeric_fields_return_400_not_500(self, app):
        resp = app.post("/api/v1/intent",
            json={"project_id": "test-project", "expected_calls": "abc"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 400
        intent_id = self._declare_with_kill(app)
        resp = app.post("/api/v1/actual",
            json={
                "project_id": "test-project", "intent_id": intent_id,
                "actual_cost_usd": "not-a-number",
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 400

    def test_failed_kill_marks_kill_failed_not_killed(self, app, monkeypatch):
        import intent as intent_mod
        import intent_kill
        from providers.base import KillResult
        from providers import registry as provider_registry

        monkeypatch.setattr(
            provider_registry, "kill_job",
            lambda desc, reason: KillResult(killed=False, job_id=desc.get("job_id", ""), error="nope"),
        )
        i = intent_mod.Intent(
            intent_id="int_killfail", project_id="test-project",
            job_id="job-x", status="running",
            kill={"type": "http_callback", "url": "https://example.com/kill"},
        )
        intent_mod.save_intent(i)
        result = intent_kill.kill_intent(i, reason="test", rule="test")
        assert result["killed"] is False
        assert intent_mod.get_intent("int_killfail").status == "kill_failed"

    def test_protected_project_kill_blocked(self, app, monkeypatch):
        import intent as intent_mod
        import intent_kill
        import main

        monkeypatch.setattr(main, "SELF_PROJECT_ID", "hub-project")
        i = intent_mod.Intent(
            intent_id="int_selfkill", project_id="hub-project",
            job_id="job-x", status="running",
            kill={"type": "http_callback", "url": "https://example.com/kill"},
        )
        result = intent_kill.kill_intent(i, reason="overrun", rule="test")
        assert result["killed"] is False
        assert "protected" in result["reason"]

    def test_project_month_spend_uses_latest_per_intent(self, app):
        """Cumulative incremental reports must not double-count."""
        from datetime import datetime, timezone
        import intent as intent_mod
        from intent_detection import project_month_spend

        now_iso = datetime.now(timezone.utc).isoformat()
        for intent_id, seqs in (
            ("int_a", [(1, 10.0), (2, 20.0)]),   # cumulative: latest is 20
            ("int_b", [(1, 5.0)]),
        ):
            intent_mod.save_intent(intent_mod.Intent(
                intent_id=intent_id, project_id="test-project",
                created_at=now_iso, updated_at=now_iso,
            ))
            for seq, cost in seqs:
                intent_mod.save_actual(intent_mod.Actual(
                    actual_id=f"act_{intent_id}_{seq}", intent_id=intent_id,
                    project_id="test-project", actual_cost_usd=cost,
                    sequence=seq - 1, client_seq=seq, created_at=now_iso,
                ))
        assert project_month_spend("test-project") == 25.0


class TestHttpCallbackSsrf:
    def test_metadata_ip_blocked(self):
        from providers.http_callback import _host_is_public
        ok, _ = _host_is_public("http://169.254.169.254/latest/meta-data")
        assert ok is False

    def test_loopback_blocked(self):
        from providers.http_callback import _host_is_public
        ok, _ = _host_is_public("http://127.0.0.1:8080/admin")
        assert ok is False

    def test_public_ip_allowed(self, monkeypatch):
        import socket as _socket
        import providers.http_callback as mod
        monkeypatch.setattr(
            _socket, "getaddrinfo",
            lambda host, port: [(2, 1, 6, "", ("93.184.216.34", 0))],
        )
        ok, _ = mod._host_is_public("https://example.com/kill")
        assert ok is True
