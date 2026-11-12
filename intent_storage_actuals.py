"""Firestore/YAML persistence for actuals, expected costs, and kill
events.

Split out of ``intent_storage.py`` during the ≤250-line refactor — pure
structural move, no behavior change. Shared helpers come from
``intent_storage_common`` (no import back into ``intent_storage`` — the
dependency is one-directional); ``intent_storage`` re-exports every name
here so all existing call sites keep working.

Maintenance-mode snapshot (upstream convergence): the canonical
implementation of this module is maintained upstream in
``biofool/CloudManagement``. This copy is a frozen snapshot kept so
this repo stays standalone-runnable — it receives security fixes and
upstream backports only. See docs/convergence.md.
"""

from __future__ import annotations

import os
from typing import Any

import intent as _intent_mod
from intent_models import Actual, ExpectedCost
from intent_storage_common import (
    _gen_id,
    _get_firestore_client,
    _now_iso,
    _yaml_save,
)


def _yaml_load_actuals() -> list[Actual]:
    import yaml
    if not os.path.exists(_intent_mod._ACTUALS_FILE):
        return []
    with open(_intent_mod._ACTUALS_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return [Actual.from_dict(d) for d in data.get("actuals", [])]


def _yaml_load_expected_costs() -> list[ExpectedCost]:
    import yaml
    if not os.path.exists(_intent_mod._EXPECTED_COSTS_FILE):
        return []
    with open(_intent_mod._EXPECTED_COSTS_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return [ExpectedCost.from_dict(d) for d in data.get("expected_costs", [])]


# --- Actual store ---

def save_actual(actual: Actual) -> None:
    if _intent_mod.USE_FIRESTORE:
        client = _get_firestore_client()
        client.collection(_intent_mod.ACTUALS_COLLECTION).document(actual.actual_id).set(actual.to_dict())
    else:
        existing = [a.to_dict() for a in _yaml_load_actuals() if a.actual_id != actual.actual_id]
        _yaml_save(_intent_mod._ACTUALS_FILE, "actuals", existing + [actual.to_dict()])


def list_actuals(intent_id: str | None = None, project_id: str | None = None) -> list[Actual]:
    if _intent_mod.USE_FIRESTORE:
        client = _get_firestore_client()
        # Query by a single field to avoid composite index requirements.
        # If both filters are needed, query by the more selective one and
        # filter the rest in memory.
        q = client.collection(_intent_mod.ACTUALS_COLLECTION)
        if intent_id:
            q = q.where("intent_id", "==", intent_id)
        elif project_id:
            q = q.where("project_id", "==", project_id)
        actuals = [Actual.from_dict(doc.to_dict() or {}) for doc in q.stream()]
        # Filter in memory for the secondary field
        if project_id and intent_id:
            actuals = [a for a in actuals if a.project_id == project_id]
        # Sort by sequence to ensure latest is last
        actuals.sort(key=lambda a: a.sequence)
        return actuals
    else:
        actuals = _yaml_load_actuals()
        if intent_id:
            actuals = [a for a in actuals if a.intent_id == intent_id]
        if project_id:
            actuals = [a for a in actuals if a.project_id == project_id]
        actuals.sort(key=lambda a: a.sequence)
        return actuals


def sum_actuals_for_intent(intent_id: str) -> dict[str, Any]:
    """Return the latest actual report for an intent.

    Clients send cumulative totals (running total of calls/cost), not
    deltas.  Summing all reports would double-count.  The latest report
    by sequence number is the authoritative cumulative value.

    ``client_seq`` (issue #1 part 2): the client stamps a monotonic
    per-intent ``client_seq`` on each report so stale replays that would
    overwrite a newer cumulative actual are rejected. The hub enforces
    this by picking the report with the highest ``client_seq`` (falling
    back to ``sequence`` when ``client_seq`` is absent or tied, for
    backward compatibility with older clients that don't stamp it).
    """
    actuals = list_actuals(intent_id=intent_id)
    if not actuals:
        return {
            "actual_calls": 0,
            "actual_cost_usd": 0.0,
            "actual_tokens": None,
            "status": "declared",
            "report_count": 0,
        }
    # Pick the latest by client_seq (issue #1 part 2), falling back to
    # sequence for backward compat with older clients. list_actuals
    # already sorts by sequence, so ties break correctly.
    latest = max(actuals, key=lambda a: (a.client_seq, a.sequence))
    return {
        "actual_calls": latest.actual_calls,
        "actual_cost_usd": latest.actual_cost_usd,
        "actual_tokens": latest.actual_tokens,
        "status": latest.status,
        "report_count": len(actuals),
    }


# --- Expected cost store ---

def save_expected_cost(ec: ExpectedCost) -> None:
    ec.updated_at = _now_iso()
    if _intent_mod.USE_FIRESTORE:
        client = _get_firestore_client()
        doc_id = f"{ec.project_id}__{ec.provider}"
        client.collection(_intent_mod.EXPECTED_COSTS_COLLECTION).document(doc_id).set(ec.to_dict())
    else:
        existing = [e.to_dict() for e in _yaml_load_expected_costs() if not (e.project_id == ec.project_id and e.provider == ec.provider)]
        _yaml_save(_intent_mod._EXPECTED_COSTS_FILE, "expected_costs", existing + [ec.to_dict()])


def list_expected_costs(project_id: str | None = None) -> list[ExpectedCost]:
    if _intent_mod.USE_FIRESTORE:
        client = _get_firestore_client()
        q = client.collection(_intent_mod.EXPECTED_COSTS_COLLECTION)
        if project_id:
            q = q.where("project_id", "==", project_id)
        return [ExpectedCost.from_dict(doc.to_dict() or {}) for doc in q.stream()]
    else:
        costs = _yaml_load_expected_costs()
        if project_id:
            costs = [c for c in costs if c.project_id == project_id]
        return costs


# --- Kill event store ---

def save_kill_event(event: dict[str, Any]) -> None:
    if _intent_mod.USE_FIRESTORE:
        client = _get_firestore_client()
        client.collection(_intent_mod.KILL_EVENTS_COLLECTION).document(event.get("kill_id", _gen_id("kill"))).set(event)
    else:
        import yaml
        events = []
        if os.path.exists(_intent_mod._KILL_EVENTS_FILE):
            with open(_intent_mod._KILL_EVENTS_FILE, "r", encoding="utf-8") as f:
                events = (yaml.safe_load(f) or {}).get("kill_events", [])
        events.append(event)
        _yaml_save(_intent_mod._KILL_EVENTS_FILE, "kill_events", events)


def list_kill_events(project_id: str | None = None, limit: int = 50) -> list[dict]:
    if _intent_mod.USE_FIRESTORE:
        client = _get_firestore_client()
        # Fetch without order_by+where (avoids composite index requirement),
        # then filter and sort in memory.
        q = client.collection(_intent_mod.KILL_EVENTS_COLLECTION).order_by("timestamp", direction="DESCENDING").limit(limit * 5)
        events = [doc.to_dict() or {} for doc in q.stream()]
        if project_id:
            events = [e for e in events if e.get("project_id") == project_id]
        return events[:limit]
    else:
        import yaml
        if not os.path.exists(_intent_mod._KILL_EVENTS_FILE):
            return []
        with open(_intent_mod._KILL_EVENTS_FILE, "r", encoding="utf-8") as f:
            events = (yaml.safe_load(f) or {}).get("kill_events", [])
        if project_id:
            events = [e for e in events if e.get("project_id") == project_id]
        return events[-limit:]
