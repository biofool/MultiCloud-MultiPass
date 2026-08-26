"""CloudManagementClient construction and background-worker lifecycle.

One of several mixins that ``client.CloudManagementClient`` combines —
split out of the original monolithic class for readability. Pure
structural move: every method here behaves identically to before, just
relocated. See ``client.py`` for how the mixins are assembled.
"""

from __future__ import annotations

import os
import queue
import random
import threading
import time
from typing import Any

from .errors import _fail, log
from .spool import _DEFAULT_SPOOL_DIR, _Spool
from ._transport import _is_permanent_error


class _LifecycleMixin:
    """__init__, ``enabled``, and the async report_actual worker thread.

    (The user-facing class docstring lives on ``CloudManagementClient``
    itself in ``client.py`` — this mixin is an internal implementation
    detail.)
    """

    def __init__(
        self,
        project_id: str = "",
        report_token: str = "",
        base_url: str = "",
        source_repo: str = "",
        application: str = "",
        timeout: int | None = None,
        intent_timeout: int | None = None,
        strict: bool | None = None,
        spool_dir: str | None = None,
        use_identity: bool | None = None,
        gate_token: str = "",
    ) -> None:
        self.project_id = project_id or os.environ.get("CLOUDMANAGEMENT_PROJECT_ID", "")
        self.report_token = report_token or os.environ.get("CLOUDMANAGEMENT_REPORT_TOKEN", "")
        self.base_url = (base_url or os.environ.get("CLOUDMANAGEMENT_URL", "http://127.0.0.1:8080")).rstrip("/")
        self.gate_token = gate_token or os.environ.get("CLOUDMANAGEMENT_GATE_TOKEN", "")
        self.source_repo = source_repo
        # Human-readable name of the calling application (e.g. "OSenseiArchiver"),
        # distinct from source_repo (the GitHub repo, e.g. "biofool/OSenseiDocuments").
        # Recorded on every intent/actual report for attribution in the dashboard.
        self.application = application or os.environ.get("CLOUDMANAGEMENT_APPLICATION", "")
        self.timeout = timeout if timeout is not None else int(os.environ.get("CLOUDMANAGEMENT_TIMEOUT", "5"))
        self.intent_timeout = intent_timeout if intent_timeout is not None else int(os.environ.get("CLOUDMANAGEMENT_INTENT_TIMEOUT", "3"))
        self.strict = strict if strict is not None else os.environ.get("CLOUDMANAGEMENT_STRICT", "false").lower() == "true"

        # Durable on-disk spool for report_actual (issue #12). Set
        # CLOUDMANAGEMENT_SPOOL_DIR="" to disable (read-only filesystems).
        if spool_dir is not None:
            _spool_dir = spool_dir
        else:
            _spool_dir = os.environ.get("CLOUDMANAGEMENT_SPOOL_DIR", _DEFAULT_SPOOL_DIR)
        self._spool = _Spool(
            spool_dir=_spool_dir,
            cap=int(os.environ.get("CLOUDMANAGEMENT_SPOOL_CAP", "1000")),
            max_attempts=int(os.environ.get("CLOUDMANAGEMENT_SPOOL_MAX_ATTEMPTS", "10")),
            max_age_seconds=float(os.environ.get("CLOUDMANAGEMENT_SPOOL_MAX_AGE_SECONDS", "86400")),
            strict=self.strict,
        )

        # Identity-token mode (issue #10): when True, the client fetches a GCP
        # OIDC ID token from the metadata server scoped to base_url and sends
        # it as the bearer credential instead of a shared report_token. This
        # eliminates the need to create/distribute/rotate a shared secret for
        # GCP-resident clients. Falls back to report_token if the metadata
        # server is unreachable (local dev, OpenStack).
        if use_identity is not None:
            self.use_identity = use_identity
        else:
            self.use_identity = os.environ.get("CLOUDMANAGEMENT_USE_IDENTITY", "false").lower() == "true"
        self._id_token: str | None = None
        self._id_token_expiry: float = 0.0

        # Per-intent monotonic client_seq — stamped into each report so the
        # hub can reject stale replays that would overwrite a newer cumulative
        # actual (scenario 6 / issue #12).
        self._client_seq: dict[str, int] = {}
        self._seq_lock = threading.Lock()
        # Seed the high-water mark from any entries left on disk by a previous
        # process. Without this a restart restarts numbering at 1 for an
        # intent that already has higher-numbered reports spooled, and the
        # hub's "latest sequence wins" rule discards the newer report.
        self._seed_client_seq_from_spool()

        # Background queue for async report_actual calls. Items are spool
        # entry IDs (or None as the stop sentinel). Retry entries carry
        # a due-time so the worker doesn't sleep inline (issue #1 part 3).
        # Two worker threads share this queue so a retry backoff on one
        # entry does not head-of-line-block delivery of others (issue #1).
        self._queue: queue.Queue[Any] = queue.Queue()
        self._workers: list[threading.Thread] = []
        self._worker_lock = threading.Lock()
        # Transient in-memory entries for when the spool is disabled.
        self._inline_entries: dict[str, dict[str, Any]] = {}
        # Last transport exception — set by _post_sync, read by the spool
        # worker to distinguish permanent (4xx) from transient failures.
        self._last_exc: Exception | None = None

        if not self.project_id:
            log.warning("cloud_management: project_id not set — client disabled")
        if not self.report_token and not self.use_identity:
            log.warning("cloud_management: report_token not set and use_identity=False — client disabled")

    @property
    def enabled(self) -> bool:
        # In identity mode, the token is fetched at request time from the
        # metadata server, so report_token is not required.
        return bool(self.project_id and (self.report_token or self.use_identity))

    # ------------------------------------------------------------------
    # Background worker for async report_actual
    # ------------------------------------------------------------------

    def _seed_client_seq_from_spool(self) -> None:
        """Initialise per-intent client_seq from un-delivered spool entries.

        Spooled reports carry the client_seq assigned by the process that
        wrote them. A fresh process must continue *above* that high-water
        mark, otherwise its genuinely-newer cumulative actuals look stale to
        the hub and get dropped (silent under-reporting of spend).

        Seeding is strictly best-effort. It runs in ``__init__``, so it must
        never prevent the client from being constructed: in strict mode
        ``_Spool.list_entries``/``read`` raise on an unreadable or corrupt
        leftover file, and a stale file in the cache directory is not a
        reason to take the calling application down at startup. Failures are
        logged and skipped; the worst case is that one entry does not raise
        the high-water mark.
        """
        try:
            entry_ids = self._spool.list_entries()
        except Exception as e:
            log.warning(
                "cloud_management: could not list the spool to seed client_seq, "
                "continuing without a high-water mark: %s", e
            )
            return
        for entry_id in entry_ids:
            try:
                entry = self._spool.read(entry_id)
                if not entry:
                    continue
                intent_id = (entry.get("payload") or {}).get("intent_id", "")
                if not intent_id:
                    continue
                seq = int(entry.get("client_seq", 0) or 0)
            except Exception as e:
                log.warning(
                    "cloud_management: skipping unreadable spool entry %s while "
                    "seeding client_seq: %s", entry_id, e
                )
                continue
            if seq > self._client_seq.get(intent_id, 0):
                self._client_seq[intent_id] = seq

    def _ensure_worker(self) -> None:
        """Start the background worker threads if not already running.

        Two worker threads share the queue so a retry backoff on one
        entry does not head-of-line-block delivery of others (issue #1).

        Also replays any spool entries left by a previous process — this is
        the headline scenario for issue #12 (test_spool_survives_process_restart).
        """
        if self._workers and all(w.is_alive() for w in self._workers):
            return
        with self._worker_lock:
            if self._workers and all(w.is_alive() for w in self._workers):
                return
            # Replay spool entries from a previous process before starting
            # the workers, so they are processed in order.
            for entry_id in self._spool.list_entries():
                self._queue.put(entry_id)
            self._workers = []
            for i in range(2):
                t = threading.Thread(
                    target=self._worker_loop,
                    name=f"cloud-management-reporter-{i}",
                    daemon=True,
                )
                t.start()
                self._workers.append(t)

    def _sleep(self, seconds: float) -> None:
        """Indirection so tests can mock sleep without actually waiting."""
        time.sleep(seconds)

    def _worker_loop(self) -> None:
        """Process queued report_actual requests with spool-backed retry.

        Two worker threads run this loop concurrently, sharing a single
        queue (issue #1). When one worker pulls a not-yet-due retry entry
        and re-enqueues it with a brief sleep, the other worker keeps
        draining ready entries — so a backoff on one entry no longer
        head-of-line-blocks delivery of the rest.

        Issue #1 part 3: retry entries carry a due-time so the worker
        does not sleep inline. Instead of blocking a worker thread on a
        backoff timer (which head-of-line-blocks every subsequent
        report), failed entries are re-enqueued with a due-time and the
        worker immediately picks up the next ready entry. The queue
        holds either plain entry IDs (str) or ``(entry_id, due_time)``
        tuples for retries.
        """
        while True:
            item = self._queue.get()
            if item is None:
                # Sentinel — signal to stop
                self._queue.task_done()
                break
            # Unpack: plain entry_id (str) or (entry_id, due_time) tuple
            if isinstance(item, tuple):
                entry_id, due_time = item
                # Wait until due-time, but yield to let other entries
                # be processed. We re-queue ourselves at the front if
                # not yet due, after a short sleep.
                now = time.monotonic()
                if now < due_time:
                    # Not yet due — put it back and sleep briefly. Since
                    # this is the only worker, we sleep for the remaining
                    # time (capped) rather than busy-waiting. This is a
                    # compromise: the single worker still waits, but
                    # other entries that were already queued behind this
                    # one get processed first because we re-queue at the
                    # back, not the front. The key improvement over the
                    # old code is that a permanent failure (4xx) is now
                    # dropped immediately instead of retrying 10 times.
                    remaining = min(due_time - now, 1.0)
                    self._queue.put((entry_id, due_time))
                    self._queue.task_done()
                    self._sleep(remaining)
                    continue
            else:
                entry_id = item
            try:
                self._process_spool_entry(entry_id)
            except Exception as e:
                _fail("async report_actual failed", e, strict=self.strict)
            finally:
                self._queue.task_done()

    def _process_spool_entry(self, entry_id: str) -> None:
        """Attempt to deliver a spool entry once, re-enqueuing on transient failure.

        Issue #1 parts 3-4: instead of retrying inline with exponential
        backoff (which head-of-line-blocks the worker for up to ~5 min
        per failing entry), this makes a single attempt and re-enqueues
        with a due-time if the failure is transient. Permanent failures
        (HTTP 4xx except 408/429) are dropped immediately — retrying a
        400 Bad Request or 401 Unauthorized 10 times over 24h is pure waste.
        """
        # Inline (spool disabled) — single attempt, no retry.
        if entry_id.startswith("inline_"):
            entry = self._inline_entries.pop(entry_id, None)
            if entry is None:
                return
            self._post_sync(entry["path"], entry["payload"])
            return

        entry = self._spool.read(entry_id)
        if entry is None:
            return  # entry was lost or corrupted — nothing to deliver
        attempts = entry.get("attempts", 0)

        # Single attempt — no inline retry loop (issue #1 part 3).
        data, exc = self._post_sync_with_error(entry["path"], entry["payload"])
        self._last_exc = exc
        if data is not None:
            # Confirmed delivery — remove from spool.
            self._spool.remove(entry_id)
            return

        # Check if this is a permanent failure (issue #1 part 4).
        if exc is not None and _is_permanent_error(exc):
            self._spool.drop(entry_id, f"permanent failure (HTTP {getattr(exc, 'code', '?')}): {exc}")
            return

        attempts += 1
        self._spool.update_attempt(entry_id, attempts)
        if self._spool.is_expired({**entry, "attempts": attempts}):
            self._spool.drop(entry_id, f"max attempts ({attempts}) or max age exceeded")
            return
        # Re-enqueue with a due-time instead of sleeping inline.
        # Exponential backoff with jitter: base * 2^(attempts-1) + random
        backoff = min(1.0 * (2 ** (attempts - 1)) + random.uniform(0, 1), 60.0)
        due_time = time.monotonic() + backoff
        self._queue.put((entry_id, due_time))

    def flush(self, timeout: float = 5.0) -> None:
        """Wait for all pending async report_actual calls to complete.

        Blocks until the queue is drained or ``timeout`` seconds elapse.
        If the timeout expires, pending reports are NOT lost — they remain
        in the on-disk spool and are replayed on the next client startup
        (issue #12). The daemon worker thread will also continue processing
        them if the process stays alive.

        Note: entries that are waiting for a retry due-time will not be
        drained within the timeout — they are re-enqueued with backoff
        (issue #1 part 3). This is expected: a report that is failing
        because the hub is unreachable should not block shutdown.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # all_tasks_done is an internal Condition; wait briefly
            if self._queue.all_tasks_done.acquire(timeout=0.1):
                try:
                    if self._queue.unfinished_tasks == 0:
                        return
                finally:
                    self._queue.all_tasks_done.release()
            else:
                continue
        # Timeout expired — pending items remain in the spool for replay

    def close(self) -> None:
        """Signal the background worker threads to stop and wait briefly.

        Issue #1 part 3: the sentinel is put at the front of the queue
        (via a new PriorityQueue-style approach) so shutdown is not
        blocked behind pending retry entries. Pending entries remain in
        the on-disk spool for replay on next startup.

        Two workers share the queue, so the None sentinel is enqueued
        once per worker so both threads shut down cleanly (issue #1).
        """
        # Drain the queue of retry entries, then put the sentinel.
        # This ensures shutdown is not blocked behind retry backoffs.
        # Pending entries are safe in the on-disk spool.
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                break
        # One sentinel per worker thread so both shut down.
        for _ in self._workers:
            self._queue.put(None)
        for w in self._workers:
            w.join(timeout=2.0)
        self._workers = []
