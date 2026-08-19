# storage.tf — BigQuery billing export + Firestore account registry
# Split out of the original monolithic main.tf — pure structural move,
# no behavior change. Terraform loads every *.tf file in a directory
# automatically, so resource references across these files work exactly
# as they did when everything lived in one file.

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

