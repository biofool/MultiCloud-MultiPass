-- Month-to-date spend by project
SELECT
  project.id       AS project_id,
  ROUND(SUM(cost), 2) AS mtd_cost,
  currency
FROM `your-project.cloud_billing_export.gcp_billing_export_resource_v1_XXXXXX`
WHERE invoice.month = FORMAT_DATE('%Y%m', CURRENT_DATE())
  AND cost > 0
GROUP BY project_id, currency
ORDER BY mtd_cost DESC;
