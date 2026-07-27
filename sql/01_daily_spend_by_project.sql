-- Daily spend by project (current billing month only)
-- Minimises scanned bytes: filters on partition column, no SELECT *
SELECT
  project.id       AS project_id,
  DATE(usage_start_time) AS usage_date,
  ROUND(SUM(cost), 2)    AS daily_cost,
  currency
FROM `your-project.cloud_billing_export.gcp_billing_export_resource_v1_XXXXXX`
WHERE invoice.month = FORMAT_DATE('%Y%m', CURRENT_DATE())
  AND cost > 0
GROUP BY project_id, usage_date, currency
ORDER BY usage_date DESC, daily_cost DESC;
