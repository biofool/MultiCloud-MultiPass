"""Admin/ops Blueprint: quota polling, intent overrun polling,
billing reconciliation, and the service-info endpoint.

Split out of the original monolithic ``main.py`` — pure structural move,
no behavior change. Registered on ``main.app`` the same way as the
existing ``dashboard``/``intent``/``inventory`` blueprints. Reads main's
config flags via ``import main`` (qualified access) for the same
live-value reason as ``killswitch_actions.py`` — see that module's
docstring.

Note on auth (post-refactor review): these routes carry no in-app
token check, but they are not reachable without one — the Cloud Run
service is deployed private (see terraform/iam.tf's
``google_cloud_run_service_iam_member.invoker``), which grants
``roles/run.invoker`` *only* to the dedicated ``killswitch-rt`` service
account used by Cloud Scheduler's OIDC-authenticated calls
(terraform/scheduler.tf) and Pub/Sub push. An unauthenticated caller is
rejected by Cloud Run itself before this code ever runs. Bolting on an
app-level shared secret here wouldn't add real defense-in-depth (it
would just duplicate what IAM already enforces) and risks breaking
Cloud Scheduler, which doesn't send one. Left as-is; if per-caller
identity ever needs checking beyond Cloud Run IAM, verify the
OIDC token's `email` claim in-app rather than adding a shared secret.

Maintenance-mode snapshot (upstream convergence): the canonical
implementation of this module is maintained upstream in
``biofool/CloudManagement``. This copy is a frozen snapshot kept so
this repo stays standalone-runnable — it receives security fixes and
upstream backports only. See docs/convergence.md.
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


# ---------------------------------------------------------------------------
# Reconcile route — implemented in admin_reconcile.py (split during the
# ≤250-line refactor); importing it attaches POST /reconcile to ``bp``.
# Helpers re-exported so `from admin_routes import _in_window` etc. keep
# working for existing callers/tests.
# ---------------------------------------------------------------------------

from admin_reconcile import _in_window, _parse_ts  # noqa: E402,F401
