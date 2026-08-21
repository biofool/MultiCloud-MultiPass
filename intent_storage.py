"""Firestore/YAML persistence for intents, actuals, expected costs, and
kill events.

Split out of the original monolithic ``intent.py`` — pure structural
move, no behavior change. See the inline comment below on why config
access is qualified through ``_intent_mod`` rather than a bare
``import intent``. ``_fs_client`` (the lazily-created Firestore client
cache) stays defined in intent.py itself rather than moving here, so
``importlib.reload(intent)`` keeps resetting it exactly as before.
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone
from typing import Any

import intent as _intent_mod
from intent_models import Intent, Actual, ExpectedCost

# ---------------------------------------------------------------------------
# Store backend — Firestore or YAML
# ---------------------------------------------------------------------------


def _get_firestore_client():
    if _intent_mod._fs_client is None:
        from google.cloud import firestore
        _intent_mod._fs_client = firestore.Client(project=_intent_mod.FIRESTORE_PROJECT or None)
    return _intent_mod._fs_client


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gen_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


# --- Intent store ---

def save_intent(intent: Intent) -> None:
    if _intent_mod.USE_FIRESTORE:
        client = _get_firestore_client()
        client.collection(_intent_mod.INTENTS_COLLECTION).document(intent.intent_id).set(intent.to_dict())
    else:
        _yaml_save(_intent_mod._INTENTS_FILE, "intents", [i.to_dict() for i in _yaml_load_intents() if i.intent_id != intent.intent_id] + [intent.to_dict()])


def get_intent(intent_id: str) -> Intent | None:
    for intent in list_intents():
        if intent.intent_id == intent_id:
            return intent
    return None


def list_intents(project_id: str | None = None, status: str | None = None) -> list[Intent]:
    if _intent_mod.USE_FIRESTORE:
        client = _get_firestore_client()
        # Query by only one field to avoid composite index requirements.
        # Filter the secondary field in memory.
        q = client.collection(_intent_mod.INTENTS_COLLECTION)
        if project_id:
            q = q.where("project_id", "==", project_id)
        intents = [Intent.from_dict(doc.to_dict() or {}) for doc in q.stream()]
        if status:
            intents = [i for i in intents if i.status == status]
        return intents
    else:
        intents = _yaml_load_intents()
        if project_id:
            intents = [i for i in intents if i.project_id == project_id]
        if status:
            intents = [i for i in intents if i.status == status]
        return intents


def _yaml_load_intents() -> list[Intent]:
    import yaml
    if not os.path.exists(_intent_mod._INTENTS_FILE):
        return []
    with open(_intent_mod._INTENTS_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return [Intent.from_dict(d) for d in data.get("intents", [])]


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


def _yaml_save(path: str, key: str, items: list[dict]) -> None:
    import yaml
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump({key: items}, f, sort_keys=False)


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
