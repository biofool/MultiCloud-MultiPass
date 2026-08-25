# Infrastructure Architecture

## Terraform layout

All Terraform in `terraform/` (GCP provider ~> 5.0, Terraform >= 1.5).
Split into focused files from original monolithic `main.tf`.

| File | Resources |
|------|-----------|
| `provider.tf` | GCP provider config (project, region) |
| `variables.tf` | All input variables |
| `locals.tf` | Derived locals (org/folder roles, budget targets, billing accounts) |
| `cloud_run.tf` | `google_cloud_run_service.killswitch` (scale 0-1, env vars) |
| `iam.tf` | Service account `killswitch-rt`, org/folder/project IAM, Cloud Run invoker |
| `pubsub.tf` | `google_pubsub_topic.budget-alerts`, push subscription → Cloud Run |
| `scheduler.tf` | 3 Cloud Scheduler jobs: `/poll` (5min), `/poll-intents` (2min), `/reconcile` (daily 06:00 UTC) |
| `budgets.tf` | Per-project `google_billing_budget` + hub self-budget (email-only) |
| `storage.tf` | BigQuery dataset `cloud_billing_export`, Firestore `(default)` DB, account registry seed docs |
| `outputs.tf` | Cloud Run URL, Pub/Sub topic, SA email, BQ dataset, Firestore DB |

## Cloud Run service

- **Scale:** min 0, max 1 instance. Prevents self-scaling cost feedback loop.
- **Concurrency:** 1 (container_concurrency).
- **Env vars:** `DRY_RUN`, `ALLOWLIST`, all `ENABLE_*` toggles, `PROJECT_ID`,
  `SELF_PROJECT_ID`, `ALERT_TOPIC`, `BQ_BILLING_TABLE`, `BUDGET_AMOUNT_USD`,
  `USE_FIRESTORE=true`, `FIRESTORE_PROJECT`.
- **Service account:** `killswitch-rt@<hub-project>.iam.gserviceaccount.com`.
- **Image:** Artifact Registry (`<region>-docker.pkg.dev/<project>/cloud-management/killswitch:latest`).

## IAM model

- **Org/folder-level roles** (inherited into every monitored project):
  `roles/run.admin`, `roles/monitoring.viewer`, plus conditional roles
  (`cloudscheduler.admin`, `compute.instanceAdmin.v1`, `cloudbuild.builds.editor`,
  `serviceusage.apiKeysAdmin`, `container.admin`) based on `enable_*` toggles.
- **Billing-account-level grants:** `roles/billing.admin` on each distinct
  billing account (hub + monitored).
- **Hub-project-only roles:** `pubsub.subscriber`, `bigquery.dataViewer`,
  `bigquery.jobUser`, `firestore.user`, `run.invoker`.
- **Cloud Run invoker:** `roles/run.invoker` granted to `killswitch-rt` SA
  only — restricts `/poll`, `/poll-intents`, `/reconcile` to OIDC-authenticated
  Scheduler/Pub/Sub calls. OBSERVED in `iam.tf` + `admin_routes.py` docstring.

## Budget alerts

- **Per monitored project:** `google_billing_budget.monthly` with thresholds
  at 50% (current), 90% (forecast), 100% (current), 150% (current). All
  updates → Pub/Sub `budget-alerts` topic.
- **Hub self-budget:** `google_billing_budget.self` — email-only notification
  (NOT to Pub/Sub). Prevents feedback loop. Thresholds: 50%, 100%.

## Firestore

- `(default)` database, `FIRESTORE_NATIVE` mode.
- Location: `var.firestore_location` (default `nam5`).
- Seeded with `monitored_projects` entries via `google_firestore_document.account_registry_seed`.

## BigQuery

- Dataset `cloud_billing_export` in hub project.
- Location: `var.bigquery_dataset_location` (default `US`).
- Table naming: `gcp_billing_export_resource_v1_<billing_account_id_with_underscores>`.
- SQL queries in `sql/` (daily spend, MTD, top services, spike detection, daily summary).

## Deployment

- **Terraform:** `cd terraform && terraform init && terraform plan`
- **Manual gcloud:** `scripts/deploy.sh <PROJECT_ID> <BILLING_ACCOUNT_ID> [REGION]`
- **Docker:** `Dockerfile` (python:3.12-slim, copies service + client + providers + templates)
