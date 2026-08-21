"""Intent/actual API usage reporting protocol.

This module implements the real-time tier of the multi-cloud billing
monitor.  Sub-projects declare expected API usage *before* making calls
and report actuals *after* (or incrementally during long jobs).
CloudManagement validates actual vs intent, detects overruns, and can kill
the specific job that is accumulating cost.

Endpoints (Flask blueprint, registered in main.py):
  POST /api/v1/intent                     — declare intent (pre-call)
  POST /api/v1/actual                     — report actual (post-call / incremental)
  GET  /api/v1/expected-costs/<project_id> — pull authoritative expected costs
  GET  /api/v1/intents                    — list active intents (dashboard)
  GET  /api/v1/intents/<project_id>       — list intents for a project
  POST /api/v1/kill/<intent_id>           — manual kill override (dashboard)
  GET  /api/v1/budget/<project_id>        — read-only budget status (issue #1)
  POST /api/v1/budget/<project_id>        — budget admission decision (issue #1)
  GET  /api/v1/intent/<intent_id>         — fetch single intent by ID (issue #1)
  GET  /api/v1/kill-orders                — client-polled kill orders (issue #1)
  POST /api/v1/exposure                   — report key exposure for rotation (issue #1)

Data stores (Firestore in production, YAML files in dev/test):
  api_intents     — one doc per intent declaration
  api_actuals     — one doc per actual report (supports incremental)
  expected_costs  — one doc per (project_id, provider) with authoritative pricing
  kill_events     — audit log of every kill invocation

Environment variables:
  USE_FIRESTORE              "true" | "false" (default: "false")
  FIRESTORE_PROJECT          Firestore host project (default: PROJECT_ID)
  CLOUDMANAGEMENT_REPORT_TOKEN  Global fallback report token (per-project tokens
                             take precedence via the registry's report_token_secret)
  INTENT_VARIANCE_THRESHOLD  Actual/expected ratio that triggers a kill (default: 1.2)
  RATE_CAP_BUFFER            Multiplier on declared rate_limit_rpm before tripping (default: 1.1)
"""

from __future__ import annotations

import logging
import os

from paths import resolve as _resolve_path

log = logging.getLogger("killswitch.intent")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

USE_FIRESTORE = os.environ.get("USE_FIRESTORE", "false").lower() == "true"
FIRESTORE_PROJECT = os.environ.get("FIRESTORE_PROJECT", os.environ.get("PROJECT_ID", ""))
GLOBAL_REPORT_TOKEN = os.environ.get("CLOUDMANAGEMENT_REPORT_TOKEN", "")
INTENT_VARIANCE_THRESHOLD = float(os.environ.get("INTENT_VARIANCE_THRESHOLD", "1.2"))
RATE_CAP_BUFFER = float(os.environ.get("RATE_CAP_BUFFER", "1.1"))

INTENTS_COLLECTION = "api_intents"
ACTUALS_COLLECTION = "api_actuals"
EXPECTED_COSTS_COLLECTION = "expected_costs"
KILL_EVENTS_COLLECTION = "kill_events"

# YAML fallback paths
_INTENTS_FILE = _resolve_path(os.environ.get("INTENTS_FILE", "config/api_intents.yaml"))
_ACTUALS_FILE = _resolve_path(os.environ.get("ACTUALS_FILE", "config/api_actuals.yaml"))
_EXPECTED_COSTS_FILE = _resolve_path(os.environ.get("EXPECTED_COSTS_FILE", "config/expected_costs.yaml"))
_KILL_EVENTS_FILE = _resolve_path(os.environ.get("KILL_EVENTS_FILE", "config/kill_events.yaml"))


# ---------------------------------------------------------------------------
# Store backend — Firestore or YAML
# ---------------------------------------------------------------------------

_fs_client = None

# ---------------------------------------------------------------------------
# Everything below is implemented in sibling modules (see each module's
# docstring) and re-exported here so `import intent; intent.X` keeps
# working exactly as before the split, and so importlib.reload(intent) —
# used by tests/test_intent.py's `yaml_backend` fixture to pick up
# fresh env-var-derived file paths — continues to refresh the config
# above in place without needing to reload every sibling module too
# (they read this module's config back through it at call time).
# ---------------------------------------------------------------------------

from intent_models import Intent, Actual, ExpectedCost  # noqa: E402
from intent_storage import (  # noqa: E402
    _gen_id,
    _get_firestore_client,
    _now_iso,
    get_intent,
    list_actuals,
    list_expected_costs,
    list_intents,
    list_kill_events,
    save_actual,
    save_expected_cost,
    save_intent,
    save_kill_event,
    sum_actuals_for_intent,
)
from intent_auth import _validate_token  # noqa: E402
from intent_detection import check_intent_overrun, check_project_budget  # noqa: E402
from intent_kill import kill_intent  # noqa: E402
from intent_routes import bp  # noqa: E402
