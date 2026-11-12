"""
Dashboard blueprint for the GCP Cost Kill Switch.

Serves a lightweight web UI and JSON API endpoints that query BigQuery
billing export data. Designed for Cloud Run scale-to-zero: no always-on
polling, queries only on page load.

Environment variables:
  BQ_BILLING_TABLE   Full BigQuery table path for billing export
                     (e.g. "your-project.cloud_billing_export.gcp_billing_export_resource_v1_XXXXXX")
  BQ_DATASET         Dataset ID (default: "cloud_billing_export")
  BUDGET_AMOUNT_USD  Monthly budget for progress bar (default: 5)
  HUB_PROJECT_ID     Project hosting the shared billing export dataset, used to
                      build per-account table paths for the team view
                      (default: derived from BQ_BILLING_TABLE's project prefix)

Maintenance-mode snapshot (upstream convergence): the canonical
implementation of this module is maintained upstream in
``biofool/CloudManagement``. This copy is a frozen snapshot kept so
this repo stays standalone-runnable — it receives security fixes and
upstream backports only. See docs/convergence.md.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

import flask

import registry

log = logging.getLogger("killswitch.dashboard")

bp = flask.Blueprint("dashboard", __name__, template_folder="templates", static_folder="static")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BQ_BILLING_TABLE = os.environ.get("BQ_BILLING_TABLE", "")
# The table name is interpolated into BigQuery SQL (it cannot be a query
# parameter), so only accept a strict project.dataset.table identifier.
if BQ_BILLING_TABLE and not re.fullmatch(r"[A-Za-z0-9\-]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+", BQ_BILLING_TABLE):
    log.error(json.dumps({"event": "config_error", "setting": "BQ_BILLING_TABLE", "reason": "invalid table identifier"}))
    BQ_BILLING_TABLE = ""
BQ_DATASET = os.environ.get("BQ_DATASET", "cloud_billing_export")
BUDGET_AMOUNT_USD = float(os.environ.get("BUDGET_AMOUNT_USD", "5"))
HUB_PROJECT_ID = os.environ.get("HUB_PROJECT_ID") or (BQ_BILLING_TABLE.split(".")[0] if BQ_BILLING_TABLE else "")

# Cache results for 5 minutes to avoid repeated BigQuery scans
_cache: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 300


def _cached(key: str):
    """Decorator: cache endpoint results for _CACHE_TTL seconds."""
    def decorator(fn):
        def wrapper(*args, **kwargs):
            now = time.time()
            if key in _cache:
                ts, data = _cache[key]
                if now - ts < _CACHE_TTL:
                    return data
            result = fn(*args, **kwargs)
            _cache[key] = (now, result)
            return result
        wrapper.__name__ = fn.__name__
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# BigQuery client (lazy init)
# ---------------------------------------------------------------------------

_bq_client = None


def _get_bq_client():
    global _bq_client
    if _bq_client is None:
        from google.cloud import bigquery
        _bq_client = bigquery.Client()
    return _bq_client


def _query_bq(sql: str) -> list[dict]:
    """Run a BigQuery query and return rows as dicts. Returns [] on error."""
    if not BQ_BILLING_TABLE:
        return []
    try:
        client = _get_bq_client()
        job = client.query(sql)
        rows = job.result()
        return [dict(r) for r in rows]
    except Exception as exc:
        log.warning(json.dumps({"event": "bq_query_error", "error": str(exc)[:200]}))
        return []


# ---------------------------------------------------------------------------
# SQL builders — implemented in dashboard_sql.py (split during the
# ≤250-line refactor; it reads this module's config via `import dashboard`
# qualified access so patched config values keep working). Re-exported
# here so `dashboard._sql_*` / `dashboard._billing_table_for` call sites
# keep working exactly as before.
# ---------------------------------------------------------------------------

from dashboard_sql import (  # noqa: E402
    _billing_table_for,
    _sql_daily_spend,
    _sql_mtd_summary,
    _sql_team_mtd_by_project,
    _sql_top_projects,
    _sql_top_services,
    _sql_yesterday_vs_avg,
)


# ---------------------------------------------------------------------------
# API endpoints (JSON)
# ---------------------------------------------------------------------------

@bp.route("/api/summary")
@_cached("summary")
def api_summary():
    rows = _query_bq(_sql_mtd_summary())
    if rows:
        mtd = float(rows[0].get("mtd_cost", 0))
        currency = rows[0].get("currency", "USD")
        project_count = rows[0].get("project_count", 0)
    else:
        mtd = 0
        currency = "USD"
        project_count = 0

    pct = round(mtd / BUDGET_AMOUNT_USD * 100, 1) if BUDGET_AMOUNT_USD > 0 else 0
    remaining = round(BUDGET_AMOUNT_USD - mtd, 2)

    return flask.jsonify({
        "mtd_cost": mtd,
        "budget": BUDGET_AMOUNT_USD,
        "currency": currency,
        "pct_of_budget": pct,
        "remaining": remaining,
        "project_count": project_count,
        "budget_configured": BQ_BILLING_TABLE != "",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


@bp.route("/api/daily")
@_cached("daily")
def api_daily():
    rows = _query_bq(_sql_daily_spend())
    return flask.jsonify({
        "labels": [r["usage_date"].isoformat() if hasattr(r["usage_date"], "isoformat") else str(r["usage_date"]) for r in rows],
        "values": [float(r["daily_cost"]) for r in rows],
    })


@bp.route("/api/services")
@_cached("services")
def api_services():
    rows = _query_bq(_sql_top_services())
    return flask.jsonify({
        "labels": [r["service_name"] for r in rows],
        "values": [float(r["total_cost"]) for r in rows],
    })


@bp.route("/api/projects")
@_cached("projects")
def api_projects():
    rows = _query_bq(_sql_top_projects())
    return flask.jsonify({
        "projects": [
            {"project_id": r["project_id"], "total_cost": float(r["total_cost"])}
            for r in rows
        ],
    })


@bp.route("/api/spike")
@_cached("spike")
def api_spike():
    rows = _query_bq(_sql_yesterday_vs_avg())
    if rows:
        r = rows[0]
        return flask.jsonify({
            "yesterday_cost": float(r.get("yesterday_cost", 0)),
            "daily_avg_7d": float(r.get("daily_avg_7d", 0)),
            "pct_of_baseline": float(r.get("pct_of_baseline", 0)),
        })
    return flask.jsonify({"yesterday_cost": 0, "daily_avg_7d": 0, "pct_of_baseline": 0})


@bp.route("/api/accounts")
@_cached("accounts")
def api_accounts():
    """Team view: every registered account, with MTD spend if a billing
    export table can be resolved for its billing account."""
    accounts = registry.list_accounts()

    spend_by_project: dict[str, dict] = {}
    sql = _sql_team_mtd_by_project(accounts)
    if sql:
        for row in _query_bq(sql):
            spend_by_project[row["project_id"]] = row

    result = []
    for acct in accounts:
        spend = spend_by_project.get(acct.project_id, {})
        mtd_cost = float(spend.get("mtd_cost", 0))
        pct = round(mtd_cost / acct.budget_amount_usd * 100, 1) if acct.budget_amount_usd > 0 else 0
        result.append({
            "project_id": acct.project_id,
            "owner_email": acct.owner_email,
            "allowlist": acct.allowlist,
            "budget_amount_usd": acct.budget_amount_usd,
            "mtd_cost": mtd_cost,
            "currency": spend.get("currency", "USD"),
            "pct_of_budget": pct,
            "quota_rpm_cap": acct.quota_rpm_cap,
        })

    return flask.jsonify({
        "accounts": sorted(result, key=lambda a: a["pct_of_budget"], reverse=True),
        "registry_backend": "firestore" if registry.USE_FIRESTORE else registry.ACCOUNTS_FILE,
    })


# ---------------------------------------------------------------------------
# Dashboard page
# ---------------------------------------------------------------------------

@bp.route("/dashboard")
def dashboard():
    return flask.render_template(
        "dashboard.html",
        budget=BUDGET_AMOUNT_USD,
        bq_configured=BQ_BILLING_TABLE != "",
    )
