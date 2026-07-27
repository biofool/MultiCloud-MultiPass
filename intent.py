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

import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

import flask

import registry
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
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Intent:
    """A declared API usage intent from a sub-project."""
    intent_id: str
    project_id: str
    source_repo: str = ""
    job_id: str = ""
    job_name: str = ""
    provider: str = ""
    api: str = ""
    expected_calls: int = 0
    expected_cost_usd: float = 0.0
    expected_tokens: int | None = None
    rate_limit_rpm: int = 0
    window_start: str = ""
    window_end: str = ""
    kill: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    approved: bool = True
    status: str = "declared"  # declared | running | completed | failed | killed
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Intent":
        return cls(
            intent_id=d.get("intent_id", ""),
            project_id=d.get("project_id", ""),
            source_repo=d.get("source_repo", ""),
            job_id=d.get("job_id", ""),
            job_name=d.get("job_name", ""),
            provider=d.get("provider", ""),
            api=d.get("api", ""),
            expected_calls=int(d.get("expected_calls", 0)),
            expected_cost_usd=float(d.get("expected_cost_usd", 0)),
            expected_tokens=d.get("expected_tokens"),
            rate_limit_rpm=int(d.get("rate_limit_rpm", 0)),
            window_start=d.get("window_start", ""),
            window_end=d.get("window_end", ""),
            kill=d.get("kill", {}),
            metadata=d.get("metadata", {}),
            approved=bool(d.get("approved", True)),
            status=d.get("status", "declared"),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
        )


@dataclass
class Actual:
    """An actual API usage report (post-call or incremental)."""
    actual_id: str
    intent_id: str
    project_id: str
    job_id: str = ""
    provider: str = ""
    api: str = ""
    actual_calls: int = 0
    actual_cost_usd: float = 0.0
    actual_tokens: int | None = None
    status: str = "completed"  # running | completed | failed | killed
    started_at: str = ""
    ended_at: str = ""
    sequence: int = 0           # for incremental reports
    reconciled_cost_usd: float | None = None
    reconciled_at: str | None = None
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Actual":
        return cls(
            actual_id=d.get("actual_id", ""),
            intent_id=d.get("intent_id", ""),
            project_id=d.get("project_id", ""),
            job_id=d.get("job_id", ""),
            provider=d.get("provider", ""),
            api=d.get("api", ""),
            actual_calls=int(d.get("actual_calls", 0)),
            actual_cost_usd=float(d.get("actual_cost_usd", 0)),
            actual_tokens=d.get("actual_tokens"),
            status=d.get("status", "completed"),
            started_at=d.get("started_at", ""),
            ended_at=d.get("ended_at", ""),
            sequence=int(d.get("sequence", 0)),
            reconciled_cost_usd=d.get("reconciled_cost_usd"),
            reconciled_at=d.get("reconciled_at"),
            created_at=d.get("created_at", ""),
        )


@dataclass
class ExpectedCost:
    """Authoritative expected cost for a (project, provider) pair."""
    project_id: str
    provider: str
    unit_cost_usd: float = 0.0
    free_tier_remaining_calls: int | None = None
    free_tier_reset: str = ""
    expected_remaining_monthly_usd: float = 0.0
    calibration_delta: float = 0.0
    pricing: dict[str, Any] = field(default_factory=dict)  # e.g. {input_cost_per_million, output_cost_per_million}
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ExpectedCost":
        return cls(
            project_id=d.get("project_id", ""),
            provider=d.get("provider", ""),
            unit_cost_usd=float(d.get("unit_cost_usd", 0)),
            free_tier_remaining_calls=d.get("free_tier_remaining_calls"),
            free_tier_reset=d.get("free_tier_reset", ""),
            expected_remaining_monthly_usd=float(d.get("expected_remaining_monthly_usd", 0)),
            calibration_delta=float(d.get("calibration_delta", 0)),
            pricing=d.get("pricing", {}),
            updated_at=d.get("updated_at", ""),
        )


# ---------------------------------------------------------------------------
# Store backend — Firestore or YAML
# ---------------------------------------------------------------------------

_fs_client = None


def _get_firestore_client():
    global _fs_client
    if _fs_client is None:
        from google.cloud import firestore
        _fs_client = firestore.Client(project=FIRESTORE_PROJECT or None)
    return _fs_client


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gen_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


# --- Intent store ---

def save_intent(intent: Intent) -> None:
    if USE_FIRESTORE:
        client = _get_firestore_client()
        client.collection(INTENTS_COLLECTION).document(intent.intent_id).set(intent.to_dict())
    else:
        _yaml_save(_INTENTS_FILE, "intents", [i.to_dict() for i in _yaml_load_intents() if i.intent_id != intent.intent_id] + [intent.to_dict()])


def get_intent(intent_id: str) -> Intent | None:
    for intent in list_intents():
        if intent.intent_id == intent_id:
            return intent
    return None


def list_intents(project_id: str | None = None, status: str | None = None) -> list[Intent]:
    if USE_FIRESTORE:
        client = _get_firestore_client()
        # Query by only one field to avoid composite index requirements.
        # Filter the secondary field in memory.
        q = client.collection(INTENTS_COLLECTION)
        if project_id:
            q = q.where("project_id", "==", project_id)
        intents = [Intent.from_dict(doc.to_dict() or {}) for doc in q.stream()]
        if status:
            intents = [i for i in intents if i.status == status]
        return intents
    else:
        intents = _yaml_load_intents()
        if project_id:
            intents = [i for i in intents if i.project_id == project_id]
        if status:
            intents = [i for i in intents if i.status == status]
        return intents


def _yaml_load_intents() -> list[Intent]:
    import yaml
    if not os.path.exists(_INTENTS_FILE):
        return []
    with open(_INTENTS_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return [Intent.from_dict(d) for d in data.get("intents", [])]


def _yaml_load_actuals() -> list[Actual]:
    import yaml
    if not os.path.exists(_ACTUALS_FILE):
        return []
    with open(_ACTUALS_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return [Actual.from_dict(d) for d in data.get("actuals", [])]


def _yaml_load_expected_costs() -> list[ExpectedCost]:
    import yaml
    if not os.path.exists(_EXPECTED_COSTS_FILE):
        return []
    with open(_EXPECTED_COSTS_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return [ExpectedCost.from_dict(d) for d in data.get("expected_costs", [])]


def _yaml_save(path: str, key: str, items: list[dict]) -> None:
    import yaml
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump({key: items}, f, sort_keys=False)


# --- Actual store ---

def save_actual(actual: Actual) -> None:
    if USE_FIRESTORE:
        client = _get_firestore_client()
        client.collection(ACTUALS_COLLECTION).document(actual.actual_id).set(actual.to_dict())
    else:
        existing = [a.to_dict() for a in _yaml_load_actuals() if a.actual_id != actual.actual_id]
        _yaml_save(_ACTUALS_FILE, "actuals", existing + [actual.to_dict()])


def list_actuals(intent_id: str | None = None, project_id: str | None = None) -> list[Actual]:
    if USE_FIRESTORE:
        client = _get_firestore_client()
        # Query by a single field to avoid composite index requirements.
        # If both filters are needed, query by the more selective one and
        # filter the rest in memory.
        q = client.collection(ACTUALS_COLLECTION)
        if intent_id:
            q = q.where("intent_id", "==", intent_id)
        elif project_id:
            q = q.where("project_id", "==", project_id)
        actuals = [Actual.from_dict(doc.to_dict() or {}) for doc in q.stream()]
        # Filter in memory for the secondary field
        if project_id and intent_id:
            actuals = [a for a in actuals if a.project_id == project_id]
        # Sort by sequence to ensure latest is last
        actuals.sort(key=lambda a: a.sequence)
        return actuals
    else:
        actuals = _yaml_load_actuals()
        if intent_id:
            actuals = [a for a in actuals if a.intent_id == intent_id]
        if project_id:
            actuals = [a for a in actuals if a.project_id == project_id]
        actuals.sort(key=lambda a: a.sequence)
        return actuals


def sum_actuals_for_intent(intent_id: str) -> dict[str, Any]:
    """Return the latest actual report for an intent.

    Clients send cumulative totals (running total of calls/cost), not
    deltas.  Summing all reports would double-count.  The latest report
    by sequence number is the authoritative cumulative value.
    """
    actuals = list_actuals(intent_id=intent_id)
    if not actuals:
        return {
            "actual_calls": 0,
            "actual_cost_usd": 0.0,
            "actual_tokens": None,
            "status": "declared",
            "report_count": 0,
        }
    # list_actuals orders by sequence; take the latest
    latest = actuals[-1]
    return {
        "actual_calls": latest.actual_calls,
        "actual_cost_usd": latest.actual_cost_usd,
        "actual_tokens": latest.actual_tokens,
        "status": latest.status,
        "report_count": len(actuals),
    }


# --- Expected cost store ---

def save_expected_cost(ec: ExpectedCost) -> None:
    ec.updated_at = _now_iso()
    if USE_FIRESTORE:
        client = _get_firestore_client()
        doc_id = f"{ec.project_id}__{ec.provider}"
        client.collection(EXPECTED_COSTS_COLLECTION).document(doc_id).set(ec.to_dict())
    else:
        existing = [e.to_dict() for e in _yaml_load_expected_costs() if not (e.project_id == ec.project_id and e.provider == ec.provider)]
        _yaml_save(_EXPECTED_COSTS_FILE, "expected_costs", existing + [ec.to_dict()])


def list_expected_costs(project_id: str | None = None) -> list[ExpectedCost]:
    if USE_FIRESTORE:
        client = _get_firestore_client()
        q = client.collection(EXPECTED_COSTS_COLLECTION)
        if project_id:
            q = q.where("project_id", "==", project_id)
        return [ExpectedCost.from_dict(doc.to_dict() or {}) for doc in q.stream()]
    else:
        costs = _yaml_load_expected_costs()
        if project_id:
            costs = [c for c in costs if c.project_id == project_id]
        return costs


# --- Kill event store ---

def save_kill_event(event: dict[str, Any]) -> None:
    if USE_FIRESTORE:
        client = _get_firestore_client()
        client.collection(KILL_EVENTS_COLLECTION).document(event.get("kill_id", _gen_id("kill"))).set(event)
    else:
        import yaml
        events = []
        if os.path.exists(_KILL_EVENTS_FILE):
            with open(_KILL_EVENTS_FILE, "r", encoding="utf-8") as f:
                events = (yaml.safe_load(f) or {}).get("kill_events", [])
        events.append(event)
        _yaml_save(_KILL_EVENTS_FILE, "kill_events", events)


def list_kill_events(project_id: str | None = None, limit: int = 50) -> list[dict]:
    if USE_FIRESTORE:
        client = _get_firestore_client()
        # Fetch without order_by+where (avoids composite index requirement),
        # then filter and sort in memory.
        q = client.collection(KILL_EVENTS_COLLECTION).order_by("timestamp", direction="DESCENDING").limit(limit * 5)
        events = [doc.to_dict() or {} for doc in q.stream()]
        if project_id:
            events = [e for e in events if e.get("project_id") == project_id]
        return events[:limit]
    else:
        import yaml
        if not os.path.exists(_KILL_EVENTS_FILE):
            return []
        with open(_KILL_EVENTS_FILE, "r", encoding="utf-8") as f:
            events = (yaml.safe_load(f) or {}).get("kill_events", [])
        if project_id:
            events = [e for e in events if e.get("project_id") == project_id]
        return events[-limit:]


# ---------------------------------------------------------------------------
# Auth — per-project report token validation
# ---------------------------------------------------------------------------

def _validate_token(project_id: str) -> bool:
    """Validate the bearer token against the project's configured token.

    Per-project tokens take precedence (from the registry's
    report_token_secret); the global CLOUDMANAGEMENT_REPORT_TOKEN is a
    fallback for dev/test.
    """
    auth_header = flask.request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return False
    token = auth_header[7:]

    acct = registry.get_account(project_id)
    if acct and acct.report_token_secret:
        # In production this is a Secret Manager ref; in dev it's an env var name.
        # For now, look it up as an env var.
        expected = os.environ.get(acct.report_token_secret, "")
        if expected and secrets.compare_digest(token, expected):
            return True

    if GLOBAL_REPORT_TOKEN and secrets.compare_digest(token, GLOBAL_REPORT_TOKEN):
        return True

    return False


# ---------------------------------------------------------------------------
# Detection — evaluate intent vs actual for overrun
# ---------------------------------------------------------------------------

def check_intent_overrun(intent: Intent) -> dict[str, Any] | None:
    """Check if an intent's actuals have exceeded the variance threshold.

    Returns a dict describing the overrun if detected, otherwise None.
    """
    summed = sum_actuals_for_intent(intent.intent_id)

    # Rule 1: actual_exceeds_intent (calls)
    if intent.expected_calls > 0:
        call_ratio = summed["actual_calls"] / intent.expected_calls
        if call_ratio > INTENT_VARIANCE_THRESHOLD:
            return {
                "rule": "actual_exceeds_intent_calls",
                "intent_id": intent.intent_id,
                "project_id": intent.project_id,
                "job_id": intent.job_id,
                "expected": intent.expected_calls,
                "actual": summed["actual_calls"],
                "ratio": round(call_ratio, 2),
                "threshold": INTENT_VARIANCE_THRESHOLD,
            }

    # Rule 1b: actual_exceeds_intent (cost)
    if intent.expected_cost_usd > 0:
        cost_ratio = summed["actual_cost_usd"] / intent.expected_cost_usd
        if cost_ratio > INTENT_VARIANCE_THRESHOLD:
            return {
                "rule": "actual_exceeds_intent_cost",
                "intent_id": intent.intent_id,
                "project_id": intent.project_id,
                "job_id": intent.job_id,
                "expected_cost": intent.expected_cost_usd,
                "actual_cost": round(summed["actual_cost_usd"], 4),
                "ratio": round(cost_ratio, 2),
                "threshold": INTENT_VARIANCE_THRESHOLD,
            }

    return None


def check_project_budget(project_id: str) -> dict[str, Any] | None:
    """Check if a project's rolling spend has exceeded its budget.

    Sums the latest actual report per intent for the current calendar
    month.  Each intent's latest report is a cumulative total, so we
    take the latest per intent (not the sum of all reports) and sum
    across intents.
    """
    acct = registry.get_account(project_id)
    if not acct or acct.budget_amount_usd <= 0:
        return None

    # Get all actuals for the project, filter to current month
    now = datetime.now(timezone.utc)
    current_month_prefix = now.strftime("%Y-%m")
    actuals = list_actuals(project_id=project_id)
    month_actuals = [
        a for a in actuals
        if a.created_at and a.created_at.startswith(current_month_prefix)
    ]

    # Group by intent_id and take the latest (highest sequence) per intent
    by_intent: dict[str, Actual] = {}
    for a in month_actuals:
        existing = by_intent.get(a.intent_id)
        if existing is None or a.sequence > existing.sequence:
            by_intent[a.intent_id] = a

    total_spend = sum(a.actual_cost_usd for a in by_intent.values())

    if total_spend > acct.budget_amount_usd:
        return {
            "rule": "project_budget_exceeded",
            "project_id": project_id,
            "budget": acct.budget_amount_usd,
            "spend": round(total_spend, 4),
        }
    return None


# ---------------------------------------------------------------------------
# Kill execution — route to providers
# ---------------------------------------------------------------------------

def kill_intent(intent: Intent, reason: str, rule: str = "") -> dict[str, Any]:
    """Kill the job associated with an intent via its kill descriptor."""
    from providers import registry as provider_registry

    kill_desc = intent.kill or {}
    if not kill_desc:
        # Fall back to registry jobs config — match by job_id prefix
        acct = registry.get_account(intent.project_id)
        if acct and acct.jobs:
            for job_cfg in acct.jobs:
                prefix = job_cfg.get("job_id_prefix", "")
                if prefix and intent.job_id.startswith(prefix):
                    kill_desc = job_cfg.get("kill", {})
                    break

    if not kill_desc:
        log.warning(json.dumps({
            "event": "kill_no_descriptor",
            "intent_id": intent.intent_id,
            "project_id": intent.project_id,
        }))
        return {"killed": False, "reason": "no kill descriptor available"}

    kill_desc = {**kill_desc, "job_id": intent.job_id, "project_id": intent.project_id}
    result = provider_registry.kill_job(kill_desc, reason)

    # Record the kill event
    kill_event = {
        "kill_id": _gen_id("kill"),
        "intent_id": intent.intent_id,
        "project_id": intent.project_id,
        "job_id": intent.job_id,
        "reason": reason,
        "rule": rule,
        "kill_type": kill_desc.get("type", ""),
        "killed": result.killed,
        "detail": result.detail,
        "error": result.error,
        "timestamp": _now_iso(),
    }
    save_kill_event(kill_event)

    # Update intent status
    intent.status = "killed"
    intent.updated_at = _now_iso()
    save_intent(intent)

    log.warning(json.dumps({"event": "job_killed", **kill_event}))
    return kill_event


# ---------------------------------------------------------------------------
# Flask blueprint
# ---------------------------------------------------------------------------

bp = flask.Blueprint("intent", __name__)


@bp.route("/api/v1/intent", methods=["POST"])
def declare_intent():
    """Declare expected API usage before making calls."""
    data = flask.request.get_json(silent=True)
    if not data:
        return flask.jsonify({"error": "not JSON"}), 400

    project_id = data.get("project_id", "")
    if not project_id:
        return flask.jsonify({"error": "missing project_id"}), 400

    if not _validate_token(project_id):
        return flask.jsonify({"error": "unauthorized"}), 401

    intent_id = data.get("intent_id") or _gen_id("int")
    now = _now_iso()

    intent = Intent(
        intent_id=intent_id,
        project_id=project_id,
        source_repo=data.get("source_repo", ""),
        job_id=data.get("job_id", ""),
        job_name=data.get("job_name", ""),
        provider=data.get("provider", ""),
        api=data.get("api", ""),
        expected_calls=int(data.get("expected_calls", 0)),
        expected_cost_usd=float(data.get("expected_cost_usd", 0)),
        expected_tokens=data.get("expected_tokens"),
        rate_limit_rpm=int(data.get("rate_limit_rpm", 0)),
        window_start=data.get("window_start", ""),
        window_end=data.get("window_end", ""),
        kill=data.get("kill", {}),
        metadata=data.get("metadata", {}),
        approved=True,
        status="declared",
        created_at=now,
        updated_at=now,
    )

    # Check project budget — deny if already exceeded
    budget_check = check_project_budget(project_id)
    if budget_check:
        intent.approved = False
        intent.status = "denied"
        save_intent(intent)
        log.warning(json.dumps({"event": "intent_denied", "intent_id": intent_id, "reason": budget_check}))
        return flask.jsonify({
            "intent_id": intent_id,
            "approved": False,
            "reason": budget_check["rule"],
            "budget_remaining_usd": 0,
        }), 200

    save_intent(intent)
    log.info(json.dumps({"event": "intent_declared", "intent_id": intent_id, "project": project_id, "provider": intent.provider}))

    # Compute budget remaining
    acct = registry.get_account(project_id)
    actuals = list_actuals(project_id=project_id)
    spent = sum(a.actual_cost_usd for a in actuals)
    budget_remaining = (acct.budget_amount_usd - spent) if acct else 0

    return flask.jsonify({
        "intent_id": intent_id,
        "approved": True,
        "budget_remaining_usd": round(budget_remaining, 2),
        "kill_switch_armed": True,
        "warnings": [],
    }), 200


@bp.route("/api/v1/actual", methods=["POST"])
def report_actual():
    """Report actual API usage (post-call or incremental)."""
    data = flask.request.get_json(silent=True)
    if not data:
        return flask.jsonify({"error": "not JSON"}), 400

    project_id = data.get("project_id", "")
    intent_id = data.get("intent_id", "")
    if not project_id or not intent_id:
        return flask.jsonify({"error": "missing project_id or intent_id"}), 400

    if not _validate_token(project_id):
        return flask.jsonify({"error": "unauthorized"}), 401

    intent = get_intent(intent_id)
    if intent is None:
        return flask.jsonify({"error": "intent not found", "intent_id": intent_id}), 404

    # Determine sequence number for incremental reports
    existing_actuals = list_actuals(intent_id=intent_id)
    seq = max((a.sequence for a in existing_actuals), default=-1) + 1

    actual = Actual(
        actual_id=_gen_id("act"),
        intent_id=intent_id,
        project_id=project_id,
        job_id=data.get("job_id", intent.job_id),
        provider=data.get("provider", intent.provider),
        api=data.get("api", intent.api),
        actual_calls=int(data.get("actual_calls", 0)),
        actual_cost_usd=float(data.get("actual_cost_usd", 0)),
        actual_tokens=data.get("actual_tokens"),
        status=data.get("status", "completed"),
        started_at=data.get("started_at", ""),
        ended_at=data.get("ended_at", ""),
        sequence=seq,
        created_at=_now_iso(),
    )
    save_actual(actual)

    # Update intent status
    intent.status = actual.status
    intent.updated_at = _now_iso()
    save_intent(intent)

    log.info(json.dumps({
        "event": "actual_reported",
        "intent_id": intent_id,
        "project": project_id,
        "calls": actual.actual_calls,
        "cost": actual.actual_cost_usd,
        "status": actual.status,
        "sequence": seq,
    }))

    # Check for overrun — kill if exceeded (regardless of status;
    # a completed job that made 2x the expected calls still needs killing)
    overrun = check_intent_overrun(intent)
    if overrun:
        kill_result = kill_intent(intent, reason=overrun["rule"], rule=overrun["rule"])
        return flask.jsonify({
            "actual_id": actual.actual_id,
            "overrun_detected": True,
            "overrun": overrun,
            "kill_result": kill_result,
        }), 200

    # Check project budget
    budget_check = check_project_budget(project_id)
    if budget_check:
        kill_result = kill_intent(intent, reason=budget_check["rule"], rule=budget_check["rule"])
        return flask.jsonify({
            "actual_id": actual.actual_id,
            "budget_exceeded": True,
            "budget_check": budget_check,
            "kill_result": kill_result,
        }), 200

    return flask.jsonify({
        "actual_id": actual.actual_id,
        "overrun_detected": False,
        "status": actual.status,
    }), 200


@bp.route("/api/v1/expected-costs/<project_id>", methods=["GET"])
def get_expected_costs(project_id: str):
    """Pull authoritative expected costs for a project."""
    costs = list_expected_costs(project_id=project_id)
    return flask.jsonify({
        "project_id": project_id,
        "updated_at": costs[0].updated_at if costs else "",
        "providers": {
            c.provider: {
                "unit_cost_usd": c.unit_cost_usd,
                "free_tier_remaining_calls": c.free_tier_remaining_calls,
                "free_tier_reset": c.free_tier_reset,
                "expected_remaining_monthly_usd": c.expected_remaining_monthly_usd,
                "calibration_delta": c.calibration_delta,
                "pricing": c.pricing,
            }
            for c in costs
        },
    }), 200


@bp.route("/api/v1/intents", methods=["GET"])
def list_all_intents():
    """List active intents (for dashboard). Optional ?project_id= and ?status= filters."""
    project_id = flask.request.args.get("project_id")
    status = flask.request.args.get("status")
    intents = list_intents(project_id=project_id, status=status)
    return flask.jsonify({
        "intents": [i.to_dict() for i in intents],
        "count": len(intents),
    }), 200


@bp.route("/api/v1/intents/<project_id>", methods=["GET"])
def list_project_intents(project_id: str):
    """List intents for a specific project."""
    intents = list_intents(project_id=project_id)
    result = []
    for intent in intents:
        summed = sum_actuals_for_intent(intent.intent_id)
        result.append({
            **intent.to_dict(),
            "actual_calls": summed["actual_calls"],
            "actual_cost_usd": round(summed["actual_cost_usd"], 4),
            "actual_tokens": summed["actual_tokens"],
            "variance_pct": round(
                (summed["actual_calls"] / intent.expected_calls * 100) if intent.expected_calls else 0, 1
            ),
        })
    return flask.jsonify({"intents": result, "count": len(result)}), 200


@bp.route("/api/v1/kill/<intent_id>", methods=["POST"])
def manual_kill(intent_id: str):
    """Manual kill override (dashboard button).

    Requires the same bearer token auth as declare/report endpoints —
    the token must match the intent's project_id.
    """
    intent = get_intent(intent_id)
    if intent is None:
        return flask.jsonify({"error": "intent not found"}), 404

    if not _validate_token(intent.project_id):
        return flask.jsonify({"error": "unauthorized"}), 401

    result = kill_intent(intent, reason="manual_override", rule="manual")
    return flask.jsonify(result), 200
