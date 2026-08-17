# outputs.tf
# Split out of the original monolithic main.tf — pure structural move,
# no behavior change. Terraform loads every *.tf file in a directory
# automatically, so resource references across these files work exactly
# as they did when everything lived in one file.

###############################################################################
# Outputs
###############################################################################

output "cloud_run_url" {
  value = google_cloud_run_service.killswitch.status[0].url
}

output "pubsub_topic" {
  value = google_pubsub_topic.budget_alerts.id
}

output "service_account_email" {
  value = google_service_account.killswitch_runtime.email
}

output "bigquery_dataset" {
  value = google_bigquery_dataset.billing_export.id
}

output "firestore_database" {
  value = google_firestore_database.registry.name
}
