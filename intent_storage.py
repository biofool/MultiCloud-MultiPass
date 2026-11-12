"""Firestore/YAML persistence for intents, actuals, expected costs, and
kill events.

Split out of the original monolithic ``intent.py`` — pure structural
move, no behavior change. See the inline comment below on why config
access is qualified through ``_intent_mod`` rather than a bare
``import intent``. ``_fs_client`` (the lazily-created Firestore client
cache) stays defined in intent.py itself rather than moving here, so
``importlib.reload(intent)`` keeps resetting it exactly as before.

Further split during the ≤250-line refactor: the shared helpers
(``_get_firestore_client``, ``_now_iso``, ``_gen_id``, ``_yaml_save``)
moved to ``intent_storage_common.py`` and the actual/expected-cost/
kill-event stores moved to ``intent_storage_actuals.py``. Both are
re-exported below so every existing ``intent_storage.X`` call site keeps
working unchanged.

Maintenance-mode snapshot (upstream convergence): the canonical
implementation of this module is maintained upstream in
``biofool/CloudManagement``. This copy is a frozen snapshot kept so
this repo stays standalone-runnable — it receives security fixes and
upstream backports only. See docs/convergence.md.
"""

from __future__ import annotations

import os

import intent as _intent_mod
from intent_models import Intent
from intent_storage_common import (  # noqa: F401  (re-exported)
    _gen_id,
    _get_firestore_client,
    _now_iso,
    _yaml_save,
)
from intent_storage_actuals import (  # noqa: F401  (re-exported)
    _yaml_load_actuals,
    _yaml_load_expected_costs,
    list_actuals,
    list_expected_costs,
    list_kill_events,
    save_actual,
    save_expected_cost,
    save_kill_event,
    sum_actuals_for_intent,
)

# ---------------------------------------------------------------------------
# Store backend — Firestore or YAML
# ---------------------------------------------------------------------------


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


def save_declared_intent(intent: Intent) -> str | None:
    """save_intent() plus a cross-project ownership check on the id.

    Used by ``declare_intent`` for caller-chosen ids (CISO-117): on
    Firestore the check and the write run in one transaction, so two
    concurrent declares for the same id cannot both pass — the loser gets
    the conflicting project_id back instead of silently winning
    last-write (issue #16). The YAML backend is a single-process file, so
    the check there stays a plain read-then-write. Same-project overwrite
    stays allowed. Returns the project_id already holding the id, or None
    on success.
    """
    if _intent_mod.USE_FIRESTORE:
        from google.cloud import firestore as _firestore
        client = _get_firestore_client()
        doc_ref = client.collection(_intent_mod.INTENTS_COLLECTION).document(intent.intent_id)

        @_firestore.transactional
        def _declare(txn):
            snap = doc_ref.get(transaction=txn)
            owner = (snap.to_dict() or {}).get("project_id") if snap.exists else None
            if snap.exists and owner != intent.project_id:
                return owner
            txn.set(doc_ref, intent.to_dict())
            return None

        return _declare(client.transaction())
    existing = get_intent(intent.intent_id)
    if existing is not None and existing.project_id != intent.project_id:
        return existing.project_id
    save_intent(intent)
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
