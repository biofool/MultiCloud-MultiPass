# Technology Underpinnings — CloudManagement

CloudManagement is a Python Flask service that runs on Google Cloud Run.
It watches cloud projects, receives usage reports, and runs kill-switch
actions. This document describes the main features and the files that
implement them.

## 1. Cost kill switch

**What it does:** Stops billable resources in a cloud project when a budget
alert or runaway-consumption signal fires.

**Implementation:**
- `main.py` exposes the Flask app and the `execute_killswitch()`
  orchestrator. It protects the hub project with `SELF_PROJECT_ID` to
  avoid a feedback loop.
- `killswitch_actions.py` performs the actions: scale Cloud Run services
  to zero, pause Cloud Scheduler jobs, disable Cloud Build triggers, stop
  Compute Engine instances, scale GKE node pools to zero, revoke API keys,
  and unlink billing.
- `providers/gcp.py` wraps these actions into a `GcpProvider` class and
  exposes per-job kill descriptors such as `cloud_run`, `cloud_scheduler`,
  `gce`, and `gke`.
- `alerts.py` parses Pub/Sub budget-alert messages and decides whether a
  threshold has been reached.
- `dedup.py` drops duplicate Pub/Sub messages within a 10-minute window.

All actions respect `DRY_RUN=true` (log only) and the `ALLOWLIST` and
registry allowlist.

## 2. Real-time quota polling

**What it does:** Checks Cloud Monitoring quota metrics every few minutes
and trips the kill switch before billing exports catch up.

**Implementation:**
- `poller.py` is invoked by Cloud Scheduler via `POST /poll` in
  `admin_routes.py`.
- It evaluates three rules per registered account:
  1. `quota_exceeded` — any nonzero `serviceruntime.googleapis.com/quota/exceeded`.
  2. `baseline_ratio` — recent usage exceeds the trailing 1-hour baseline
     by `BASELINE_MULTIPLIER` (default 5×).
  3. `absolute_cap` — recent rate exceeds the account's `quota_rpm_cap`.
- It skips the hub project to avoid self-kill.

## 3. Intent/actual reporting protocol

**What it does:** Lets sub-projects declare expected API usage before
making calls and report actuals after. CloudManagement detects overruns
and can kill the specific job.

**Implementation:**
- `intent.py` configures the protocol and re-exports the subsystem.
- `intent_routes.py` registers the Flask blueprint and routes:
  `POST /api/v1/intent`, `POST /api/v1/actual`,
  `GET /api/v1/expected-costs/<project_id>`, and others.
- `intent_models.py` defines `Intent`, `Actual`, `ExpectedCost`, and
  `KillEvent` dataclasses.
- `intent_storage.py` persists records to Firestore in production or to
  YAML files in `config/api_intents.yaml`, `config/api_actuals.yaml`,
  `config/expected_costs.yaml`, and `config/kill_events.yaml` for local
  development.
- `intent_detection.py` checks whether actuals exceed expected usage by
  `INTENT_VARIANCE_THRESHOLD` (default 1.2×) or exceed a rate cap.
- `intent_kill.py` executes per-job kills using provider adapters or
  HTTP callbacks described in the intent or the registry.
- `intent_auth.py` validates report tokens.

## 4. Account registry

**What it does:** Tracks every monitored project, owner, budget, quota
cap, allowlist status, cloud provider, and fallback kill descriptors.

**Implementation:**
- `registry.py` provides an `Account` dataclass and a backend switch:
  Firestore when `USE_FIRESTORE=true`, or YAML at `config/accounts.yaml`
  when false.
- `config/accounts.example.yaml` shows the schema for the YAML backend.
- `scripts/register_project.py` is a CLI tool for adding projects.

## 5. Resource inventory

**What it does:** Produces a unified list of cloud accounts and resources.

**Implementation:**
- `inventory.py` reads the account registry and parses
  `terraform/terraform.tfstate` to extract managed resources such as
  Cloud Run services, Cloud Functions, Cloud Scheduler jobs, BigQuery
  datasets, Storage buckets, Firestore databases, Pub/Sub topics, billing
  budgets, and service accounts.
- `GET /api/v1/inventory` returns accounts, resources, and a summary by
  cloud and resource type.

## 6. Dashboard

**What it does:** Serves a web UI and JSON endpoints for cost and status.

**Implementation:**
- `dashboard.py` is a Flask blueprint that queries BigQuery billing export
  for month-to-date spend, daily spend, top services, top projects, and a
  team view across accounts.
- `templates/dashboard.html` renders the UI.
- Results are cached in memory for five minutes to reduce BigQuery scans.

## 7. Multi-cloud provider adapters

**What it does:** Lets the kill switch and reconciliation work across
GCP, OpenStack, Cloudflare, and arbitrary HTTP endpoints.

**Implementation:**
- `providers/base.py` defines the abstract `CostProvider` with
  `fetch_billed_costs()` and `kill_job()`.
- `providers/registry.py` routes kill descriptors to the right provider.
- `providers/gcp.py` implements GCP-native kills and BigQuery
  reconciliation.
- `providers/openstack.py` stops Nova servers.
- `providers/cloudflare.py` disables Workers and Pages via GraphQL.
- `providers/http_callback.py` sends a portable `POST` to any URL.

## 8. Sub-project client

**What it does:** Gives other projects a small client to declare intent
and report actuals without adding heavy cloud dependencies.

**Implementation:**
- `cloud_management_client/__init__.py` exports `CloudManagementClient`.
- `cloud_management_client/client.py` assembles the class from mixins:
  `_lifecycle.py`, `_transport.py`, `_intent_ops.py`, `_actual_ops.py`.
- `cloud_management_client/models.py` defines request/response models.
- `cloud_management_client/spool.py` stores reports on disk if the network
  is unavailable.
- `pyproject.toml` packages the client as `cloud-management-client`.

## 9. Deployment infrastructure

**What it does:** Provisions the Cloud Run service and supporting GCP
resources.

**Implementation:**
- `terraform/` contains split Terraform modules:
  - `cloud_run.tf` — the Flask service
  - `scheduler.tf` — Cloud Scheduler jobs for `/poll`, `/poll-intents`,
    and `/reconcile`
  - `pubsub.tf` — budget-alert topic and push subscription
  - `storage.tf` — BigQuery dataset and Firestore database
  - `iam.tf` — service account and IAM bindings
  - `budgets.tf` — per-project billing budgets
- `scripts/deploy.sh` is an alternative gcloud-based deployment path.
- `Dockerfile` builds the service image on Python 3.12 slim.

## 10. Configuration and secrets

**What it does:** Centralizes settings without committing credentials.

**Implementation:**
- `.env.example` lists all environment variables.
- `secret_loader.py` reads values from GCP Secret Manager when
  `USE_SECRET_MANAGER=true`; otherwise it falls back to environment
  variables.
- `paths.py` resolves all file paths relative to the project root, with a
  `CLOUDMANAGEMENT_ROOT` override for containers.

## 11. Testing

**What it does:** Verifies registry, providers, poller, killswitch,
inventory, intent, dashboard, and client behavior.

**Implementation:**
- `tests/` contains pytest suites.
- Run tests with `python -m pytest tests/ -v` after installing
  `pip install -e ".[dev]"`.
