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

Maintenance-mode snapshot (upstream convergence): the canonical
implementation of this module is maintained upstream in
``biofool/CloudManagement``. This copy is a frozen snapshot kept so
this repo stays standalone-runnable — it receives security fixes and
upstream backports only. See docs/convergence.md.
"""

from __future__ import annotations

import logging
import os
import sys

import flask
from google.cloud import run_v2, scheduler_v1, compute_v1, billing_v1  # noqa: F401 (re-exported: `main.billing_v1` etc. are patched directly in tests, and killswitch_actions.py imports the same cached modules)

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
# Kill switch orchestration + alert pipeline — implemented in
# alert_pipeline.py (split during the ≤250-line refactor; it reads this
# module's config via `import main` qualified access so test-mutated
# flags keep working). Re-exported here so `main.execute_killswitch`,
# `main.is_project_protected`, `main.is_self_project`, and
# `main.process_alert` keep working exactly as before.
# ---------------------------------------------------------------------------

from alert_pipeline import (  # noqa: E402
    execute_killswitch,
    is_project_protected,
    is_self_project,
    process_alert,
)


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
