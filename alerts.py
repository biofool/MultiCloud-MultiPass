"""Budget alert model, Pub/Sub parsing, and threshold evaluation for
the kill switch service.

Split out of the original monolithic ``main.py`` — pure structural move,
no behavior change. Logging goes through ``main.log`` (imported lazily
via ``import main``, not ``from main import log``) so this module never
caches a stale reference — consistent with how the rest of the split
reaches back into main's shared, sometimes test-mutated, state.
"""

from __future__ import annotations

import json
import base64
from dataclasses import dataclass, field
from typing import Any

import main

# Budget alert type constants from Cloud Billing
ALERT_TYPE_BUDGET = "budget"
ALERT_TYPE_FORECAST = "forecast"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class BudgetAlert:
    """Parsed budget alert from Pub/Sub."""
    alert_type: str          # "budget" or "forecast"
    budget_name: str
    threshold_percent: float
    actual_spend: float
    forecasted_spend: float
    budget_amount: float
    currency: str
    project_ids: list[str]
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_actual(self) -> bool:
        return self.alert_type == ALERT_TYPE_BUDGET

    @property
    def is_forecast(self) -> bool:
        return self.alert_type == ALERT_TYPE_FORECAST


# ---------------------------------------------------------------------------
# Alert parsing
# ---------------------------------------------------------------------------

def parse_pubsub_message(envelope: dict[str, Any]) -> BudgetAlert | None:
    """Parse a Pub/Sub push envelope into a BudgetAlert.

    Returns None if the message is malformed.
    """
    try:
        message = envelope["message"]
        data_b64 = message.get("data", "")
        raw = json.loads(base64.b64decode(data_b64).decode("utf-8"))
    except (KeyError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        main.log.error(json.dumps({"event": "parse_error", "error": str(exc)}))
        return None

    # Cloud Billing budget notification schema (costThresholdString or similar)
    try:
        alert_type = raw.get("alertType", ALERT_TYPE_BUDGET)
        threshold = float(raw.get("thresholdPercent", raw.get("threshold", 0)))
        actual = float(raw.get("actualCost", raw.get("costAmount", 0)))
        forecast = float(raw.get("forecastCost", raw.get("forecastAmount", 0)))
        budget_amt = float(raw.get("budgetAmount", 0))
        currency = raw.get("currency", raw.get("currencyCode", "USD"))
        budget_name = raw.get("budgetName", raw.get("displayName", "unknown"))

        # Projects can be in a list or single field
        project_ids = raw.get("projectIds", [])
        if isinstance(project_ids, str):
            project_ids = [project_ids]
        if not project_ids:
            # If no projects specified, the alert applies to the billing account
            project_ids = []

        return BudgetAlert(
            alert_type=alert_type,
            budget_name=budget_name,
            threshold_percent=threshold,
            actual_spend=actual,
            forecasted_spend=forecast,
            budget_amount=budget_amt,
            currency=currency,
            project_ids=project_ids,
            raw=raw,
        )
    except (ValueError, TypeError) as exc:
        main.log.error(json.dumps({"event": "parse_error", "error": str(exc), "raw": str(raw)[:500]}))
        return None


# ---------------------------------------------------------------------------
# Threshold evaluation
# ---------------------------------------------------------------------------

def should_take_action(alert: BudgetAlert) -> bool:
    """Decide whether the alert warrants action."""
    # Take action on actual spend at >= 100% or forecast at >= 90%
    if alert.is_actual and alert.threshold_percent >= 100:
        return True
    if alert.is_forecast and alert.threshold_percent >= 90:
        return True
    # Also act on any actual alert >= 50% if forecasted spend exceeds budget
    if alert.is_actual and alert.threshold_percent >= 50 and alert.forecasted_spend > alert.budget_amount:
        return True
    return False
