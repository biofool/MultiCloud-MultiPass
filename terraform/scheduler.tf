# scheduler.tf — Cloud Scheduler jobs (/poll, /poll-intents, /reconcile)
# Split out of the original monolithic main.tf — pure structural move,
# no behavior change. Terraform loads every *.tf file in a directory
# automatically, so resource references across these files work exactly
# as they did when everything lived in one file.

###############################################################################
# Cloud Scheduler → Cloud Run /poll (real-time quota-spike path)
###############################################################################

resource "google_cloud_scheduler_job" "poll_trigger" {
  name     = "${var.service_name}-poll"
  project  = var.billing_project_id
  region   = var.region
  schedule = "*/5 * * * *"

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_service.killswitch.status[0].url}/poll"
    oidc_token {
      service_account_email = google_service_account.killswitch_runtime.email
    }
  }

  retry_config {
    retry_count = 1
  }
}

# Cloud Scheduler → Cloud Run /poll-intents (intent/actual overrun detection)
resource "google_cloud_scheduler_job" "poll_intents_trigger" {
  name     = "${var.service_name}-poll-intents"
  project  = var.billing_project_id
  region   = var.region
  schedule = "*/2 * * * *"

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_service.killswitch.status[0].url}/poll-intents"
    oidc_token {
      service_account_email = google_service_account.killswitch_runtime.email
    }
  }

  retry_config {
    retry_count = 1
  }
}

# Cloud Scheduler → Cloud Run /reconcile (daily billing reconciliation)
resource "google_cloud_scheduler_job" "reconcile_trigger" {
  name     = "${var.service_name}-reconcile"
  project  = var.billing_project_id
  region   = var.region
  schedule = "0 6 * * *"  # daily at 06:00 UTC

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_service.killswitch.status[0].url}/reconcile"
    oidc_token {
      service_account_email = google_service_account.killswitch_runtime.email
    }
  }

  retry_config {
    retry_count = 1
  }
}

