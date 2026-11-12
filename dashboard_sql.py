"""SQL builders for the dashboard's BigQuery billing-export queries.

Split out of ``dashboard.py`` during the ≤250-line refactor — pure
structural move, no behavior change. All queries filter to the current
invoice month to minimise scanned bytes. Reads ``BQ_BILLING_TABLE``,
``BQ_DATASET``, and ``HUB_PROJECT_ID`` via ``import dashboard`` qualified
access so test patching (``patch("dashboard.BQ_BILLING_TABLE", ...)``)
keeps working exactly as when these functions lived in dashboard.py.

Maintenance-mode snapshot (upstream convergence): the canonical
implementation of this module is maintained upstream in
``biofool/CloudManagement``. This copy is a frozen snapshot kept so
this repo stays standalone-runnable — it receives security fixes and
upstream backports only. See docs/convergence.md.
"""

from __future__ import annotations

import dashboard


def _sql_mtd_summary() -> str:
    return f"""
SELECT
  ROUND(SUM(cost), 2) AS mtd_cost,
  ANY_VALUE(currency) AS currency,
  COUNT(DISTINCT project.id) AS project_count
FROM `{dashboard.BQ_BILLING_TABLE}`
WHERE invoice.month = FORMAT_DATE('%Y%m', CURRENT_DATE())
  AND cost > 0
"""


def _sql_daily_spend() -> str:
    return f"""
SELECT
  DATE(usage_start_time) AS usage_date,
  ROUND(SUM(cost), 2) AS daily_cost
FROM `{dashboard.BQ_BILLING_TABLE}`
WHERE invoice.month = FORMAT_DATE('%Y%m', CURRENT_DATE())
  AND cost > 0
GROUP BY usage_date
ORDER BY usage_date ASC
"""


def _sql_top_services(limit: int = 10) -> str:
    # LIMIT cannot be a query parameter — clamp to a safe int before
    # interpolating into the SQL string.
    limit = max(1, min(int(limit), 100))
    return f"""
SELECT
  service.description AS service_name,
  ROUND(SUM(cost), 2) AS total_cost
FROM `{dashboard.BQ_BILLING_TABLE}`
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
FROM `{dashboard.BQ_BILLING_TABLE}`
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
  FROM `{dashboard.BQ_BILLING_TABLE}`
  WHERE DATE(usage_start_time) = DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
    AND cost > 0
),
baseline AS (
  SELECT ROUND(SUM(cost) / 7, 2) AS daily_avg_7d
  FROM `{dashboard.BQ_BILLING_TABLE}`
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
    if not dashboard.HUB_PROJECT_ID or not billing_account_id:
        return None
    suffix = billing_account_id.replace("-", "_")
    return f"{dashboard.HUB_PROJECT_ID}.{dashboard.BQ_DATASET}.gcp_billing_export_resource_v1_{suffix}"


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
