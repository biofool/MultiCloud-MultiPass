"""Overrun and budget-exceeded detection for the intent/actual protocol.

Split out of the original monolithic ``intent.py`` — pure structural
move, no behavior change. ``check_intent_overrun``'s parameter is named
``intent`` (an ``Intent`` instance), which would shadow a plain
``import intent`` — hence the ``_intent_mod`` alias for the one
reload-sensitive constant it reads (``INTENT_VARIANCE_THRESHOLD``).

Maintenance-mode snapshot (upstream convergence): the canonical
implementation of this module is maintained upstream in
``biofool/CloudManagement``. This copy is a frozen snapshot kept so
this repo stays standalone-runnable — it receives security fixes and
upstream backports only. See docs/convergence.md.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import intent as _intent_mod
import registry
from intent_models import Intent, Actual
from intent_storage import list_actuals, sum_actuals_for_intent

# ---------------------------------------------------------------------------
# Detection — evaluate intent vs actual for overrun
# ---------------------------------------------------------------------------


def project_month_spend(project_id: str) -> float:
    """Month-to-date spend for a project in USD.

    Single definition of project spend (issue #5): incremental reports
    are *cumulative* per intent, so summing every Actual row double-counts.
    Keep only the latest report per intent — ordered by (client_seq,
    sequence), the same tie-break the hub uses everywhere — then sum.
    """
    current_month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")
    by_intent: dict[str, Actual] = {}
    for a in list_actuals(project_id=project_id):
        if not (a.created_at and a.created_at.startswith(current_month_prefix)):
            continue
        existing = by_intent.get(a.intent_id)
        if existing is None or (a.client_seq, a.sequence) > (existing.client_seq, existing.sequence):
            by_intent[a.intent_id] = a
    return sum(a.actual_cost_usd for a in by_intent.values())


def check_intent_overrun(intent: Intent) -> dict[str, Any] | None:
    """Check if an intent's actuals have exceeded the variance threshold.

    Returns a dict describing the overrun if detected, otherwise None.
    """
    summed = sum_actuals_for_intent(intent.intent_id)

    # Rule 1: actual_exceeds_intent (calls)
    if intent.expected_calls > 0:
        call_ratio = summed["actual_calls"] / intent.expected_calls
        if call_ratio > _intent_mod.INTENT_VARIANCE_THRESHOLD:
            return {
                "rule": "actual_exceeds_intent_calls",
                "intent_id": intent.intent_id,
                "project_id": intent.project_id,
                "job_id": intent.job_id,
                "expected": intent.expected_calls,
                "actual": summed["actual_calls"],
                "ratio": round(call_ratio, 2),
                "threshold": _intent_mod.INTENT_VARIANCE_THRESHOLD,
            }

    # Rule 1b: actual_exceeds_intent (cost)
    if intent.expected_cost_usd > 0:
        cost_ratio = summed["actual_cost_usd"] / intent.expected_cost_usd
        if cost_ratio > _intent_mod.INTENT_VARIANCE_THRESHOLD:
            return {
                "rule": "actual_exceeds_intent_cost",
                "intent_id": intent.intent_id,
                "project_id": intent.project_id,
                "job_id": intent.job_id,
                "expected_cost": intent.expected_cost_usd,
                "actual_cost": round(summed["actual_cost_usd"], 4),
                "ratio": round(cost_ratio, 2),
                "threshold": _intent_mod.INTENT_VARIANCE_THRESHOLD,
            }

    return None


def check_project_budget(project_id: str) -> dict[str, Any] | None:
    """Check if a project's rolling spend has exceeded its budget.

    Sums the latest actual report per intent for the current calendar
    month via ``project_month_spend`` — each intent's latest report is a
    cumulative total, so we take the latest per intent (not the sum of
    all reports) and sum across intents.
    """
    acct = registry.get_account(project_id)
    if not acct or acct.budget_amount_usd <= 0:
        return None

    total_spend = project_month_spend(project_id)

    if total_spend > acct.budget_amount_usd:
        return {
            "rule": "project_budget_exceeded",
            "project_id": project_id,
            "budget": acct.budget_amount_usd,
            "spend": round(total_spend, 4),
        }
    return None
