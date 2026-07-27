###############################################################################
# Locals
###############################################################################

locals {
  use_org    = var.org_id != ""
  use_folder = !local.use_org && var.folder_id != ""

  # Roles granted once at the org/folder level so they're inherited into
  # every monitored teammate project — no per-project IAM setup required.
  org_folder_roles = concat(
    ["roles/run.admin", "roles/monitoring.viewer"],
    var.enable_run_pause == "true" ? ["roles/cloudscheduler.admin"] : [],
    var.stop_compute_instances == "true" ? ["roles/compute.instanceAdmin.v1"] : [],
    var.enable_trigger_disable == "true" ? ["roles/cloudbuild.builds.editor"] : [],
    var.enable_api_key_revoke == "true" ? ["roles/serviceusage.apiKeysAdmin"] : [],
    var.enable_gke_scale_down == "true" ? ["roles/container.admin"] : [],
  )

  # Billing-account-level grants are separate from org/folder IAM (org policy
  # doesn't reach billing accounts), so collect every distinct billing
  # account across the hub project and all monitored projects.
  billing_accounts = toset(concat(
    [var.billing_account_id],
    [for p in var.monitored_projects : p.billing_account_id]
  ))

  # One Cloud Billing budget per monitored project.
  # The hub project (billing_project_id) MAY be included here to give it a
  # kill-switch budget routed to the Pub/Sub topic. The code-level guard in
  # execute_killswitch() (SELF_PROJECT_ID hard-block, main.py lines 508-516)
  # prevents the kill switch from actually firing on the hub, so including it
  # only surfaces hub overspend in the dashboard/logs. The hub also gets a
  # separate email-only self_budget below as an early-warning human channel.
  budget_targets = {
    for p in var.monitored_projects : p.project_id => {
      project_id         = p.project_id
      billing_account_id = p.billing_account_id
      budget_amount_usd  = p.budget_amount_usd
      currency_code      = p.currency_code
    }
  }
}

###############################################################################
# BigQuery dataset for Cloud Billing export (hub project)
###############################################################################

resource "google_bigquery_dataset" "billing_export" {
  dataset_id  = "cloud_billing_export"
  project     = var.billing_project_id
  location    = var.bigquery_dataset_location
  description = "Destination for Cloud Billing detailed usage cost export (one table per billing account). Read by providers/gcp.py fetch_billed_costs for reconciliation."
}

###############################################################################
# Firestore account registry (hub project)
###############################################################################

resource "google_firestore_database" "registry" {
  project     = var.billing_project_id
  name        = "(default)"
  location_id = var.firestore_location
  type        = "FIRESTORE_NATIVE"
}

###############################################################################
# Seed the account registry with monitored_projects (registry.py reads these)
###############################################################################

resource "google_firestore_document" "account_registry_seed" {
  for_each    = { for p in var.monitored_projects : p.project_id => p }
  project     = var.billing_project_id
  database    = google_firestore_database.registry.name
  collection  = "accounts"
  document_id = each.value.project_id

  fields = jsonencode({
    project_id          = { stringValue = each.value.project_id }
    billing_account_id  = { stringValue = each.value.billing_account_id }
    owner_email         = { stringValue = each.value.owner_email }
    allowlist           = { booleanValue = each.value.allowlist }
    budget_amount_usd   = { doubleValue = each.value.budget_amount_usd }
    quota_rpm_cap       = { integerValue = tostring(each.value.quota_rpm_cap) }
  })
}

###############################################################################
# Pub/Sub topic for budget alerts (shared by every monitored project)
###############################################################################

resource "google_pubsub_topic" "budget_alerts" {
  name    = "budget-alerts"
  project = var.billing_project_id
}

###############################################################################
# Service account for the Cloud Run service (runtime) — the one identity
# used across every monitored project via org/folder-level IAM
###############################################################################

resource "google_service_account" "killswitch_runtime" {
  account_id   = "killswitch-rt"
  display_name = "Cost Kill Switch Runtime SA"
  project      = var.billing_project_id
}

###############################################################################
# IAM — org/folder-level roles, inherited into every monitored project
###############################################################################

resource "google_organization_iam_member" "killswitch_org_roles" {
  for_each = local.use_org ? toset(local.org_folder_roles) : toset([])
  org_id   = var.org_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.killswitch_runtime.email}"
}

resource "google_folder_iam_member" "killswitch_folder_roles" {
  for_each = local.use_folder ? toset(local.org_folder_roles) : toset([])
  folder   = "folders/${var.folder_id}"
  role     = each.value
  member   = "serviceAccount:${google_service_account.killswitch_runtime.email}"
}

###############################################################################
# IAM — hub-project-only roles (Pub/Sub, BigQuery, Firestore live here only)
###############################################################################

resource "google_project_iam_member" "pubsub_subscriber" {
  project = var.billing_project_id
  role    = "roles/pubsub.subscriber"
  member  = "serviceAccount:${google_service_account.killswitch_runtime.email}"
}

resource "google_project_iam_member" "bq_reader" {
  project = var.billing_project_id
  role    = "roles/bigquery.dataViewer"
  member  = "serviceAccount:${google_service_account.killswitch_runtime.email}"
}

resource "google_project_iam_member" "bq_job_user" {
  project = var.billing_project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.killswitch_runtime.email}"
}

resource "google_project_iam_member" "firestore_user" {
  project = var.billing_project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.killswitch_runtime.email}"
}

###############################################################################
# IAM — billing account level (conditional, nuclear option)
###############################################################################

resource "google_billing_account_iam_member" "billing_user" {
  for_each           = var.enable_billing_shutoff == "true" ? local.billing_accounts : toset([])
  billing_account_id = each.value
  role                = "roles/billing.projectManager"
  member              = "serviceAccount:${google_service_account.killswitch_runtime.email}"
}

###############################################################################
# Allow Pub/Sub push and Cloud Scheduler (OIDC) to invoke the Cloud Run service
###############################################################################

resource "google_cloud_run_service_iam_member" "invoker" {
  project  = var.billing_project_id
  location = var.region
  service  = google_cloud_run_service.killswitch.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.killswitch_runtime.email}"
}

###############################################################################
# Cloud Run service
###############################################################################

resource "google_cloud_run_service" "killswitch" {
  name     = var.service_name
  project  = var.billing_project_id
  location = var.region

  template {
    spec {
      service_account_name = google_service_account.killswitch_runtime.email
      containers {
        image = var.image != "" ? var.image : "${var.region}-docker.pkg.dev/${var.billing_project_id}/cloud-management/killswitch:latest"
        env {
          name  = "DRY_RUN"
          value = var.dry_run
        }
        env {
          name  = "ALLOWLIST"
          value = var.allowlist
        }
        env {
          name  = "ENABLE_BILLING_SHUTOFF"
          value = var.enable_billing_shutoff
        }
        env {
          name  = "ENABLE_RUN_PAUSE"
          value = var.enable_run_pause
        }
        env {
          name  = "ENABLE_TRIGGER_DISABLE"
          value = var.enable_trigger_disable
        }
        env {
          name  = "STOP_COMPUTE_INSTANCES"
          value = var.stop_compute_instances
        }
        env {
          name  = "ENABLE_API_KEY_REVOKE"
          value = var.enable_api_key_revoke
        }
        env {
          name  = "ENABLE_GKE_SCALE_DOWN"
          value = var.enable_gke_scale_down
        }
        env {
          name  = "PROJECT_ID"
          value = var.billing_project_id
        }
        env {
          name  = "ALERT_TOPIC"
          value = google_pubsub_topic.budget_alerts.name
        }
        env {
          name  = "BQ_BILLING_TABLE"
          value = "${var.billing_project_id}.cloud_billing_export.gcp_billing_export_resource_v1_${replace(var.billing_account_id, "-", "_")}"
        }
        env {
          name  = "BUDGET_AMOUNT_USD"
          value = tostring(var.budget_amount_usd)
        }
        env {
          name  = "USE_FIRESTORE"
          value = "true"
        }
        env {
          name  = "FIRESTORE_PROJECT"
          value = var.billing_project_id
        }
        env {
          name  = "SELF_PROJECT_ID"
          value = var.billing_project_id
        }
      }
      # Scale to zero — no always-on cost
      # Max 1 instance — prevents self-scaling cost feedback loop
      container_concurrency = 1
    }
    metadata {
      annotations = {
        "autoscaling.min-scale" = "0"
        "autoscaling.max-scale" = "1"
      }
    }
  }

  traffic {
    percent         = 100
    latest_revision = true
  }

  depends_on = [google_pubsub_topic.budget_alerts]
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

###############################################################################
# Budget alerts → Pub/Sub, one per monitored project
# (hub included if listed in monitored_projects — see comment in locals above)
###############################################################################

resource "google_billing_budget" "monthly" {
  for_each = local.budget_targets

  billing_account = each.value.billing_account_id
  display_name    = "Cost Kill Switch Budget — ${each.value.project_id}"

  budget_filter {
    projects = ["projects/${each.value.project_id}"]
  }

  amount {
    specified_amount {
      currency_code = each.value.currency_code
      units         = each.value.budget_amount_usd
    }
  }

  threshold_rules {
    threshold_percent = 0.5
    spend_basis       = "CURRENT_SPEND"
  }

  threshold_rules {
    threshold_percent = 0.9
    spend_basis       = "FORECASTED_SPEND"
  }

  threshold_rules {
    threshold_percent = 1.0
    spend_basis       = "CURRENT_SPEND"
  }

  threshold_rules {
    threshold_percent = 1.5
    spend_basis       = "CURRENT_SPEND"
  }

  all_updates_rule {
    pubsub_topic                    = google_pubsub_topic.budget_alerts.id
    schema_version                  = "1.0"
    enable_project_level_recipients = true
  }
}

###############################################################################
# Self-budget — email-only alert for the hub project (CloudManagement itself).
# Does NOT publish to the kill switch Pub/Sub topic.  This prevents the
# feedback loop where CloudManagement kills its own infrastructure and then
# can't monitor other projects.  A human gets emailed instead.
###############################################################################

# Email notification channel for self-budget alerts
resource "google_monitoring_notification_channel" "self_budget_email" {
  project      = var.billing_project_id
  display_name = "CloudManagement Self-Budget Alert"
  type         = "email"
  labels = {
    email_address = var.self_alert_email
  }
}

resource "google_billing_budget" "self" {
  billing_account = var.billing_account_id
  display_name    = "CloudManagement Self-Monitor — ${var.billing_project_id}"

  budget_filter {
    projects = ["projects/${var.billing_project_id}"]
  }

  amount {
    specified_amount {
      currency_code = "USD"
      units         = var.self_budget_amount_usd
    }
  }

  threshold_rules {
    threshold_percent = 0.5
    spend_basis       = "CURRENT_SPEND"
  }

  threshold_rules {
    threshold_percent = 1.0
    spend_basis       = "CURRENT_SPEND"
  }

  all_updates_rule {
    monitoring_notification_channels   = [google_monitoring_notification_channel.self_budget_email.id]
    schema_version                     = "1.0"
    enable_project_level_recipients    = true
  }
}

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
