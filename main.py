"""
GCP Cost Kill Switch — Cloud Run service.

Triggered by Pub/Sub push subscription when a Cloud Billing budget alert fires.
Parses the alert, evaluates thresholds, and takes idempotent cost-control actions
on non-production projects.

Environment variables:
  DRY_RUN              "true" to log actions without executing (default: "true")
  ALLOWLIST            Comma-separated project IDs that must never be touched
  ENABLE_BILLING_SHUTOFF  "true" to allow disabling billing (default: "false")
  ENABLE_RUN_PAUSE     "true" to pause Cloud Scheduler jobs (default: "false")
  ENABLE_TRIGGER_DISABLE  "true" to disable Cloud Build triggers (default: "false")
  STOP_COMPUTE_INSTANCES  "true" to stop GCE instances (default: "false")
  LOG_LEVEL            "DEBUG" | "INFO" | "WARNING" | "ERROR" (default: "INFO")
  PROJECT_ID           Target project for billing (the billing account's host project)
  ALERT_TOPIC          Pub/Sub topic name (for dedup / validation)
  ENABLE_API_KEY_REVOKE   "true" to revoke API keys (default: "false")
  ENABLE_GKE_SCALE_DOWN   "true" to scale GKE node pools to 0 (default: "false")

See registry.py for the multi-account registry (Firestore or local YAML) that
backs the allowlist and per-account settings, and poller.py for the real-time
quota-spike detector that also drives execute_killswitch().
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

import flask
from google.cloud import run_v2, scheduler_v1, compute_v1, billing_v1  # noqa: F401 (re-exported: `main.billing_v1` etc. are patched directly in tests, and killswitch_actions.py imports the same cached modules)

import registry

# ---------------------------------------------------------------------------
# Logging — structured JSON to stdout, minimal volume
# ---------------------------------------------------------------------------

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='{"ts":"%(asctime)s","level":"%(levelname)s","msg":%(message)s}',
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    stream=sys.stdout,
)
log = logging.getLogger("killswitch")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"
ALLOWLIST = {
    p.strip()
    for p in os.environ.get("ALLOWLIST", "").split(",")
    if p.strip()
}
ENABLE_BILLING_SHUTOFF = os.environ.get("ENABLE_BILLING_SHUTOFF", "false").lower() == "true"
ENABLE_RUN_PAUSE = os.environ.get("ENABLE_RUN_PAUSE", "false").lower() == "true"
ENABLE_TRIGGER_DISABLE = os.environ.get("ENABLE_TRIGGER_DISABLE", "false").lower() == "true"
STOP_COMPUTE_INSTANCES = os.environ.get("STOP_COMPUTE_INSTANCES", "false").lower() == "true"
ENABLE_API_KEY_REVOKE = os.environ.get("ENABLE_API_KEY_REVOKE", "false").lower() == "true"
ENABLE_GKE_SCALE_DOWN = os.environ.get("ENABLE_GKE_SCALE_DOWN", "false").lower() == "true"
ALERT_TOPIC = os.environ.get("ALERT_TOPIC", "")

# The project this service runs in. CloudManagement must NEVER execute the
# kill switch against its own project — doing so would scale itself to
# zero, creating a feedback loop where the monitor generates cost by
# being repeatedly invoked by Cloud Scheduler while unable to monitor
# other projects. Budget alerts for this project go to email (see
# terraform), not to the kill switch.
SELF_PROJECT_ID = os.environ.get("SELF_PROJECT_ID", os.environ.get("PROJECT_ID", ""))

# Budget alert type constants — defined in alerts.py, re-exported here so
# `main.ALERT_TYPE_BUDGET` / `main.ALERT_TYPE_FORECAST` keep working for
# anyone importing them from this module, as before the split.
from alerts import (  # noqa: E402
    ALERT_TYPE_BUDGET,
    ALERT_TYPE_FORECAST,
    BudgetAlert,
    parse_pubsub_message,
    should_take_action,
)
from dedup import _DEDUP_TTL_SECONDS, _is_duplicate, _processed_messages  # noqa: E402
from killswitch_actions import (  # noqa: E402
    disable_billing,
    disable_build_triggers,
    disable_cloud_run_services,
    pause_scheduler_jobs,
    revoke_api_keys,
    scale_down_gke_clusters,
    stop_compute_instances,
)

# ---------------------------------------------------------------------------
# Kill switch orchestration — shared by the budget-alert path and the
# real-time quota poller (poller.py)
# ---------------------------------------------------------------------------

def execute_killswitch(project_id: str, reason: str) -> list[dict]:
    """Run every enabled kill switch action against a single project.

    `reason` is a short machine-readable tag (e.g. "budget_alert",
    "quota_spike") included in logs so the trigger source is traceable.

    **Self-protection:** If `project_id` matches `SELF_PROJECT_ID`, the
    kill switch is NOT executed.  CloudManagement must never kill its own
    infrastructure — doing so creates a feedback loop where the monitor
    generates cost by being repeatedly invoked (Cloud Scheduler cold
    starts) while unable to monitor other projects.  A critical log is
    emitted so operators are alerted via log-based metrics.
    """
    if SELF_PROJECT_ID and project_id == SELF_PROJECT_ID:
        log.critical(json.dumps({
            "event": "self_kill_blocked",
            "project": project_id,
            "reason": reason,
            "message": "Kill switch blocked on self project — would create feedback loop. "
                       "Investigate hub project costs manually.",
        }))
        return []

    log.info(json.dumps({"event": "processing_project", "project": project_id, "reason": reason, "dry_run": DRY_RUN}))

    all_actions: list[dict] = []

    actions = disable_cloud_run_services(project_id)
    all_actions.extend({"action": "scale_to_zero", "target": a} for a in actions)

    actions = pause_scheduler_jobs(project_id)
    all_actions.extend({"action": "pause_scheduler", "target": a} for a in actions)

    actions = disable_build_triggers(project_id)
    all_actions.extend({"action": "disable_trigger", "target": a} for a in actions)

    actions = stop_compute_instances(project_id)
    all_actions.extend({"action": "stop_instance", "target": a} for a in actions)

    actions = scale_down_gke_clusters(project_id)
    all_actions.extend({"action": "scale_down_gke", "target": a} for a in actions)

    actions = revoke_api_keys(project_id)
    all_actions.extend({"action": "revoke_api_key", "target": a} for a in actions)

    if disable_billing(project_id):
        all_actions.append({"action": "disable_billing", "target": project_id})

    log.info(json.dumps({
        "event": "killswitch_complete",
        "project": project_id,
        "reason": reason,
        "actions_taken": len(all_actions),
        "dry_run": DRY_RUN,
        "actions": all_actions[:50],  # cap log size
    }))
    return all_actions


def is_project_protected(project_id: str) -> bool:
    """True if project_id must never be touched (env allowlist or registry)."""
    return project_id in ALLOWLIST or registry.is_allowlisted(project_id)


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------

def process_alert(envelope: dict[str, Any]) -> tuple[int, str]:
    """Process a Pub/Sub push envelope. Returns (HTTP status, message)."""
    # Validate envelope structure
    if not isinstance(envelope, dict) or "message" not in envelope:
        log.warning(json.dumps({"event": "malformed_envelope"}))
        return (400, "Malformed envelope")

    message = envelope["message"]
    msg_id = message.get("messageId", "")

    # Dedup
    if msg_id and _is_duplicate(msg_id):
        log.info(json.dumps({"event": "duplicate", "message_id": msg_id}))
        return (200, "Duplicate — already processed")

    # Parse
    alert = parse_pubsub_message(envelope)
    if alert is None:
        return (400, "Failed to parse alert")

    log.info(json.dumps({
        "event": "alert_received",
        "message_id": msg_id,
        "budget": alert.budget_name,
        "type": alert.alert_type,
        "threshold": alert.threshold_percent,
        "actual": alert.actual_spend,
        "forecast": alert.forecasted_spend,
        "budget_amount": alert.budget_amount,
        "projects": alert.project_ids,
        "dry_run": DRY_RUN,
    }))

    # Evaluate threshold
    if not should_take_action(alert):
        log.info(json.dumps({"event": "below_threshold", "threshold": alert.threshold_percent}))
        return (200, "Below action threshold")

    # Self-protection: warn on self-project budget alerts but never kill
    self_alerts = [p for p in alert.project_ids if SELF_PROJECT_ID and p == SELF_PROJECT_ID]
    if self_alerts:
        log.warning(json.dumps({
            "event": "self_budget_alert",
            "projects": self_alerts,
            "threshold": alert.threshold_percent,
            "actual": alert.actual_spend,
            "budget_amount": alert.budget_amount,
            "message": "Hub project budget alert — investigate manually. "
                       "Kill switch is blocked on self project to prevent feedback loop.",
        }))

    # Determine target projects (env ALLOWLIST or registry allowlist protects a project)
    # Also exclude self project — execute_killswitch() has a hard block, but
    # filtering here avoids unnecessary log noise.
    target_projects = [
        p for p in alert.project_ids
        if not is_project_protected(p) and not (SELF_PROJECT_ID and p == SELF_PROJECT_ID)
    ]
    skipped = [p for p in alert.project_ids if is_project_protected(p)]

    if skipped:
        log.info(json.dumps({"event": "allowlist_skip", "projects": skipped}))

    if not target_projects:
        log.info(json.dumps({"event": "no_targets", "reason": "all_allowlisted"}))
        return (200, "All projects allowlisted")

    all_actions: list[dict] = []
    for project_id in target_projects:
        all_actions.extend(execute_killswitch(project_id, reason="budget_alert"))

    return (200, f"Processed — {len(all_actions)} actions ({'dry-run' if DRY_RUN else 'live'})")


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = flask.Flask(__name__)

# Register dashboard blueprint
from dashboard import bp as dashboard_bp
app.register_blueprint(dashboard_bp)

# Register intent/actual protocol blueprint
from intent import bp as intent_bp
app.register_blueprint(intent_bp)

# Register inventory blueprint
from inventory import bp as inventory_bp
app.register_blueprint(inventory_bp)


# Register admin/ops blueprint (quota poll, intent poll, reconcile, info)
from admin_routes import bp as admin_bp
app.register_blueprint(admin_bp)


@app.route("/", methods=["POST"])
def handle_pubsub():
    envelope = flask.request.get_json(silent=True)
    if envelope is None:
        return ("Bad Request: not JSON", 400)
    status, msg = process_alert(envelope)
    return (msg, status)


@app.route("/health", methods=["GET"])
def health():
    return ("OK", 200)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
