# budgets.tf — per-project budgets + the hub project's self-budget
# Split out of the original monolithic main.tf — pure structural move,
# no behavior change. Terraform loads every *.tf file in a directory
# automatically, so resource references across these files work exactly
# as they did when everything lived in one file.

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

