"""
Real-time runaway-consumption poller.

Billing budget alerts (main.py's `/` route) lag 12-24 hours behind actual
spend, which is too slow for a genuine runaway loop (e.g. a retry storm
against a paid API). This module is the fast path: it's invoked by a Cloud
Scheduler job every few minutes, reads Cloud Monitoring quota metrics for
every registered account, and trips execute_killswitch() the moment a
project looks like it's misbehaving — well before the bill reflects it.

Detection rules (first match wins), per account in registry.list_accounts():
  1. quota_exceeded  — serviceruntime.googleapis.com/quota/exceeded is
     nonzero in the last window. The client is already being throttled
     (HTTP 429s) — something is clearly looping.
  2. baseline_ratio   — quota/rate/net_usage in the last window is more
     than BASELINE_MULTIPLIER times the trailing 1-hour average. Catches
     a spike even when the absolute volume is still below any fixed cap.
  3. absolute_cap     — quota/rate/net_usage in the last window exceeds
     the account's quota_rpm_cap (registry field), for accounts too new
     to have a meaningful baseline yet. Skipped when quota_rpm_cap == 0.

Environment variables:
  BASELINE_MULTIPLIER   Trip threshold as a multiple of the 1h baseline (default: 5)
  POLL_WINDOW_MINUTES    Width of the "recent" window (default: 5)
  BASELINE_WINDOW_MINUTES  Width of the trailing baseline window (default: 60)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import registry

log = logging.getLogger("killswitch.poller")

BASELINE_MULTIPLIER = float(os.environ.get("BASELINE_MULTIPLIER", "5"))
POLL_WINDOW_MINUTES = int(os.environ.get("POLL_WINDOW_MINUTES", "5"))
BASELINE_WINDOW_MINUTES = int(os.environ.get("BASELINE_WINDOW_MINUTES", "60"))

_QUOTA_EXCEEDED_METRIC = "serviceruntime.googleapis.com/quota/exceeded"
_QUOTA_USAGE_METRIC = "serviceruntime.googleapis.com/quota/rate/net_usage"

_monitoring_client = None


def _get_monitoring_client():
    global _monitoring_client
    if _monitoring_client is None:
        from google.cloud import monitoring_v3

        _monitoring_client = monitoring_v3.MetricServiceClient()
    return _monitoring_client


def _sum_metric(project_id: str, metric_type: str, minutes: int) -> float:
    """Sum a metric's points over the last `minutes`. Returns 0.0 on any error."""
    from google.cloud import monitoring_v3

    client = _get_monitoring_client()
    now = datetime.now(timezone.utc)
    interval = monitoring_v3.TimeInterval(
        end_time=now,
        start_time=now - timedelta(minutes=minutes),
    )
    try:
        results = client.list_time_series(
            request={
                "name": f"projects/{project_id}",
                "filter": f'metric.type = "{metric_type}"',
                "interval": interval,
                "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
            }
        )
        total = 0.0
        for series in results:
            for point in series.points:
                val = point.value
                total += val.double_value or val.int64_value or 0
        return total
    except Exception as exc:
        log.warning(json.dumps({"event": "monitoring_query_error", "project": project_id, "metric": metric_type, "error": str(exc)[:200]}))
        return 0.0


def check_account(account: "registry.Account") -> dict[str, Any] | None:
    """Evaluate one account against the detection rules.

    Returns a dict describing the trip reason if runaway consumption was
    detected, otherwise None.
    """
    project_id = account.project_id

    exceeded = _sum_metric(project_id, _QUOTA_EXCEEDED_METRIC, POLL_WINDOW_MINUTES)
    if exceeded > 0:
        return {"rule": "quota_exceeded", "project": project_id, "value": exceeded}

    recent = _sum_metric(project_id, _QUOTA_USAGE_METRIC, POLL_WINDOW_MINUTES)
    recent_rate = recent / POLL_WINDOW_MINUTES if POLL_WINDOW_MINUTES else recent

    baseline_total = _sum_metric(project_id, _QUOTA_USAGE_METRIC, BASELINE_WINDOW_MINUTES)
    baseline_rate = baseline_total / BASELINE_WINDOW_MINUTES if BASELINE_WINDOW_MINUTES else 0

    if baseline_rate > 0 and recent_rate > baseline_rate * BASELINE_MULTIPLIER:
        return {
            "rule": "baseline_ratio",
            "project": project_id,
            "recent_rate": recent_rate,
            "baseline_rate": baseline_rate,
            "multiplier": recent_rate / baseline_rate,
        }

    if account.quota_rpm_cap > 0 and recent_rate > account.quota_rpm_cap:
        return {
            "rule": "absolute_cap",
            "project": project_id,
            "recent_rate": recent_rate,
            "cap": account.quota_rpm_cap,
        }

    return None


def poll_all_accounts(execute_killswitch) -> list[dict]:
    """Check every registered, non-allowlisted account and trip the kill
    switch for any that look like they're runaway.

    `execute_killswitch` is injected (rather than imported) so tests can
    pass a mock/fake without needing real GCP clients.

    **Self-protection:** The hub project (SELF_PROJECT_ID) is still
    checked for runaway consumption, but if a trip is detected, the kill
    switch is NOT called — only a critical warning is logged.  This
    prevents the feedback loop where CloudManagement kills its own
    infrastructure and then can't monitor anything.
    """
    self_project_id = os.environ.get("SELF_PROJECT_ID", os.environ.get("PROJECT_ID", ""))

    trips: list[dict] = []
    for account in registry.list_accounts():
        if account.allowlist:
            continue
        trip = check_account(account)
        if trip is None:
            continue

        # Self-protection: warn but don't kill self
        if self_project_id and account.project_id == self_project_id:
            log.critical(json.dumps({
                "event": "self_runaway_detected",
                **trip,
                "message": "Hub project showing runaway consumption — kill switch blocked. "
                           "Investigate manually to prevent feedback loop.",
            }))
            trips.append({**trip, "actions_taken": 0, "self_blocked": True})
            continue

        log.warning(json.dumps({"event": "runaway_detected", **trip}))
        actions = execute_killswitch(account.project_id, reason="quota_spike")
        trips.append({**trip, "actions_taken": len(actions)})

    return trips
