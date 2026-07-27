-- Recent spend spike detection: compare last 3 days vs prior 7-day average
WITH recent AS (
  SELECT
    project.id AS project_id,
    ROUND(SUM(cost), 2) AS recent_3d_cost
  FROM `your-project.cloud_billing_export.gcp_billing_export_resource_v1_XXXXXX`
  WHERE DATE(usage_start_time) >= DATE_SUB(CURRENT_DATE(), INTERVAL 3 DAY)
    AND DATE(usage_start_time) < CURRENT_DATE()
    AND cost > 0
  GROUP BY project_id
),
baseline AS (
  SELECT
    project.id AS project_id,
    ROUND(SUM(cost) / 7, 2) AS daily_avg_7d
  FROM `your-project.cloud_billing_export.gcp_billing_export_resource_v1_XXXXXX`
  WHERE DATE(usage_start_time) >= DATE_SUB(CURRENT_DATE(), INTERVAL 10 DAY)
    AND DATE(usage_start_time) < DATE_SUB(CURRENT_DATE(), INTERVAL 3 DAY)
    AND cost > 0
  GROUP BY project_id
)
SELECT
  r.project_id,
  r.recent_3d_cost,
  b.daily_avg_7d,
  ROUND(r.recent_3d_cost / 3, 2) AS recent_daily_avg,
  ROUND((r.recent_3d_cost / 3) / NULLIF(b.daily_avg_7d, 0) * 100, 1) AS pct_of_baseline
FROM recent r
JOIN baseline b USING (project_id)
WHERE r.recent_3d_cost / 3 > b.daily_avg_7d * 1.5
ORDER BY pct_of_baseline DESC;
