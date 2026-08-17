"""Actual-usage reporting, key-exposure reporting, client-polled kill
orders, and the ``intent()`` context-manager factory for
CloudManagementClient.

One of several mixins that ``client.CloudManagementClient`` combines —
split out of the original monolithic class for readability. Pure
structural move: every method here behaves identically to before, just
relocated (the ``IntentContext`` import is local to ``intent()`` to
avoid a circular import with ``context.py``). See ``client.py`` for how
the mixins are assembled.
"""

from __future__ import annotations

import os
import time
import urllib.parse
from typing import TYPE_CHECKING, Any

from .errors import KillOrder, log
from .models import ActualResponse

if TYPE_CHECKING:
    from .context import IntentContext


class _ActualOpsMixin:
    """``report_exposure``, ``report_actual``, ``check_kill_orders``,
    and the ``intent()`` context-manager factory."""

    def report_exposure(
        self,
        display_name: str = "",
        all_keys: bool = True,
        dry_run: bool = False,
        enable_api: bool = False,
    ) -> dict[str, Any] | None:
        """Report that an API key has been exposed and request rotation.

        Returns the server response including new key string(s) on success.
        Errors are logged and returned as None (or raise in strict mode).
        """
        if not self.enabled:
            return None
        payload: dict[str, Any] = {
            "project_id": self.project_id,
            "display_name": display_name,
            "all": all_keys,
            "dry_run": dry_run,
            "enable_api": enable_api,
        }
        return self._post_sync("/api/v1/exposure", payload)

    def report_actual(
        self,
        intent_id: str,
        job_id: str = "",
        provider: str = "",
        api: str = "",
        actual_calls: int = 0,
        actual_cost_usd: float = 0.0,
        actual_tokens: int | None = None,
        status: str = "completed",  # "running" | "completed" | "failed"
        started_at: str = "",
        ended_at: str = "",
        application: str = "",
        sync: bool = False,
    ) -> ActualResponse:
        """Report actual API usage (post-call or incremental).

        By default, this is **asynchronous** — the HTTP request is
        enqueued to a background daemon thread so it never blocks the
        caller.  This is important for per-call reporting from hot
        paths (e.g. every Gemini API call).

        For the **final** report (status="completed" or "failed"), pass
        ``sync=True`` to get the response synchronously, and call
        ``flush()`` afterwards to ensure all prior async reports have
        been delivered.

        Returns an ActualResponse.  In async mode (default), the
        response is a placeholder — the actual HTTP happens in the
        background.
        """
        # Stamp a monotonic client_seq per intent so the hub can reject
        # stale replays that would overwrite a newer cumulative actual
        # (scenario 6 / issue #12).
        with self._seq_lock:
            seq = self._client_seq.get(intent_id, 0) + 1
            self._client_seq[intent_id] = seq

        payload: dict[str, Any] = {
            "project_id": self.project_id,
            "intent_id": intent_id,
            "job_id": job_id,
            "provider": provider,
            "api": api,
            "actual_calls": actual_calls,
            "actual_cost_usd": actual_cost_usd,
            "status": status,
            "application": application or self.application,
            "client_seq": seq,
        }
        if actual_tokens is not None:
            payload["actual_tokens"] = actual_tokens
        if started_at:
            payload["started_at"] = started_at
        if ended_at:
            payload["ended_at"] = ended_at

        if sync or not self.enabled:
            # Persist to spool before the HTTP attempt so a crash mid-send
            # doesn't lose the report. On success, remove from spool.
            # A disabled client can never deliver, so spooling would just
            # accumulate undeliverable entries until the cap evicts them.
            entry_id = (
                self._spool.write("/api/v1/actual", payload, client_seq=seq)
                if self.enabled
                else None
            )
            data = self._post_sync("/api/v1/actual", payload)
            if data is not None and entry_id is not None:
                self._spool.remove(entry_id)
            if data is None:
                return ActualResponse()
            return ActualResponse(
                actual_id=data.get("actual_id", ""),
                overrun_detected=data.get("overrun_detected", False),
                status=data.get("status", ""),
                overrun=data.get("overrun", {}),
                kill_result=data.get("kill_result", {}),
                raw=data,
            )
        else:
            # Async — persist to spool, then enqueue the entry ID to the
            # background worker. The worker retries with backoff on failure.
            entry_id = self._spool.write("/api/v1/actual", payload, client_seq=seq)
            self._ensure_worker()
            if entry_id is not None:
                self._queue.put(entry_id)
            else:
                # Spool disabled — fall back to direct enqueue (best-effort,
                # no retry, matches pre-#12 behaviour).
                self._queue.put(self._inline_entry("/api/v1/actual", payload))
            return ActualResponse()  # placeholder — actual HTTP happens in background

    def _inline_entry(self, path: str, payload: dict[str, Any]) -> str:
        """When the spool is disabled, create a transient in-memory entry
        that the worker processes without persistence or retry."""
        entry_id = f"inline_{time.time():.6f}_{os.getpid()}"
        self._inline_entries[entry_id] = {"path": path, "payload": payload}
        return entry_id

    # ------------------------------------------------------------------
    # Client-polled kill orders (issue #13)
    # ------------------------------------------------------------------

    def check_kill_orders(self, since: str = "") -> list[KillOrder]:
        """Poll the hub for kill orders targeting this project.

        Returns a list of ``KillOrder`` objects. An empty list means no
        kill orders (the job should continue). This is the inverted kill
        channel — instead of the hub pushing to a callback URL, the
        client polls. Use this between reports for long-running jobs.

        ``since`` is an ISO 8601 timestamp; only orders at or after this
        time are returned. Pass the timestamp of the last order seen to
        avoid re-processing.
        """
        if not self.enabled:
            return []
        params = urllib.parse.urlencode(
            {"project_id": self.project_id, **({"since": since} if since else {})}
        )
        data = self._get_sync(f"/api/v1/kill-orders?{params}")
        if data is None:
            return []
        orders = []
        for raw_order in data.get("kill_orders", []):
            try:
                orders.append(KillOrder(
                    kill_id=raw_order.get("kill_id", ""),
                    intent_id=raw_order.get("intent_id", ""),
                    project_id=raw_order.get("project_id", ""),
                    job_id=raw_order.get("job_id", ""),
                    reason=raw_order.get("reason", ""),
                    rule=raw_order.get("rule", ""),
                    kill_type=raw_order.get("kill_type", ""),
                    killed=bool(raw_order.get("killed", False)),
                    detail=raw_order.get("detail", ""),
                    error=raw_order.get("error", ""),
                    timestamp=raw_order.get("timestamp", ""),
                    raw=raw_order,
                ))
            except Exception as e:
                log.warning("cloud_management: malformed kill order ignored: %s", e)
        return orders

    # ------------------------------------------------------------------
    # Context manager for automatic intent/actual lifecycle
    # ------------------------------------------------------------------

    def intent(
        self,
        job_id: str,
        **kwargs: Any,
    ) -> "IntentContext":
        """Context manager that declares an intent and reports the
        final actual on exit.

        Usage:
            with cb.intent(job_id="x", provider="google",
                           expected_calls=100, expected_cost_usd=1.0) as ctx:
                for q in queries:
                    call_api(q)
                    ctx.add_calls(1, cost_usd=0.01)
                # on normal exit: reports "completed" (sync=True)
                # on exception: reports "failed" (sync=True)
        """
        from .context import IntentContext

        return IntentContext(self, job_id, **kwargs)
