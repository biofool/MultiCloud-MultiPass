# pubsub.tf — Pub/Sub topic + push subscription for budget alerts
# Split out of the original monolithic main.tf — pure structural move,
# no behavior change. Terraform loads every *.tf file in a directory
# automatically, so resource references across these files work exactly
# as they did when everything lived in one file.

###############################################################################
# Pub/Sub topic for budget alerts (shared by every monitored project)
###############################################################################

resource "google_pubsub_topic" "budget_alerts" {
  name    = "budget-alerts"
  project = var.billing_project_id
}


###############################################################################
# Pub/Sub push subscription → Cloud Run (budget-alert path)
###############################################################################

resource "google_pubsub_subscription" "killswitch_push" {
  name    = "killswitch-push"
  topic   = google_pubsub_topic.budget_alerts.name
  project = var.billing_project_id

  push_config {
    push_endpoint = "${google_cloud_run_service.killswitch.status[0].url}/"
    oidc_token {
      service_account_email = google_service_account.killswitch_runtime.email
    }
  }

  # Retry config — don't hammer the service
  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }

  # Drop messages after 1 day
  message_retention_duration = "86400s"
  ack_deadline_seconds       = 30
}

