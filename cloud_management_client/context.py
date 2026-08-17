"""IntentContext: the context-manager wrapper returned by
``CloudManagementClient.intent()`` for automatic intent/actual lifecycle
management.

Split out of the original monolithic ``__init__.py`` — pure structural
move, no behavior change.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from .errors import JobKilledError, KillOrder, _fail, log
from .models import ActualResponse, IntentResponse

if TYPE_CHECKING:
    from .client import CloudManagementClient


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

    def report_incremental(self, status: str = "running", sync: bool = False) -> ActualResponse:
        """Send an incremental actual report mid-job.

        By default async (returns a placeholder). Pass ``sync=True`` to
        get the response synchronously — this is required for kill-order
        detection (the hub returns the kill directive on the response).

        If the hub returns a kill directive (overrun detected, budget
        exceeded, or a manual kill), this method raises ``JobKilledError``
        so the host application's own cleanup runs. The error is not
        raised in async mode (the placeholder response has no kill data).
        """
        if not self.intent or not self.intent.intent_id:
            return ActualResponse()
        resp = self._client.report_actual(
            intent_id=self.intent.intent_id,
            job_id=self._job_id,
            provider=self._intent_kwargs.get("provider", ""),
            api=self._intent_kwargs.get("api", ""),
            actual_calls=self._calls,
            actual_cost_usd=self._cost,
            actual_tokens=self._tokens,
            status=status,
            sync=sync,
        )
        # Check for kill directive in the response (only available in sync mode).
        if sync and resp.kill_result:
            kr = resp.kill_result
            if isinstance(kr, dict) and kr.get("killed"):
                raise JobKilledError(
                    f"job {self._job_id} killed by hub: {kr.get('reason', kr.get('rule', 'unknown'))}",
                    kill_order=kr,
                )
        return resp

    def check_kill_orders(self, since: str = "") -> list[KillOrder]:
        """Poll the hub for kill orders targeting this intent's project.

        Use this between reports for long-running jobs that need faster
        kill detection than the report cadence provides. Returns a list
        of ``KillOrder`` objects; an empty list means no kill orders.
        """
        if not self.intent or not self.intent.intent_id:
            return []
        return self._client.check_kill_orders(since=since)

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if not self._client.enabled or not self.intent or not self.intent.intent_id:
            return
        if self._reported:
            return
        # If the job was killed via JobKilledError, report "killed" status.
        # If another exception occurred, report "failed". Otherwise "completed".
        if exc_type is not None and isinstance(exc_val, JobKilledError):
            status = "killed"
        elif exc_type is not None:
            status = "failed"
        else:
            status = "completed"
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

