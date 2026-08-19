"""Intent-declaration and budget-admission API methods for
CloudManagementClient (``declare_intent``, ``get_intent``,
``wait_for_reschedule``, ``check_budget``, ``can_run``).

One of several mixins that ``client.CloudManagementClient`` combines —
split out of the original monolithic class for readability. Pure
structural move: every method here behaves identically to before, just
relocated. See ``client.py`` for how the mixins are assembled.
"""

from __future__ import annotations

from typing import Any

from .errors import log
from .models import BudgetCheck, IntentResponse


class _IntentOpsMixin:
    """``declare_intent``, ``get_intent``, ``wait_for_reschedule``,
    ``check_budget``, and ``can_run``."""

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
        application: str = "",
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
            "application": application or self.application,
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
            deferred=data.get("deferred", False),
            budget_remaining_usd=float(data.get("budget_remaining_usd", 0)),
            budget_short_usd=float(data.get("budget_short_usd", 0)),
            suggested_retry_at=data.get("suggested_retry_at", ""),
            kill_switch_armed=data.get("kill_switch_armed", False),
            reason=data.get("reason", ""),
            warnings=data.get("warnings", []),
            raw=data,
        )

    def get_intent(self, intent_id: str) -> dict[str, Any] | None:
        """Fetch a single intent by ID (Issue #39).

        Used by ``wait_for_reschedule`` and by callers that poll for
        status changes. Returns the full intent dict, or None on error.
        No project-scoped auth — the intent_id is an unguessable token.
        """
        return self._get_sync(f"/api/v1/intent/{intent_id}")

    def wait_for_reschedule(
        self,
        intent_id: str,
        timeout: float = 3600.0,
        poll_interval: float = 60.0,
    ) -> dict[str, Any] | None:
        """Poll ``GET /api/v1/intent/<intent_id>`` until the deferred intent
        is rescheduled (``status: scheduled``) or expired/failed, or
        ``timeout`` seconds elapse (Issue #39).

        Returns the final intent dict on status change, or None on
        timeout/error. This is the poll-based fallback for clients that
        cannot receive webhook callbacks — the primary notify mechanism
        is the ``resume_callback`` webhook (see reschedule.py).

        Blocks the calling thread; use from a background thread if the
        caller needs to continue other work.
        """
        import time as _time
        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            data = self._get_sync(f"/api/v1/intent/{intent_id}")
            if data is None:
                return None
            status = data.get("status", "")
            if status != "deferred":
                return data
            _time.sleep(poll_interval)
        log.warning("cloud_management: wait_for_reschedule timed out after %ss for %s", timeout, intent_id)
        return None

    def check_budget(self, expected_cost_usd: float = 0.0) -> BudgetCheck:
        """Pre-flight budget admission probe (budget-informed runtimes).

        Asks the hub "can I afford ``expected_cost_usd`` of work right now?"
        WITHOUT persisting an intent — the side-effect-free counterpart to
        ``declare_intent``'s deferral path. Use this before deciding which
        batch to run, whether to pull a queue, or whether to fire a cron
        job, so a runtime can pick budget-feasible work instead of
        declaring an intent that gets deferred (or killed mid-run).

        - ``expected_cost_usd > 0`` → POST admission decision
          (``admit`` / ``deferred`` / ``reason`` / ``suggested_retry_at``).
        - ``expected_cost_usd == 0`` (default) → GET read-only budget
          status (``budget_remaining_usd`` / ``over_budget``); ``admit``
          is True iff the project is not over budget.

        Always synchronous. On error returns a BudgetCheck with
        ``admit=False`` (fail-closed: when the hub is unreachable, do not
        start cost-incurring work on the assumption budget is available).
        """
        path = f"/api/v1/budget/{self.project_id}"
        if expected_cost_usd and expected_cost_usd > 0:
            data = self._post_sync(path, {"expected_cost_usd": expected_cost_usd},
                                   timeout=self.intent_timeout)
        else:
            data = self._get_sync(path, timeout=self.intent_timeout)
        if data is None:
            # Fail-closed: unknown budget state → do not admit.
            return BudgetCheck(admit=False, reason="budget_check_unavailable")
        if expected_cost_usd and expected_cost_usd > 0:
            return BudgetCheck(
                admit=bool(data.get("admit", False)),
                deferred=bool(data.get("deferred", False)),
                reason=data.get("reason", ""),
                budget_configured=bool(data.get("budget_configured", False)),
                budget_amount_usd=float(data.get("budget_amount_usd", 0)),
                spent_this_month_usd=float(data.get("spent_this_month_usd", 0)),
                budget_remaining_usd=float(data.get("budget_remaining_usd", 0)),
                budget_short_usd=float(data.get("budget_short_usd", 0)),
                suggested_retry_at=data.get("suggested_retry_at", ""),
                raw=data,
            )
        # GET status response — derive admit from over_budget.
        over = bool(data.get("over_budget", False))
        configured = bool(data.get("budget_configured", False))
        return BudgetCheck(
            admit=not over,
            deferred=False,
            budget_configured=configured,
            budget_amount_usd=float(data.get("budget_amount_usd", 0)),
            spent_this_month_usd=float(data.get("spent_this_month_usd", 0)),
            budget_remaining_usd=float(data.get("budget_remaining_usd", 0)),
            over_budget=over,
            raw=data,
        )

    def can_run(self, expected_cost_usd: float) -> bool:
        """Convenience wrapper around ``check_budget`` — True iff the
        projected cost is admissible right now. Fail-closed on error."""
        return self.check_budget(expected_cost_usd).admit
