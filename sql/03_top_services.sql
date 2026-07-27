-- Top services by cost (current month)
SELECT
  service.id          AS service_id,
  service.description AS service_name,
  ROUND(SUM(cost), 2) AS total_cost,
  currency
FROM `your-project.cloud_billing_export.gcp_billing_export_resource_v1_XXXXXX`
WHERE invoice.month = FORMAT_DATE('%Y%m', CURRENT_DATE())
  AND cost > 0
GROUP BY service_id, service_name, currency
ORDER BY total_cost DESC
LIMIT 20;
