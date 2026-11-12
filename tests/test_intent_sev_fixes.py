"""Regression tests for CloudManagement #198 security fixes ported to the
intent/actual routes and the kill path:

- report_actual accepts only client job outcomes as status
- negative numeric fields are rejected on declare and report
- a terminal intent status is not regressed by a later report
- kill is blocked on an account whose gcp_project_id is protected
- YAML writes are atomic (temp file, then rename)
"""

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import intent  # noqa: E402,F401  (must load before intent_storage: circular import)
import intent_storage  # noqa: E402
import registry  # noqa: E402

from test_intent import yaml_backend, app  # noqa: E402,F401  (fixtures)

HEADERS = {"Authorization": "Bearer test-token"}


def _declare(app, **extra):
    body = {
        "project_id": "test-project",
        "job_id": "job-sev-001",
        "provider": "google_places",
        "expected_calls": 500,
        "expected_cost_usd": 17.50,
    }
    body.update(extra)
    resp = app.post("/api/v1/intent", json=body, headers=HEADERS)
    return resp


def _report(app, intent_id, **extra):
    body = {
        "intent_id": intent_id,
        "project_id": "test-project",
        "actual_calls": 10,
        "actual_cost_usd": 0.35,
        "status": "completed",
    }
    body.update(extra)
    return app.post("/api/v1/actual", json=body, headers=HEADERS)


class TestReportedStatus:
    def test_lifecycle_status_rejected(self, app):
        intent_id = _declare(app).get_json()["intent_id"]
        resp = _report(app, intent_id, status="killed")
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "invalid status"

    def test_client_outcome_accepted(self, app):
        intent_id = _declare(app).get_json()["intent_id"]
        assert _report(app, intent_id, status="running").status_code == 200


class TestNonNegativeNumerics:
    def test_negative_declare_rejected(self, app):
        resp = _declare(app, expected_calls=-1)
        assert resp.status_code == 400
        assert "must be >= 0" in resp.get_json()["error"]

    def test_negative_report_rejected(self, app):
        intent_id = _declare(app).get_json()["intent_id"]
        resp = _report(app, intent_id, actual_cost_usd=-5)
        assert resp.status_code == 400


class TestTerminalStatusNotRegressed:
    def test_killed_intent_keeps_status(self, app):
        intent_id = _declare(app).get_json()["intent_id"]
        intent = intent_storage.get_intent(intent_id)
        intent.status = "killed"
        intent_storage.save_intent(intent)

        resp = _report(app, intent_id, status="running")
        assert resp.status_code == 200
        assert intent_storage.get_intent(intent_id).status == "killed"


class TestAliasedKillProtection:
    def test_kill_blocked_when_gcp_project_is_self(self, app, yaml_backend):
        import intent_kill
        import main

        acct = registry.get_account("test-project")
        acct.gcp_project_id = "hub-project"
        registry.register_account(acct)

        intent_id = _declare(app).get_json()["intent_id"]
        intent = intent_storage.get_intent(intent_id)

        with patch.object(main, "SELF_PROJECT_ID", "hub-project"), \
                patch("providers.registry.kill_job") as kill_job:
            result = intent_kill.kill_intent(intent, reason="overrun")

        assert result == {"killed": False, "reason": "project is protected",
                          "project_id": "test-project"}
        kill_job.assert_not_called()


class TestAtomicYamlWrite:
    def test_save_leaves_no_temp_file(self, tmp_path):
        target = tmp_path / "items.yaml"
        intent_storage._yaml_save(str(target), "items", [{"a": 1}])
        assert target.exists()
        assert not (tmp_path / "items.yaml.tmp").exists()

    def test_failed_write_keeps_previous_file(self, tmp_path):
        target = tmp_path / "items.yaml"
        intent_storage._yaml_save(str(target), "items", [{"a": 1}])
        before = target.read_text()
        with patch("yaml.safe_dump", side_effect=RuntimeError("disk full")):
            with pytest.raises(RuntimeError):
                intent_storage._yaml_save(str(target), "items", [{"a": 2}])
        assert target.read_text() == before
