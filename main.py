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

import base64
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import flask
from google.cloud import run_v2, scheduler_v1, compute_v1, billing_v1

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

# Budget alert type constants from Cloud Billing
ALERT_TYPE_BUDGET = "budget"
ALERT_TYPE_FORECAST = "forecast"

# ---------------------------------------------------------------------------
# Dedup cache — in-memory, survives only within a single container instance
# ---------------------------------------------------------------------------

_processed_messages: dict[str, float] = {}
_DEDUP_TTL_SECONDS = 600  # 10 min


def _is_duplicate(msg_id: str) -> bool:
    now = time.time()
    for k in [k for k, v in _processed_messages.items() if now - v >= _DEDUP_TTL_SECONDS]:
        del _processed_messages[k]
    if msg_id in _processed_messages:
        return True
    _processed_messages[msg_id] = now
    return False


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class BudgetAlert:
    """Parsed budget alert from Pub/Sub."""
    alert_type: str          # "budget" or "forecast"
    budget_name: str
    threshold_percent: float
    actual_spend: float
    forecasted_spend: float
    budget_amount: float
    currency: str
    project_ids: list[str]
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_actual(self) -> bool:
        return self.alert_type == ALERT_TYPE_BUDGET

    @property
    def is_forecast(self) -> bool:
        return self.alert_type == ALERT_TYPE_FORECAST


# ---------------------------------------------------------------------------
# Alert parsing
# ---------------------------------------------------------------------------

def parse_pubsub_message(envelope: dict[str, Any]) -> BudgetAlert | None:
    """Parse a Pub/Sub push envelope into a BudgetAlert.

    Returns None if the message is malformed.
    """
    try:
        message = envelope["message"]
        data_b64 = message.get("data", "")
        raw = json.loads(base64.b64decode(data_b64).decode("utf-8"))
    except (KeyError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        log.error(json.dumps({"event": "parse_error", "error": str(exc)}))
        return None

    # Cloud Billing budget notification schema (costThresholdString or similar)
    try:
        alert_type = raw.get("alertType", ALERT_TYPE_BUDGET)
        threshold = float(raw.get("thresholdPercent", raw.get("threshold", 0)))
        actual = float(raw.get("actualCost", raw.get("costAmount", 0)))
        forecast = float(raw.get("forecastCost", raw.get("forecastAmount", 0)))
        budget_amt = float(raw.get("budgetAmount", 0))
        currency = raw.get("currency", raw.get("currencyCode", "USD"))
        budget_name = raw.get("budgetName", raw.get("displayName", "unknown"))

        # Projects can be in a list or single field
        project_ids = raw.get("projectIds", [])
        if isinstance(project_ids, str):
            project_ids = [project_ids]
        if not project_ids:
            # If no projects specified, the alert applies to the billing account
            project_ids = []

        return BudgetAlert(
            alert_type=alert_type,
            budget_name=budget_name,
            threshold_percent=threshold,
            actual_spend=actual,
            forecasted_spend=forecast,
            budget_amount=budget_amt,
            currency=currency,
            project_ids=project_ids,
            raw=raw,
        )
    except (ValueError, TypeError) as exc:
        log.error(json.dumps({"event": "parse_error", "error": str(exc), "raw": str(raw)[:500]}))
        return None


# ---------------------------------------------------------------------------
# Action: scale Cloud Run services to 0 min instances
# ---------------------------------------------------------------------------

def _run_parent(project_id: str, region: str) -> str:
    return f"projects/{project_id}/locations/{region}"


def disable_cloud_run_services(project_id: str, regions: list[str] | None = None) -> list[str]:
    """Set min instances to 0 on all Cloud Run services in a project."""
    regions = regions or ["us-central1", "us-east1", "europe-west1"]
    actions = []
    client = run_v2.ServicesClient()

    for region in regions:
        parent = _run_parent(project_id, region)
        try:
            services = client.list_services(request={"parent": parent})
        except Exception as exc:
            log.warning(json.dumps({"event": "list_services_error", "project": project_id, "region": region, "error": str(exc)}))
            continue

        for svc in services:
            svc_name = svc.name  # projects/{proj}/locations/{reg}/services/{name}
            changed = False
            template = svc.template
            if template.scaling and template.scaling.min_instance_count and template.scaling.min_instance_count > 0:
                template.scaling.min_instance_count = 0
                changed = True

            # Restrict ingress to internal-only as a belt-and-suspenders measure
            if svc.ingress != run_v2.IngressTraffic.INTERNAL_ONLY:
                svc.ingress = run_v2.IngressTraffic.INTERNAL_ONLY
                changed = True

            if changed:
                if DRY_RUN:
                    log.info(json.dumps({"event": "dry_run", "action": "scale_to_zero", "service": svc_name}))
                else:
                    try:
                        req = run_v2.UpdateServiceRequest(
                            service=svc,
                            field_mask="template.scaling.min_instance_count,ingress",
                        )
                        client.update_service(request=req)
                        log.info(json.dumps({"event": "action", "action": "scale_to_zero", "service": svc_name, "status": "done"}))
                    except Exception as exc:
                        log.error(json.dumps({"event": "action_error", "action": "scale_to_zero", "service": svc_name, "error": str(exc)}))
                actions.append(svc_name)
    return actions


# ---------------------------------------------------------------------------
# Action: pause Cloud Scheduler jobs
# ---------------------------------------------------------------------------

def pause_scheduler_jobs(project_id: str, regions: list[str] | None = None) -> list[str]:
    """Pause all Cloud Scheduler jobs in a project."""
    if not ENABLE_RUN_PAUSE:
        return []
    regions = regions or ["us-central1", "us-east1", "europe-west1"]
    actions = []
    client = scheduler_v1.CloudSchedulerClient()

    for region in regions:
        parent = _run_parent(project_id, region)
        try:
            jobs = client.list_jobs(request={"parent": parent})
        except Exception as exc:
            log.warning(json.dumps({"event": "list_jobs_error", "project": project_id, "region": region, "error": str(exc)}))
            continue

        for job in jobs:
            if job.state == scheduler_v1.Job.State.ENABLED:
                job_name = job.name
                if DRY_RUN:
                    log.info(json.dumps({"event": "dry_run", "action": "pause_scheduler", "job": job_name}))
                else:
                    try:
                        paused = scheduler_v1.Job()
                        paused.name = job_name
                        paused.state = scheduler_v1.Job.State.PAUSED
                        client.update_job(request={"job": paused, "update_mask": {"paths": ["state"]}})
                        log.info(json.dumps({"event": "action", "action": "pause_scheduler", "job": job_name, "status": "done"}))
                    except Exception as exc:
                        log.error(json.dumps({"event": "action_error", "action": "pause_scheduler", "job": job_name, "error": str(exc)}))
                actions.append(job_name)
    return actions


# ---------------------------------------------------------------------------
# Action: disable Cloud Build triggers
# ---------------------------------------------------------------------------

def disable_build_triggers(project_id: str) -> list[str]:
    """Disable all Cloud Build triggers in a project."""
    if not ENABLE_TRIGGER_DISABLE:
        return []
    actions = []
    # Use REST via requests to avoid heavy client dependency
    import requests
    import google.auth.transport.requests as tr
    import google.auth

    creds, _ = google.auth.default()
    creds.refresh(tr.Request())

    url = f"https://cloudbuild.googleapis.com/v1/projects/{project_id}/triggers"
    headers = {"Authorization": f"Bearer {creds.token}"}
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        triggers = resp.json().get("triggers", [])
    except Exception as exc:
        log.warning(json.dumps({"event": "list_triggers_error", "project": project_id, "error": str(exc)}))
        return []

    for trig in triggers:
        tid = trig.get("id")
        if not tid or not trig.get("disabled", False) is False:
            continue
        if DRY_RUN:
            log.info(json.dumps({"event": "dry_run", "action": "disable_trigger", "trigger": tid}))
        else:
            try:
                patch_url = f"https://cloudbuild.googleapis.com/v2/projects/{project_id}/triggers/{tid}"
                patch_resp = requests.patch(patch_url, headers=headers, json={"disabled": True}, timeout=30)
                patch_resp.raise_for_status()
                log.info(json.dumps({"event": "action", "action": "disable_trigger", "trigger": tid, "status": "done"}))
            except Exception as exc:
                log.error(json.dumps({"event": "action_error", "action": "disable_trigger", "trigger": tid, "error": str(exc)}))
        actions.append(tid)
    return actions


# ---------------------------------------------------------------------------
# Action: stop GCE instances
# ---------------------------------------------------------------------------

def stop_compute_instances(project_id: str, zones: list[str] | None = None) -> list[str]:
    """Stop all running GCE instances in configured zones."""
    if not STOP_COMPUTE_INSTANCES:
        return []
    actions = []
    client = compute_v1.InstancesClient()

    # If no zones specified, list all zones then iterate
    if not zones:
        try:
            zones_client = compute_v1.ZonesClient()
            zone_list = list(zones_client.list(project=project_id))
            zones = [z.name for z in zone_list]
        except Exception as exc:
            log.warning(json.dumps({"event": "list_zones_error", "project": project_id, "error": str(exc)}))
            return []

    for zone in zones:
        try:
            instances = client.list(project=project_id, zone=zone)
        except Exception as exc:
            log.warning(json.dumps({"event": "list_instances_error", "project": project_id, "zone": zone, "error": str(exc)}))
            continue

        for inst in instances:
            if inst.status == compute_v1.Instance.Status.RUNNING:
                inst_name = inst.name
                if DRY_RUN:
                    log.info(json.dumps({"event": "dry_run", "action": "stop_instance", "instance": inst_name, "zone": zone}))
                else:
                    try:
                        client.stop(project=project_id, zone=zone, instance=inst_name)
                        log.info(json.dumps({"event": "action", "action": "stop_instance", "instance": inst_name, "zone": zone, "status": "done"}))
                    except Exception as exc:
                        log.error(json.dumps({"event": "action_error", "action": "stop_instance", "instance": inst_name, "error": str(exc)}))
                actions.append(f"{zone}/{inst_name}")
    return actions


# ---------------------------------------------------------------------------
# Action: revoke API keys
# ---------------------------------------------------------------------------

def revoke_api_keys(project_id: str) -> list[str]:
    """Delete all active API keys in a project. Recoverable via UndeleteKey for 30 days."""
    if not ENABLE_API_KEY_REVOKE:
        return []
    from google.cloud import api_keys_v2

    actions = []
    client = api_keys_v2.ApiKeysClient()
    parent = f"projects/{project_id}/locations/global"
    try:
        keys = client.list_keys(parent=parent)
    except Exception as exc:
        log.warning(json.dumps({"event": "list_keys_error", "project": project_id, "error": str(exc)}))
        return []

    for key in keys:
        key_name = key.name  # projects/{proj}/locations/global/keys/{id}
        if getattr(key, "delete_time", None):
            continue  # already soft-deleted
        if DRY_RUN:
            log.info(json.dumps({"event": "dry_run", "action": "revoke_api_key", "key": key_name}))
        else:
            try:
                client.delete_key(name=key_name)
                log.info(json.dumps({"event": "action", "action": "revoke_api_key", "key": key_name, "status": "done"}))
            except Exception as exc:
                log.error(json.dumps({"event": "action_error", "action": "revoke_api_key", "key": key_name, "error": str(exc)}))
        actions.append(key_name)
    return actions


# ---------------------------------------------------------------------------
# Action: scale down GKE node pools
# ---------------------------------------------------------------------------

def scale_down_gke_clusters(project_id: str, zones_or_regions: list[str] | None = None) -> list[str]:
    """Resize all GKE node pools in a project to 0 nodes."""
    if not ENABLE_GKE_SCALE_DOWN:
        return []
    from google.cloud import container_v1

    actions = []
    client = container_v1.ClusterManagerClient()
    parent = f"projects/{project_id}/locations/-"  # "-" = all locations
    try:
        resp = client.list_clusters(parent=parent)
        clusters = resp.clusters
    except Exception as exc:
        log.warning(json.dumps({"event": "list_clusters_error", "project": project_id, "error": str(exc)}))
        return []

    for cluster in clusters:
        for pool in cluster.node_pools:
            if pool.initial_node_count == 0 and not any(
                ig for ig in getattr(pool, "instance_group_urls", [])
            ):
                continue
            pool_id = f"{cluster.name}/{pool.name}"
            if DRY_RUN:
                log.info(json.dumps({"event": "dry_run", "action": "scale_down_gke", "pool": pool_id}))
            else:
                try:
                    pool_name = (
                        f"projects/{project_id}/locations/{cluster.location}"
                        f"/clusters/{cluster.name}/nodePools/{pool.name}"
                    )
                    client.set_node_pool_size(name=pool_name, node_count=0)
                    log.info(json.dumps({"event": "action", "action": "scale_down_gke", "pool": pool_id, "status": "done"}))
                except Exception as exc:
                    log.error(json.dumps({"event": "action_error", "action": "scale_down_gke", "pool": pool_id, "error": str(exc)}))
            actions.append(pool_id)
    return actions


# ---------------------------------------------------------------------------
# Action: disable billing (nuclear option)
# ---------------------------------------------------------------------------

def disable_billing(project_id: str) -> bool:
    """Disable billing on a project. Requires ENABLE_BILLING_SHUTOFF=true."""
    if not ENABLE_BILLING_SHUTOFF:
        log.warning(json.dumps({"event": "skip", "action": "disable_billing", "reason": "not_enabled"}))
        return False

    if DRY_RUN:
        log.info(json.dumps({"event": "dry_run", "action": "disable_billing", "project": project_id}))
        return True

    client = billing_v1.CloudBillingClient()
    try:
        # Get the billing account name for this project
        proj_billing = client.get_project_billing_info(name=f"projects/{project_id}")
        if not proj_billing.billing_account_name:
            log.info(json.dumps({"event": "skip", "action": "disable_billing", "project": project_id, "reason": "no_billing_account"}))
            return False

        # Disabling billing = unlink the billing account
        updated = billing_v1.ProjectBillingInfo()
        updated.name = f"projects/{project_id}"
        updated.billing_account_name = ""  # empty = disable
        client.update_project_billing_info(
            name=f"projects/{project_id}",
            project_billing_info=updated,
        )
        log.info(json.dumps({"event": "action", "action": "disable_billing", "project": project_id, "status": "done"}))
        return True
    except Exception as exc:
        log.error(json.dumps({"event": "action_error", "action": "disable_billing", "project": project_id, "error": str(exc)}))
        return False


# ---------------------------------------------------------------------------
# Threshold evaluation
# ---------------------------------------------------------------------------

def should_take_action(alert: BudgetAlert) -> bool:
    """Decide whether the alert warrants action."""
    # Take action on actual spend at >= 100% or forecast at >= 90%
    if alert.is_actual and alert.threshold_percent >= 100:
        return True
    if alert.is_forecast and alert.threshold_percent >= 90:
        return True
    # Also act on any actual alert >= 50% if forecasted spend exceeds budget
    if alert.is_actual and alert.threshold_percent >= 50 and alert.forecasted_spend > alert.budget_amount:
        return True
    return False


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


@app.route("/poll", methods=["POST"])
def handle_poll():
    """Invoked by Cloud Scheduler every few minutes — real-time quota-spike check.

    Complements the budget-alert path with a fast, cost-independent signal.
    Also checks intent/actual overruns from the reporting protocol.
    See poller.py for detection rules.
    """
    import poller

    trips = poller.poll_all_accounts(execute_killswitch)
    return (
        json.dumps({"checked": True, "trips": trips, "dry_run": DRY_RUN}),
        200,
        {"Content-Type": "application/json"},
    )


@app.route("/poll-intents", methods=["POST"])
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
        is_self = SELF_PROJECT_ID and intent_obj.project_id == SELF_PROJECT_ID

        overrun = intent_mod.check_intent_overrun(intent_obj)
        if overrun:
            if is_self:
                log.critical(json.dumps({
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
                    log.critical(json.dumps({
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
        json.dumps({"checked": True, "overruns": checks, "dry_run": DRY_RUN}),
        200,
        {"Content-Type": "application/json"},
    )


@app.route("/reconcile", methods=["POST"])
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
        json.dumps({"reconciled": True, "results": results, "dry_run": DRY_RUN}),
        200,
        {"Content-Type": "application/json"},
    )


@app.route("/", methods=["GET"])
def info():
    return (
        json.dumps({
            "service": "cloudmanagement",
            "dry_run": DRY_RUN,
            "self_project_id": SELF_PROJECT_ID,
            "allowlist": sorted(ALLOWLIST),
            "billing_shutoff": ENABLE_BILLING_SHUTOFF,
            "run_pause": ENABLE_RUN_PAUSE,
            "trigger_disable": ENABLE_TRIGGER_DISABLE,
            "stop_compute": STOP_COMPUTE_INSTANCES,
            "api_key_revoke": ENABLE_API_KEY_REVOKE,
            "gke_scale_down": ENABLE_GKE_SCALE_DOWN,
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
