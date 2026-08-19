# locals.tf
# Split out of the original monolithic main.tf — pure structural move,
# no behavior change. Terraform loads every *.tf file in a directory
# automatically, so resource references across these files work exactly
# as they did when everything lived in one file.

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

