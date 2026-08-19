"""In-memory Pub/Sub message dedup cache for the kill switch service.

Split out of the original monolithic ``main.py`` — pure structural move,
no behavior change. Self-contained: no dependency on main's mutable
config flags, so it needs no back-reference to ``main``.
"""

from __future__ import annotations

import time

# ---------------------------------------------------------------------------
# Dedup cache — in-memory, survives only within a single container instance
# ---------------------------------------------------------------------------

_processed_messages: dict[str, float] = {}
_DEDUP_TTL_SECONDS = 600  # 10 min


def _is_duplicate(msg_id: str) -> bool:
    now = time.time()
    for k in [k for k, v in _processed_messages.items() if now - v >= _DEDUP_TTL_SECONDS]:
        del _processed_messages[k]
    if msg_id in _processed_messages:
        return True
    _processed_messages[msg_id] = now
    return False
