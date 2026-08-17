"""Durable on-disk spool for report_actual entries (issue #12).

Split out of the original monolithic ``__init__.py`` — pure structural
move, no behavior change.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from .errors import _fail, log

# ---------------------------------------------------------------------------
# Durable on-disk spool (issue #12)
# ---------------------------------------------------------------------------

_DEFAULT_SPOOL_DIR = os.path.expanduser("~/.cache/cloud_management_client/spool")


class _Spool:
    """Durable on-disk spool for report_actual entries.

    Each entry is a JSON file in ``spool_dir``. Entries are written before
    the HTTP attempt and deleted on confirmed delivery. On failure, the
    worker retries with exponential backoff and jitter, bounded by a max
    attempt count and a max spool age. On client startup, any entries left
    by a previous process are replayed.

    The spool is best-effort: all I/O errors are caught and logged at
    WARNING (or ERROR for drops). It never raises into the host application
    unless ``strict`` is True. Stdlib-only — no new dependencies.

    A ``client_seq`` (monotonic per intent) is stamped into each entry so
    the hub can reject stale replays that would overwrite a newer cumulative
    actual (see scenario 6 / issue #12).
    """

    def __init__(
        self,
        spool_dir: str,
        cap: int = 1000,
        max_attempts: int = 10,
        max_age_seconds: float = 86400.0,
        strict: bool = False,
    ) -> None:
        self.dir = spool_dir
        self.cap = cap
        self.max_attempts = max_attempts
        self.max_age_seconds = max_age_seconds
        self.strict = strict
        self._lock = threading.Lock()
        self._counter = 0
        self._enabled = bool(spool_dir)
        if self._enabled:
            self._ensure_dir()

    def _ensure_dir(self) -> None:
        """Create the spool directory if it doesn't exist. Best-effort."""
        try:
            os.makedirs(self.dir, exist_ok=True)
        except OSError as e:
            log.warning("cloud_management: spool dir unusable: %s", e)
            self._enabled = False

    def _next_id(self) -> str:
        """Generate a unique, sortable spool entry ID."""
        with self._lock:
            self._counter += 1
            return f"{time.time():.6f}_{os.getpid()}_{self._counter}"

    def write(self, path: str, payload: dict[str, Any], client_seq: int = 0) -> str | None:
        """Persist a report to the spool. Returns the entry ID, or None if
        the spool is disabled or unwritable. Enforces the cap by dropping
        oldest entries with an ERROR log."""
        if not self._enabled:
            return None
        entry_id = self._next_id()
        entry = {
            "id": entry_id,
            "path": path,
            "payload": payload,
            "client_seq": client_seq,
            "attempts": 0,
            "created_at": time.time(),
            "last_attempt_at": 0.0,
        }
        try:
            with self._lock:
                filepath = os.path.join(self.dir, f"{entry_id}.json")
                # Atomic write: write to temp then rename
                tmp = filepath + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(entry, f)
                os.rename(tmp, filepath)
                # Enforce cap after write so total never exceeds cap.
                self._enforce_cap()
            return entry_id
        except OSError as e:
            _fail("spool write failed", e, strict=self.strict)
            return None

    def _enforce_cap(self) -> None:
        """Drop oldest entries if the spool exceeds the cap. Caller holds _lock."""
        try:
            files = [f for f in os.listdir(self.dir) if f.endswith(".json")]
            if len(files) <= self.cap:
                return
            # Sort by filename (timestamp-prefixed → oldest first)
            files.sort()
            to_drop = len(files) - self.cap
            for f in files[:to_drop]:
                try:
                    os.remove(os.path.join(self.dir, f))
                    log.error("cloud_management: spool cap exceeded — dropped oldest entry %s", f)
                except OSError as e:
                    log.error("cloud_management: failed to drop spool entry %s: %s", f, e)
        except OSError as e:
            log.error("cloud_management: spool cap enforcement failed: %s", e)

    def read(self, entry_id: str) -> dict[str, Any] | None:
        """Read a spool entry. Returns None if not found or unreadable."""
        if not self._enabled:
            return None
        try:
            filepath = os.path.join(self.dir, f"{entry_id}.json")
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            _fail(f"spool read failed for {entry_id}", e, strict=self.strict)
            return None

    def remove(self, entry_id: str) -> None:
        """Delete a spool entry after confirmed delivery."""
        if not self._enabled:
            return
        try:
            os.remove(os.path.join(self.dir, f"{entry_id}.json"))
        except FileNotFoundError:
            pass  # already removed
        except OSError as e:
            _fail(f"spool remove failed for {entry_id}", e, strict=self.strict)

    def update_attempt(self, entry_id: str, attempts: int) -> None:
        """Update the attempt count and last_attempt_at for an entry."""
        if not self._enabled:
            return
        try:
            filepath = os.path.join(self.dir, f"{entry_id}.json")
            with open(filepath, "r", encoding="utf-8") as f:
                entry = json.load(f)
            entry["attempts"] = attempts
            entry["last_attempt_at"] = time.time()
            tmp = filepath + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(entry, f)
            os.rename(tmp, filepath)
        except (OSError, json.JSONDecodeError) as e:
            _fail(f"spool update failed for {entry_id}", e, strict=self.strict)

    def list_entries(self) -> list[str]:
        """Return all spool entry IDs, sorted oldest-first for replay."""
        if not self._enabled:
            return []
        try:
            files = [f[:-5] for f in os.listdir(self.dir) if f.endswith(".json")]
            files.sort()
            return files
        except OSError as e:
            _fail("spool list failed", e, strict=self.strict)
            return []

    def is_expired(self, entry: dict[str, Any]) -> bool:
        """Check if an entry has exceeded max_attempts or max_age."""
        if entry.get("attempts", 0) >= self.max_attempts:
            return True
        age = time.time() - entry.get("created_at", 0)
        if age > self.max_age_seconds:
            return True
        return False

    def drop(self, entry_id: str, reason: str) -> None:
        """Drop an entry with an ERROR log."""
        log.error("cloud_management: spool entry %s dropped: %s", entry_id, reason)
        self.remove(entry_id)
