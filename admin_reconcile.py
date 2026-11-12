"""Reconciliation route for the admin/ops blueprint: POST /reconcile
compares provider-billed costs against intent/actual self-reports and
recalibrates the expected_costs store.

Split out of ``admin_routes.py`` during the ≤250-line refactor — pure
structural move, no behavior change. Attaches to the shared ``bp``
blueprint from ``admin_routes``, which imports this module at the
bottom so registering the admin blueprint still mounts every route.
Reads main's config flags via ``import main`` (qualified access) for the
same live-value reason as ``admin_routes.py``.

Maintenance-mode snapshot (upstream convergence): the canonical
implementation of this module is maintained upstream in
``biofool/CloudManagement``. This copy is a frozen snapshot kept so
this repo stays standalone-runnable — it receives security fixes and
upstream backports only. See docs/convergence.md.
"""

from __future__ import annotations

import json

import main
import registry
from admin_routes import bp


def _parse_ts(created_at: str):
    """Parse an ISO timestamp to an aware datetime, or None if unparseable."""
    from datetime import datetime, timezone
    try:
        when = datetime.fromisoformat(created_at)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when


def _in_window(created_at: str, since, until) -> bool:
    """True if an ISO timestamp falls in [since, until). Unparseable -> False."""
    when = _parse_ts(created_at)
    return when is not None and since <= when < until


@bp.route("/reconcile", methods=["POST"])
def handle_reconcile():
    """Reconciliation tier — pull actual billed costs and compare to self-reports.

    Invoked by Cloud Scheduler daily (or on-demand).  For each registered
    account, fetch billed costs from the cloud provider's billing API and
    compare against the intent/actual self-reports.  Discrepancies
    recalibrate the expected_costs store.
    """
    from datetime import datetime, timedelta, timezone
    from providers import registry as provider_registry
    import intent as intent_mod

    until = datetime.now(timezone.utc) - timedelta(hours=24)
    since = until - timedelta(hours=24)

    results = []
    for account in registry.list_accounts():
        if account.allowlist:
            continue
        cloud = account.cloud or "gcp"
        project = account.gcp_project_id or account.project_id

        billed = provider_registry.fetch_billed_costs(cloud, project, since, until)
        if not billed:
            results.append({"project_id": account.project_id, "billed_count": 0, "note": "no billing data"})
            continue

        # Compare billed vs self-reported per provider
        self_reported = {}
        for actual in intent_mod.list_actuals(project_id=account.project_id):
            # Compare the same window as the billed costs, not all-time (CISO-121).
            if not _in_window(actual.created_at, since, until):
                # A dropped actual whose created_at never parses (legacy rows
                # predate the field) silently zeroes that provider's
                # self-report — name it (issue #17).
                if _parse_ts(actual.created_at) is None:
                    main.log.warning(json.dumps({
                        "event": "reconcile_actual_dropped",
                        "project_id": account.project_id,
                        "actual_id": actual.actual_id,
                        "provider": actual.provider,
                        "reason": "unparseable created_at",
                    }))
                continue
            key = actual.provider
            if key not in self_reported:
                self_reported[key] = {"cost": 0.0, "calls": 0}
            self_reported[key]["cost"] += actual.actual_cost_usd
            self_reported[key]["calls"] += actual.actual_calls

        variances = []
        for cost in billed:
            provider_key = cost.provider
            reported = self_reported.get(provider_key, {"cost": 0.0, "calls": 0})
            if cost.cost_usd > 0:
                variance = abs(reported["cost"] - cost.cost_usd) / cost.cost_usd
            else:
                variance = 0.0
            variances.append({
                "provider": provider_key,
                "billed_cost": round(cost.cost_usd, 4),
                "reported_cost": round(reported["cost"], 4),
                "variance": round(variance, 4),
            })

            # Recalibrate expected costs if variance is significant
            if cost.cost_usd > 0 and variance > 0.15:
                calibration_delta = (cost.cost_usd - reported["cost"]) / max(reported["calls"], 1)
                ec = intent_mod.ExpectedCost(
                    project_id=account.project_id,
                    provider=provider_key,
                    calibration_delta=round(calibration_delta, 6),
                )
                intent_mod.save_expected_cost(ec)

        results.append({
            "project_id": account.project_id,
            "billed_count": len(billed),
            "variances": variances,
        })

    return (
        json.dumps({"reconciled": True, "results": results, "dry_run": main.DRY_RUN}),
        200,
        {"Content-Type": "application/json"},
    )
