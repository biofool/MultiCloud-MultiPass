# cloud_run.tf — the Cloud Run kill-switch service
# Split out of the original monolithic main.tf — pure structural move,
# no behavior change. Terraform loads every *.tf file in a directory
# automatically, so resource references across these files work exactly
# as they did when everything lived in one file.

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

