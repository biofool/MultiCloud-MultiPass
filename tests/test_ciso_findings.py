"""Regression tests for CISO-117, CISO-118, and CISO-121."""

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import intent  # noqa: E402,F401  (must load before intent_storage: circular import)
import intent_storage  # noqa: E402
import registry  # noqa: E402
import main  # noqa: E402

from test_intent import yaml_backend, app  # noqa: E402,F401  (fixtures)


def _register(project_id, gcp_project_id=""):
    registry.register_account(registry.Account(
        project_id=project_id,
        gcp_project_id=gcp_project_id,
        billing_account_id="01AB-23CD-EF45",
        owner_email="test@example.com",
        budget_amount_usd=100.0,
        quota_rpm_cap=6000,
        report_token_secret="CLOUDMANAGEMENT_REPORT_TOKEN",
    ))


class TestIntentIdCollision:  # CISO-117
    def test_other_project_cannot_overwrite_intent(self, app):
        _register("other-project")
        body = {"intent_id": "shared-id", "job_id": "j1", "provider": "p", "api": "a",
                "expected_calls": 10, "expected_cost_usd": 1.0}
        first = app.post("/api/v1/intent", json={**body, "project_id": "test-project"},
                         headers={"Authorization": "Bearer test-token"})
        assert first.status_code == 200

        second = app.post("/api/v1/intent", json={**body, "project_id": "other-project"},
                          headers={"Authorization": "Bearer test-token"})
        assert second.status_code == 409
        assert intent_storage.get_intent("shared-id").project_id == "test-project"


class TestSelfProjectAlias:  # CISO-118
    def test_alias_of_hub_is_self_project(self, yaml_backend):
        _register("aliased-project", gcp_project_id="hub-project")
        with patch.object(main, "SELF_PROJECT_ID", "hub-project"):
            assert main.is_self_project("aliased-project") is True
            assert main.is_self_project("hub-project") is True
            assert main.is_self_project("unrelated") is False

    def test_no_self_project_configured(self, yaml_backend):
        with patch.object(main, "SELF_PROJECT_ID", ""):
            assert main.is_self_project("anything") is False


class TestReconcileWindow:  # CISO-121
    def test_only_actuals_inside_window_count(self):
        from admin_routes import _in_window
        until = datetime(2026, 11, 5, tzinfo=timezone.utc)
        since = until - timedelta(hours=24)
        inside = (until - timedelta(hours=1)).isoformat()
        before = (since - timedelta(hours=1)).isoformat()
        assert _in_window(inside, since, until) is True
        assert _in_window(before, since, until) is False
        assert _in_window(until.isoformat(), since, until) is False  # end is exclusive

    def test_naive_and_bad_timestamps(self):
        from admin_routes import _in_window
        until = datetime(2026, 11, 5, tzinfo=timezone.utc)
        since = until - timedelta(hours=24)
        assert _in_window("2026-11-04T12:00:00", since, until) is True
        assert _in_window("", since, until) is False
        assert _in_window("not-a-date", since, until) is False
