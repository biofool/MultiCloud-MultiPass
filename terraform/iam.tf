# iam.tf — runtime service account and all IAM bindings
# Split out of the original monolithic main.tf — pure structural move,
# no behavior change. Terraform loads every *.tf file in a directory
# automatically, so resource references across these files work exactly
# as they did when everything lived in one file.

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

