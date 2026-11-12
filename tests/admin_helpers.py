"""Shared fixtures and factories for the admin_routes test files.

Split out of ``test_admin_routes.py`` during the ≤250-line refactor —
pure structural move, no behavior change. Mocks the GCP client libraries
and imports ``main`` exactly as the original module header did.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

# Reconcile compares billed cost for [now-48h, now-24h). Actuals used in the
# tests must fall inside that window (CISO-121).
_IN_RECONCILE_WINDOW = (datetime.now(timezone.utc) - timedelta(hours=36)).isoformat()

# Mock GCP client libraries before importing main — same as test_killswitch.py.
for _mod in ("google.cloud.run_v2", "google.cloud.scheduler_v1",
             "google.cloud.compute_v1", "google.cloud.billing_v1",
             "google.cloud.api_keys_v2", "google.cloud.container_v1"):
    sys.modules[_mod] = MagicMock()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import main  # noqa: E402
import registry  # noqa: E402
import intent  # noqa: E402


@pytest.fixture
def app():
    main.app.config["TESTING"] = True
    return main.app.test_client()


def make_account(**kwargs):
    defaults = dict(
        project_id="proj-a",
        billing_account_id="01AB-23CD-EF45",
        owner_email="a@example.com",
        allowlist=False,
        budget_amount_usd=10,
    )
    defaults.update(kwargs)
    return registry.Account(**defaults)


def make_intent(**kwargs):
    defaults = dict(
        intent_id="intent-1",
        project_id="proj-a",
        status="running",
    )
    defaults.update(kwargs)
    return intent.Intent(**defaults)
