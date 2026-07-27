###############################################################################
# Variables
###############################################################################

variable "billing_project_id" {
  description = "Hub project: hosts the BigQuery billing dataset, Firestore account registry, and Cloud Run service. Monitored teammate projects are listed separately in `monitored_projects`."
  type        = string
}

variable "billing_account_id" {
  description = "Cloud Billing account ID for the hub project's own budget (e.g. 01AB-23CD-EF45). Teammate accounts are declared per-entry in `monitored_projects`."
  type        = string
}

variable "org_id" {
  description = "GCP Organization ID that all monitored projects live under. Exactly one of org_id / folder_id must be set — IAM is granted once here and inherited into every project below, so no per-project setup is needed."
  type        = string
  default     = ""
}

variable "folder_id" {
  description = "GCP Folder ID that all monitored projects live under. Exactly one of org_id / folder_id must be set."
  type        = string
  default     = ""
}

variable "monitored_projects" {
  description = "Teammate GCP projects to monitor. Each must live under org_id/folder_id. Feeds both the per-project Cloud Billing budgets and the account registry seed."
  type = list(object({
    project_id         = string
    billing_account_id = string
    owner_email        = string
    budget_amount_usd  = number
    currency_code      = optional(string, "USD")
    allowlist          = optional(bool, false)
    quota_rpm_cap      = optional(number, 0)
  }))
  default = []
}

variable "enable_api_key_revoke" {
  description = "Allow the kill switch to revoke (soft-delete) API keys"
  type        = string
  default     = "false"
}

variable "enable_gke_scale_down" {
  description = "Allow the kill switch to scale GKE node pools to 0"
  type        = string
  default     = "false"
}

variable "region" {
  description = "Primary region for Cloud Run, Scheduler, Pub/Sub"
  type        = string
  default     = "us-central1"
}

variable "service_name" {
  description = "Cloud Run service name"
  type        = string
  default     = "cost-killswitch"
}

variable "dry_run" {
  description = "If true, the service logs actions without executing them"
  type        = string
  default     = "true"
}

variable "allowlist" {
  description = "Comma-separated project IDs that must never be shut down"
  type        = string
  default     = ""
}

variable "enable_billing_shutoff" {
  type    = string
  default = "false"
}

variable "enable_run_pause" {
  type    = string
  default = "false"
}

variable "enable_trigger_disable" {
  type    = string
  default = "false"
}

variable "stop_compute_instances" {
  type    = string
  default = "false"
}

variable "budget_amount_usd" {
  description = "Monthly budget amount in USD for monitored projects"
  type        = number
  default     = 5
}

variable "self_budget_amount_usd" {
  description = "Monthly budget for the hub project (CloudManagement itself). Email-only alert — does NOT trigger the kill switch."
  type        = number
  default     = 5
}

variable "self_alert_email" {
  description = "Email address to notify when the hub project's own budget alert fires. Not routed to the kill switch."
  type        = string
  default     = ""
}

variable "image" {
  description = "Container image URI for the kill switch service"
  type        = string
  default     = ""
}

variable "bigquery_dataset_location" {
  type    = string
  default = "US"
}

variable "firestore_location" {
  description = "Firestore multi-region/region for the account registry database (e.g. nam5, eur3)"
  type        = string
  default     = "nam5"
}
