"""Shared helpers for the killswitch test files.

Split out of ``test_killswitch.py`` during the ≤250-line refactor — pure
structural move, no behavior change. Mocks the GCP client libraries and
imports ``main`` exactly as the original module header did, then provides
the ``make_envelope``/``make_alert`` payload builders.
"""

import base64
import json
import os
import sys
from unittest.mock import MagicMock

# Mock GCP client libraries before importing main — tests don't call real APIs
for _mod in ("google.cloud.run_v2", "google.cloud.scheduler_v1",
             "google.cloud.compute_v1", "google.cloud.billing_v1",
             "google.cloud.api_keys_v2", "google.cloud.container_v1"):
    sys.modules[_mod] = MagicMock()

# Ensure main.py is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import main  # noqa: E402


def make_envelope(data: dict, msg_id: str = "test-123") -> dict:
    """Build a Pub/Sub push envelope."""
    encoded = base64.b64encode(json.dumps(data).encode()).decode()
    return {
        "message": {
            "data": encoded,
            "messageId": msg_id,
            "publishTime": "2025-01-01T00:00:00Z",
        },
        "subscription": "projects/test/subscriptions/killswitch-push",
    }


def make_alert(
    threshold: float = 100,
    actual: float = 5.0,
    forecast: float = 5.0,
    budget: float = 5.0,
    alert_type: str = "budget",
    projects: list[str] | None = None,
) -> dict:
    return {
        "alertType": alert_type,
        "thresholdPercent": threshold,
        "actualCost": actual,
        "forecastCost": forecast,
        "budgetAmount": budget,
        "currency": "USD",
        "budgetName": "test-budget",
        "projectIds": projects or ["dev-project-1"],
    }
