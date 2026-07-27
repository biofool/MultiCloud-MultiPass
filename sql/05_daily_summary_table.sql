-- Daily summary table — aggregate billing data into a small table
-- Run once/day via scheduled query or Cloud Scheduler + bq query
-- Keeps query costs near zero: downstream queries hit this small table
CREATE OR REPLACE TABLE `your-project.cloud_billing_export.daily_summary` AS
SELECT
  invoice.month       AS invoice_month,
  DATE(usage_start_time) AS usage_date,
  project.id          AS project_id,
  service.id          AS service_id,
  service.description AS service_name,
  ROUND(SUM(cost), 4) AS total_cost,
  ANY_VALUE(currency) AS currency
FROM `your-project.cloud_billing_export.gcp_billing_export_resource_v1_XXXXXX`
WHERE invoice.month = FORMAT_DATE('%Y%m', CURRENT_DATE())
  AND cost > 0
GROUP BY invoice_month, usage_date, project_id, service_id, service_name
ORDER BY usage_date DESC;
