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
"""

from __future__ import annotations

import json
import logging
import os
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
# SQL builders — all filter to current invoice month to minimise scanned bytes
# ---------------------------------------------------------------------------

def _sql_mtd_summary() -> str:
    return f"""
SELECT
  ROUND(SUM(cost), 2) AS mtd_cost,
  ANY_VALUE(currency) AS currency,
  COUNT(DISTINCT project.id) AS project_count
FROM `{BQ_BILLING_TABLE}`
WHERE invoice.month = FORMAT_DATE('%Y%m', CURRENT_DATE())
  AND cost > 0
"""


def _sql_daily_spend() -> str:
    return f"""
SELECT
  DATE(usage_start_time) AS usage_date,
  ROUND(SUM(cost), 2) AS daily_cost
FROM `{BQ_BILLING_TABLE}`
WHERE invoice.month = FORMAT_DATE('%Y%m', CURRENT_DATE())
  AND cost > 0
GROUP BY usage_date
ORDER BY usage_date ASC
"""


def _sql_top_services(limit: int = 10) -> str:
    return f"""
SELECT
  service.description AS service_name,
  ROUND(SUM(cost), 2) AS total_cost
FROM `{BQ_BILLING_TABLE}`
WHERE invoice.month = FORMAT_DATE('%Y%m', CURRENT_DATE())
  AND cost > 0
GROUP BY service_name
ORDER BY total_cost DESC
LIMIT {limit}
"""


def _sql_top_projects() -> str:
    return f"""
SELECT
  project.id AS project_id,
  ROUND(SUM(cost), 2) AS total_cost
FROM `{BQ_BILLING_TABLE}`
WHERE invoice.month = FORMAT_DATE('%Y%m', CURRENT_DATE())
  AND cost > 0
GROUP BY project_id
ORDER BY total_cost DESC
LIMIT 20
"""


def _sql_yesterday_vs_avg() -> str:
    return f"""
WITH recent AS (
  SELECT ROUND(SUM(cost), 2) AS yesterday_cost
  FROM `{BQ_BILLING_TABLE}`
  WHERE DATE(usage_start_time) = DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
    AND cost > 0
),
baseline AS (
  SELECT ROUND(SUM(cost) / 7, 2) AS daily_avg_7d
  FROM `{BQ_BILLING_TABLE}`
  WHERE DATE(usage_start_time) >= DATE_SUB(CURRENT_DATE(), INTERVAL 8 DAY)
    AND DATE(usage_start_time) < DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
    AND cost > 0
)
SELECT
  recent.yesterday_cost,
  baseline.daily_avg_7d,
  ROUND(recent.yesterday_cost / NULLIF(baseline.daily_avg_7d, 0) * 100, 1) AS pct_of_baseline
FROM recent, baseline
"""


def _billing_table_for(billing_account_id: str) -> str | None:
    """Build the export table path for a billing account, assuming its export
    lives in the hub project's shared dataset (same convention as BQ_BILLING_TABLE)."""
    if not HUB_PROJECT_ID or not billing_account_id:
        return None
    suffix = billing_account_id.replace("-", "_")
    return f"{HUB_PROJECT_ID}.{BQ_DATASET}.gcp_billing_export_resource_v1_{suffix}"


def _sql_team_mtd_by_project(accounts: list) -> str | None:
    """UNION ALL of per-billing-account MTD spend grouped by project, for
    every distinct billing account across the registered accounts."""
    by_table: dict[str, list[str]] = {}
    for acct in accounts:
        table = _billing_table_for(acct.billing_account_id)
        if table:
            by_table.setdefault(table, []).append(acct.project_id)

    if not by_table:
        return None

    parts = []
    for table, project_ids in by_table.items():
        ids = ", ".join(f"'{pid}'" for pid in project_ids)
        parts.append(f"""
SELECT
  project.id AS project_id,
  ROUND(SUM(cost), 2) AS mtd_cost,
  ANY_VALUE(currency) AS currency
FROM `{table}`
WHERE invoice.month = FORMAT_DATE('%Y%m', CURRENT_DATE())
  AND cost > 0
  AND project.id IN ({ids})
GROUP BY project_id
""")
    return "\nUNION ALL\n".join(parts)


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
