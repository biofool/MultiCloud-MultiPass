"""Shared helpers for the intent storage modules: Firestore client
accessor, id/timestamp generation, and the atomic YAML writer.

Split out of ``intent_storage.py`` during the ≤250-line refactor — pure
structural move, no behavior change. Same ``_intent_mod`` qualified
config access as the other intent_* leaf modules (see intent_storage.py's
docstring for why it's not a bare ``import intent``).

Maintenance-mode snapshot (upstream convergence): the canonical
implementation of this module is maintained upstream in
``biofool/CloudManagement``. This copy is a frozen snapshot kept so
this repo stays standalone-runnable — it receives security fixes and
upstream backports only. See docs/convergence.md.
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone

import intent as _intent_mod


def _get_firestore_client():
    if _intent_mod._fs_client is None:
        from google.cloud import firestore
        _intent_mod._fs_client = firestore.Client(project=_intent_mod.FIRESTORE_PROJECT or None)
    return _intent_mod._fs_client


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gen_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def _yaml_save(path: str, key: str, items: list[dict]) -> None:
    import yaml
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump({key: items}, f, sort_keys=False)
    os.replace(tmp, path)  # atomic: a crash mid-write cannot truncate (#198)
