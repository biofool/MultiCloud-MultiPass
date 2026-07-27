"""CloudManagement intent/actual reporting client.

A lightweight, stdlib-only client that sub-projects use to declare
expected API usage before making calls and report actuals after (or
incrementally during long jobs).  CloudManagement validates actual vs
intent, detects overruns, and can kill the specific job that is
accumulating cost.

Typical usage:

    from cloud_management_client import CloudManagementClient

    cb = CloudManagementClient(
        project_id="your-project-1",
        report_token=os.environ["CLOUDMANAGEMENT_REPORT_TOKEN_AIRICHARDMOON"],
        # base_url defaults to http://127.0.0.1:8080 for local dev;
        # set to the Cloud Run URL in production.
    )

    # Declare intent before a batch of API calls (synchronous — needs
    # the response to check approval)
    intent = cb.declare_intent(
        job_id="gemini-session-abc123",
        job_name="coaching-chat-batch",
        provider="google",
        api="gemini-3.1-flash-lite",
        expected_calls=500,
        expected_cost_usd=2.50,
        rate_limit_rpm=10,
        source_repo="AIRichardMoon",
    )
    if not intent.approved:
        raise RuntimeError(f"intent denied: {intent.reason}")

    # ... make API calls ...

    # Report actual (incremental — fire-and-forget via background thread
    # so it never blocks the caller)
    cb.report_actual(
        intent_id=intent.intent_id,
        job_id="gemini-session-abc123",
        provider="google",
        api="gemini-3.1-flash-lite",
        actual_calls=150,
        actual_cost_usd=0.75,
        status="running",   # "running" | "completed" | "failed"
    )

    # Final report when done (synchronous to ensure delivery)
    cb.report_actual(
        intent_id=intent.intent_id,
        job_id="gemini-session-abc123",
        provider="google",
        api="gemini-3.1-flash-lite",
        actual_calls=500,
        actual_cost_usd=2.50,
        status="completed",
        sync=True,           # wait for the HTTP response
    )
    cb.flush()  # wait for any pending async reports to drain

The client is also usable as a context manager for automatic final
actual reporting:

    with cb.intent(
        job_id="scrape-phase1-la",
        provider="google",
        api="places-text-search",
        expected_calls=312,
        expected_cost_usd=10.0,
    ) as ctx:
        for query in queries:
            result = call_api(query)
            ctx.add_calls(1, cost_usd=0.032)
        # ctx reports "completed" on exit (or "failed" on exception)

Configuration via environment variables (all optional — constructor
args take precedence):
    CLOUDMANAGEMENT_URL          Base URL of the CloudManagement service
    CLOUDMANAGEMENT_PROJECT_ID   Default project_id for this repo
    CLOUDMANAGEMENT_REPORT_TOKEN Default report token
    CLOUDMANAGEMENT_TIMEOUT      HTTP timeout in seconds (default 5)
    CLOUDMANAGEMENT_INTENT_TIMEOUT  Timeout for declare_intent (default 3)
    CLOUDMANAGEMENT_STRICT       If "true", raise on errors instead of logging

Errors are logged at WARNING and never raised — billing reporting is
best-effort and must not break the host application.  Set
CLOUDMANAGEMENT_STRICT=true to raise on errors instead.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("cloud_management_client")

__all__ = [
    "CloudManagementClient",
    "CloudManagementError",
    "IntentResponse",
    "ActualResponse",
    "IntentContext",
]

__version__ = "0.2.0"


class CloudManagementError(Exception):
    """Raised when CLOUDMANAGEMENT_STRICT=true and a request fails."""


def _fail(msg: str, exc: Exception | None = None, strict: bool = False) -> None:
    """Log an error and optionally raise in strict mode."""
    detail = f"{msg}: {exc}" if exc else msg
    log.warning("cloud_management: %s", detail)
    if strict:
        raise CloudManagementError(detail) from exc


@dataclass
class IntentResponse:
    """Response from POST /api/v1/intent."""
    intent_id: str = ""
    approved: bool = False
    budget_remaining_usd: float = 0.0
    kill_switch_armed: bool = False
    reason: str = ""
    warnings: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ActualResponse:
    """Response from POST /api/v1/actual."""
    actual_id: str = ""
    overrun_detected: bool = False
    status: str = ""
    overrun: dict[str, Any] = field(default_factory=dict)
    kill_result: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


class CloudManagementClient:
    """Client for the CloudManagement intent/actual reporting protocol.

    All methods are best-effort: errors are logged at WARNING and the
    method returns a failure response object rather than raising
    (unless CLOUDMANAGEMENT_STRICT=true).

    ``report_actual`` is asynchronous by default — it enqueues the HTTP
    request to a background daemon thread so it never blocks the caller.
    Use ``sync=True`` for the final "completed"/"failed" report, and call
    ``flush()`` to wait for pending async reports to drain (e.g. at
    shutdown).

    ``declare_intent`` is always synchronous because the caller needs
    the response to check ``.approved``.
    """

    def __init__(
        self,
        project_id: str = "",
        report_token: str = "",
        base_url: str = "",
        source_repo: str = "",
        timeout: int | None = None,
        intent_timeout: int | None = None,
        strict: bool | None = None,
    ) -> None:
        self.project_id = project_id or os.environ.get("CLOUDMANAGEMENT_PROJECT_ID", "")
        self.report_token = report_token or os.environ.get("CLOUDMANAGEMENT_REPORT_TOKEN", "")
        self.base_url = (base_url or os.environ.get("CLOUDMANAGEMENT_URL", "http://127.0.0.1:8080")).rstrip("/")
        self.source_repo = source_repo
        self.timeout = timeout if timeout is not None else int(os.environ.get("CLOUDMANAGEMENT_TIMEOUT", "5"))
        self.intent_timeout = intent_timeout if intent_timeout is not None else int(os.environ.get("CLOUDMANAGEMENT_INTENT_TIMEOUT", "3"))
        self.strict = strict if strict is not None else os.environ.get("CLOUDMANAGEMENT_STRICT", "false").lower() == "true"

        # Background queue for async report_actual calls
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()

        if not self.project_id:
            log.warning("cloud_management: project_id not set — client disabled")
        if not self.report_token:
            log.warning("cloud_management: report_token not set — client disabled")

    @property
    def enabled(self) -> bool:
        return bool(self.project_id and self.report_token)

    # ------------------------------------------------------------------
    # Background worker for async report_actual
    # ------------------------------------------------------------------

    def _ensure_worker(self) -> None:
        """Start the background worker thread if not already running."""
        if self._worker is not None and self._worker.is_alive():
            return
        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._worker_loop,
                name="cloud-management-reporter",
                daemon=True,
            )
            self._worker.start()

    def _worker_loop(self) -> None:
        """Process queued report_actual requests."""
        while True:
            item = self._queue.get()
            if item is None:
                # Sentinel — signal to stop
                self._queue.task_done()
                break
            try:
                self._post_sync(item["path"], item["payload"])
            except Exception as e:
                _fail(f"async report_actual failed", e, strict=self.strict)
            finally:
                self._queue.task_done()

    def flush(self, timeout: float = 5.0) -> None:
        """Wait for all pending async report_actual calls to complete.

        Blocks until the queue is drained or ``timeout`` seconds elapse.
        If the timeout expires, pending reports are NOT lost — the daemon
        worker thread will still process them, but the caller is no
        longer blocked.
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
        # Timeout expired — daemon thread will still process remaining items

    def close(self) -> None:
        """Signal the background worker to stop and wait briefly."""
        self._queue.put(None)
        if self._worker is not None:
            self._worker.join(timeout=2.0)

    # ------------------------------------------------------------------
    # Low-level HTTP
    # ------------------------------------------------------------------

    def _post_sync(self, path: str, payload: dict[str, Any], timeout: int | None = None) -> dict[str, Any] | None:
        """Synchronous HTTP POST. Returns parsed JSON or None on error."""
        if not self.enabled:
            return None
        url = f"{self.base_url}{path}"
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.report_token}",
            },
            method="POST",
        )
        t = timeout if timeout is not None else self.timeout
        try:
            with urllib.request.urlopen(req, timeout=t) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8")[:200]
            except Exception:
                pass
            _fail(f"POST {path} failed (HTTP {e.code}): {err_body}", strict=self.strict)
            return None
        except urllib.error.URLError as e:
            _fail(f"POST {path} connection error", e, strict=self.strict)
            return None
        except Exception as e:
            _fail(f"POST {path} unexpected error", e, strict=self.strict)
            return None

    # ------------------------------------------------------------------
    # Intent / Actual API
    # ------------------------------------------------------------------

    def declare_intent(
        self,
        job_id: str,
        provider: str = "",
        api: str = "",
        expected_calls: int = 0,
        expected_cost_usd: float = 0.0,
        expected_tokens: int | None = None,
        rate_limit_rpm: int = 0,
        job_name: str = "",
        window_start: str = "",
        window_end: str = "",
        kill: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        source_repo: str = "",
        intent_id: str = "",
    ) -> IntentResponse:
        """Declare expected API usage before making calls.

        Always synchronous — the caller needs the response to check
        ``.approved`` before proceeding.

        Returns an IntentResponse with .approved indicating whether
        the intent was accepted (budget not yet exceeded).
        """
        payload: dict[str, Any] = {
            "project_id": self.project_id,
            "job_id": job_id,
            "job_name": job_name,
            "provider": provider,
            "api": api,
            "expected_calls": expected_calls,
            "expected_cost_usd": expected_cost_usd,
            "rate_limit_rpm": rate_limit_rpm,
            "source_repo": source_repo or self.source_repo,
        }
        if expected_tokens is not None:
            payload["expected_tokens"] = expected_tokens
        if window_start:
            payload["window_start"] = window_start
        if window_end:
            payload["window_end"] = window_end
        if kill:
            payload["kill"] = kill
        if metadata:
            payload["metadata"] = metadata
        if intent_id:
            payload["intent_id"] = intent_id

        data = self._post_sync("/api/v1/intent", payload, timeout=self.intent_timeout)
        if data is None:
            return IntentResponse()
        return IntentResponse(
            intent_id=data.get("intent_id", ""),
            approved=data.get("approved", False),
            budget_remaining_usd=float(data.get("budget_remaining_usd", 0)),
            kill_switch_armed=data.get("kill_switch_armed", False),
            reason=data.get("reason", ""),
            warnings=data.get("warnings", []),
            raw=data,
        )

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
        payload: dict[str, Any] = {
            "project_id": self.project_id,
            "intent_id": intent_id,
            "job_id": job_id,
            "provider": provider,
            "api": api,
            "actual_calls": actual_calls,
            "actual_cost_usd": actual_cost_usd,
            "status": status,
        }
        if actual_tokens is not None:
            payload["actual_tokens"] = actual_tokens
        if started_at:
            payload["started_at"] = started_at
        if ended_at:
            payload["ended_at"] = ended_at

        if sync or not self.enabled:
            data = self._post_sync("/api/v1/actual", payload)
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
            # Async — enqueue to background worker
            self._ensure_worker()
            self._queue.put({"path": "/api/v1/actual", "payload": payload})
            return ActualResponse()  # placeholder — actual HTTP happens in background

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
        return IntentContext(self, job_id, **kwargs)


class IntentContext:
    """Context manager for an intent/actual lifecycle."""

    def __init__(
        self,
        client: CloudManagementClient,
        job_id: str,
        **intent_kwargs: Any,
    ) -> None:
        self._client = client
        self._job_id = job_id
        self._intent_kwargs = intent_kwargs
        self.intent: IntentResponse | None = None
        self._calls = 0
        self._cost = 0.0
        self._tokens: int | None = None
        self._start = time.time()
        self._reported = False

    def __enter__(self) -> "IntentContext":
        if not self._client.enabled:
            log.debug("cloud_management: client disabled, skipping intent")
            return self
        self.intent = self._client.declare_intent(
            job_id=self._job_id, **self._intent_kwargs
        )
        if not self.intent.approved:
            _fail(f"intent denied for {self._job_id}: {self.intent.reason}", strict=self._client.strict)
        return self

    def add_calls(self, calls: int, cost_usd: float = 0.0, tokens: int | None = None) -> None:
        """Accumulate usage during the job. Call after each API call."""
        self._calls += calls
        self._cost += cost_usd
        if tokens is not None:
            self._tokens = (self._tokens or 0) + tokens

    def report_incremental(self, status: str = "running") -> ActualResponse:
        """Send an incremental actual report mid-job (async)."""
        if not self.intent or not self.intent.intent_id:
            return ActualResponse()
        return self._client.report_actual(
            intent_id=self.intent.intent_id,
            job_id=self._job_id,
            provider=self._intent_kwargs.get("provider", ""),
            api=self._intent_kwargs.get("api", ""),
            actual_calls=self._calls,
            actual_cost_usd=self._cost,
            actual_tokens=self._tokens,
            status=status,
        )

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if not self._client.enabled or not self.intent or not self.intent.intent_id:
            return
        if self._reported:
            return
        status = "failed" if exc_type else "completed"
        try:
            # Final report is synchronous to ensure delivery
            self._client.report_actual(
                intent_id=self.intent.intent_id,
                job_id=self._job_id,
                provider=self._intent_kwargs.get("provider", ""),
                api=self._intent_kwargs.get("api", ""),
                actual_calls=self._calls,
                actual_cost_usd=self._cost,
                actual_tokens=self._tokens,
                status=status,
                sync=True,
            )
            self._reported = True
        except Exception as e:
            _fail(f"final actual report failed for {self._job_id}", e, strict=self._client.strict)
