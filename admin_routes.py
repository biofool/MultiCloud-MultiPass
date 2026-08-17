"""Admin/ops Blueprint: quota polling, intent overrun polling,
billing reconciliation, and the service-info endpoint.

Split out of the original monolithic ``main.py`` — pure structural move,
no behavior change. Registered on ``main.app`` the same way as the
existing ``dashboard``/``intent``/``inventory`` blueprints. Reads main's
config flags via ``import main`` (qualified access) for the same
live-value reason as ``killswitch_actions.py`` — see that module's
docstring.
"""

from __future__ import annotations

import json

import flask

import main
import registry

bp = flask.Blueprint("admin", __name__)


@bp.route("/poll", methods=["POST"])
def handle_poll():
    """Invoked by Cloud Scheduler every few minutes — real-time quota-spike check.

    Complements the budget-alert path with a fast, cost-independent signal.
    Also checks intent/actual overruns from the reporting protocol.
    See poller.py for detection rules.
    """
    import poller

    trips = poller.poll_all_accounts(main.execute_killswitch)
    return (
        json.dumps({"checked": True, "trips": trips, "dry_run": main.DRY_RUN}),
        200,
        {"Content-Type": "application/json"},
    )


@bp.route("/poll-intents", methods=["POST"])
def handle_poll_intents():
    """Check all active intents for overruns — invoked by Cloud Scheduler.

    This is the intent/actual detection path: for every intent with
    status "running", check if actuals have exceeded the variance
    threshold and kill if so.

    **Self-protection:** Intents belonging to SELF_PROJECT_ID are
    checked and logged but never killed — same feedback-loop guard as
    the budget-alert and quota-poller paths.
    """
    import intent as intent_mod

    checks = []
    for intent_obj in intent_mod.list_intents(status="running"):
        is_self = main.SELF_PROJECT_ID and intent_obj.project_id == main.SELF_PROJECT_ID

        overrun = intent_mod.check_intent_overrun(intent_obj)
        if overrun:
            if is_self:
                main.log.critical(json.dumps({
                    "event": "self_intent_overrun",
                    **overrun,
                    "message": "Hub project intent overrun — kill blocked to prevent feedback loop.",
                }))
                checks.append({**overrun, "kill_result": {"killed": False, "reason": "self_project_blocked"}})
                continue
            kill_result = intent_mod.kill_intent(intent_obj, reason=overrun["rule"], rule=overrun["rule"])
            checks.append({**overrun, "kill_result": kill_result})
        else:
            # Also check project budget
            budget_check = intent_mod.check_project_budget(intent_obj.project_id)
            if budget_check:
                if is_self:
                    main.log.critical(json.dumps({
                        "event": "self_budget_exceeded",
                        **budget_check,
                        "message": "Hub project budget exceeded — kill blocked to prevent feedback loop.",
                    }))
                    checks.append({**budget_check, "intent_id": intent_obj.intent_id,
                                   "kill_result": {"killed": False, "reason": "self_project_blocked"}})
                    continue
                kill_result = intent_mod.kill_intent(intent_obj, reason=budget_check["rule"], rule=budget_check["rule"])
                checks.append({**budget_check, "intent_id": intent_obj.intent_id, "kill_result": kill_result})

    return (
        json.dumps({"checked": True, "overruns": checks, "dry_run": main.DRY_RUN}),
        200,
        {"Content-Type": "application/json"},
    )


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


@bp.route("/", methods=["GET"])
def info():
    return (
        json.dumps({
            "service": "cloudmanagement",
            "dry_run": main.DRY_RUN,
            "self_project_id": main.SELF_PROJECT_ID,
            "allowlist": sorted(main.ALLOWLIST),
            "billing_shutoff": main.ENABLE_BILLING_SHUTOFF,
            "run_pause": main.ENABLE_RUN_PAUSE,
            "trigger_disable": main.ENABLE_TRIGGER_DISABLE,
            "stop_compute": main.STOP_COMPUTE_INSTANCES,
            "api_key_revoke": main.ENABLE_API_KEY_REVOKE,
            "gke_scale_down": main.ENABLE_GKE_SCALE_DOWN,
            "registry_backend": "firestore" if registry.USE_FIRESTORE else registry.ACCOUNTS_FILE,
            "endpoints": {
                "budget_alert": "POST /",
                "health": "GET /health",
                "poll_quota": "POST /poll",
                "poll_intents": "POST /poll-intents",
                "reconcile": "POST /reconcile",
                "declare_intent": "POST /api/v1/intent",
                "report_actual": "POST /api/v1/actual",
                "expected_costs": "GET /api/v1/expected-costs/<project_id>",
                "list_intents": "GET /api/v1/intents",
                "dashboard": "GET /dashboard",
                "inventory": "GET /api/v1/inventory",
            },
        }),
        200,
        {"Content-Type": "application/json"},
    )
