# CloudManagement

**The one place that understands all cloud resources and accounts across your project portfolio.**

CloudManagement evolved from the GCP Cost Kill Switch (formerly CloudBilling) and now serves three roles:

1. **Resource & account inventory** — a unified registry of every cloud project, billing account, service, and job across all your repos. Every other repo's PRD references this inventory for where to store data and where to run jobs. See `docs/PRD.md` for the job-placement policy.
2. **Cost kill switch** — centralized cost-control for a small team of independent cloud developers. One hub project watches every teammate's GCP project and can automatically stop runaway spend — from a fast quota-based trip in minutes, down to a nuclear billing shutoff.
3. **Intent/actual reporting hub** — sub-projects declare expected API usage before making calls and report actuals after. CloudManagement validates actual vs intent, detects overruns, and can kill the specific job that is accumulating cost. This layer is **provider-agnostic** — it works for GCP, OpenStack, Cloudflare, and any third-party API without needing each cloud's billing API in the hot path.

## Free-tier optimization strategy

Your project portfolio may span multiple cloud accounts across several providers (GCP, OpenStack, Cloudflare, HuggingFace). Most of these have meaningful free tiers — but free tiers are per-account, per-API, and reset on different schedules (daily vs monthly). Without a central coordinator, a small team will either leave free-tier capacity on the table (paying for calls that could have been free) or accidentally blow past a free-tier limit and incur surprise charges.

CloudManagement optimizes free-tier usage across the portfolio through four mechanisms:

### 1. Free-tier-aware job placement

The job-placement policy in `docs/PRD.md` routes each workload to the provider whose free tier best fits it:

| Workload | Placed on | Free tier used |
|----------|-----------|----------------|
| HTTP request handlers (chat API, webhooks) | Cloud Run (GCP) | 2M requests/month, scale-to-zero |
| Background pollers / schedulers | Cloud Scheduler → Cloud Run | 3 free jobs/month per project |
| Event-driven functions (email/SMS) | Cloud Functions (GCP) | 2M invocations/month, Pub/Sub-triggered |
| Long-running batch jobs (scraping) | Compute Engine or OpenStack | OpenStack for cost-sensitive batch (your-openstack-provider) |
| ML inference (transcription, diarization) | Local GPU or HuggingFace free tier | Cloud GPU is $0.50+/hr; HF free tier for dev |
| Static websites | Cloudflare Pages or shared hosting | 500 builds/month, unlimited bandwidth |
| Large binary / media files | Cloudflare R2 or GCS | R2: zero egress fees; GCS: 5GB free |
| Application state (sessions, users) | Firestore | 50K reads/day, 20K writes/day free |
| Structured datasets / analytics | BigQuery or SQLite | BigQuery: 1TB free queries/month |

This is not a one-time decision — CloudManagement tracks which accounts are approaching their free-tier limits and the dashboard surfaces free-tier progress bars per API (e.g., "Gemini: 1,420/1,500 daily calls used", "Google Places: 6,250/10,000 monthly calls remaining"). When a project's free-tier budget for one API is exhausted, the multi-source API router in WorldStudioFinder automatically falls back to a free-tier alternative (OpenCage, HERE, Azure Maps) and declares intent for the fallback, not the paid API.

### 2. Real-time free-tier tracking via intent/actual

The intent/actual protocol gives CloudManagement real-time visibility into free-tier consumption that cloud billing exports can't provide (they lag 24–48h). Each sub-project declares intent before making API calls:

```json
{
  "project_id": "your-project-2",
  "provider": "google_places",
  "endpoint": "nearbysearch",
  "expected_calls": 500,
  "metadata": {"free_tier_remaining_today": 850, "reason": "google_places_budget_exhausted"}
}
```

CloudManagement aggregates this across all projects per API and tracks free-tier burn rate in real time. The `GET /api/v1/expected-costs/<project_id>` endpoint pushes authoritative free-tier remaining counts back to each project's local cost tracker, so projects can make routing decisions (e.g., switch to OpenCage when Google Places free tier is exhausted for the day).

### 3. Reconciliation tier for accuracy

Free-tier tracking via self-reporting is fast but can drift from reality (a project forgets to report, or reports the wrong count). The reconciliation tier (`/reconcile`, daily) pulls actual billed costs from each provider's billing API and cross-checks them against self-reported actuals:

- **GCP**: BigQuery billing export
- **OpenStack**: your-openstack-provider metering API
- **Cloudflare**: GraphQL analytics API

When billed costs diverge from self-reported actuals by more than a threshold, CloudManagement recalibrates the `expected_costs` record (adjusts `unit_cost_usd` and `calibration_delta`) and emits an accuracy alert. This is what makes the free-tier tracking **accurate** rather than just fast — projects pull the corrected expected costs every 15 minutes and update their local routing decisions.

### 4. Kill before you pay

The kill switch has three levels of escalation, each of which protects a different free-tier boundary:

| Level | Trigger | What it stops | Latency |
|-------|---------|---------------|---------|
| **Per-job** (intent/actual) | actual exceeds expected by 1.2×, or rate exceeds declared cap | The specific job that is accumulating cost (via `http_callback`, `cloud_run`, `gce`, etc.) | Seconds — detected on each actual report |
| **Per-project** (quota poller) | quota-exceeded event, usage spike vs rolling baseline, or absolute rate cap | All billable services in the project (Cloud Run, Scheduler, GCE, GKE, API keys, build triggers) | Minutes — `/poll` runs every 5 min |
| **Per-billing-account** (budget alert) | spend crosses 50%/90%/100%/150% of budget | Billing is unlinked from the project (nuclear option) | 12–24h — Cloud Billing budget alert lag |

The per-job level is the key free-tier protection: a runaway loop hammering a paid API (retry storm, stuck cron) is killed in seconds, before it can exhaust the free tier and start incurring charges. The per-project level catches quota spikes that don't go through the intent/actual protocol. The per-billing-account level is the last-resort backstop for cost creep that doesn't show up as a rate spike.

### Multi-cloud free-tier map

CloudManagement maintains a unified view of free-tier limits across all providers and accounts:

| Provider / API | Free tier limit | Reset cycle | Tracked in |
|----------------|----------------|-------------|------------|
| GCP Cloud Run | 2M requests, 360K vCPU-sec, 180K GiB-sec / month | Monthly | CloudManagement dashboard |
| GCP Firestore | 50K reads, 20K writes / day | Daily | Per-project `ProviderCostTracker` |
| GCP Cloud Storage | 5GB + 5GB egress / month | Monthly | BigQuery reconciliation |
| GCP BigQuery | 1TB queries / month | Monthly | BigQuery reconciliation |
| GCP Cloud Functions | 2M invocations / month | Monthly | CloudManagement dashboard |
| GCP Cloud Scheduler | 3 jobs / month per project | Monthly | Terraform resource count |
| GCP Secret Manager | 6 secret versions / month | Monthly | Terraform resource count |
| Gemini Developer API | 10 RPM, 1,500 RPD | Daily | Per-project `GeminiCostTracker` + intent/actual |
| Google Places API | $200/month credit | Monthly | Intent/actual + BigQuery reconciliation |
| Cloudflare Pages | 500 builds/month, unlimited bandwidth | Monthly | Cloudflare dashboard |
| Cloudflare R2 | 10GB storage, 1M Class A ops / month | Monthly | Cloudflare dashboard |
| HuggingFace | Free tier (rate-limited) | Per-user | Per-project tracking |
| OpenStack (your-openstack-provider) | Pay-per-use (no free tier) | N/A | OpenStack metering API |

The dashboard surfaces free-tier progress bars per API, aggregated across all projects. When a free-tier budget is approaching exhaustion, the dashboard highlights it and the multi-source API router in the affected project automatically falls back to a free-tier alternative.

## 1. Architecture Summary

```
                 ┌───────────────────────────────────────────────────────┐
                 │                    Hub project (shared)                 │
                 │                                                         │
Cloud Billing ──▶│ Pub/Sub (budget-alerts) ──────┐                        │
Budget alert     │  (one per monitored project)   │                        │
(12-24h lag)     │                                ▼                        │
                 │  Cloud Scheduler ──5min──▶ Cloud Run service            │
                 │  (/poll)                    │  POST /     (budget path) │
                 │                              │  POST /poll (quota path) │
                 │                              │  GET  /dashboard         │
                 │                              │  GET  /api/v1/inventory  │
                 │                              │  POST /api/v1/intent     │
                 │                              │  POST /api/v1/actual     │
                 │                              │  GET  /api/v1/expected-costs │
                 │                              ▼                          │
                 │                    execute_killswitch(project, reason)  │
                 │                              │                          │
                 │                    Firestore "accounts" registry        │
                 │                    (self-service via register_project)  │
                 └──────────────────────────────┼──────────────────────────┘
                                                 ▼
                        Org/Folder-level IAM — granted once, inherited into
                        every teammate project (no per-project setup)
                                                 │
                     ┌───────────────────────────┼───────────────────────────┐
                     ▼                           ▼                           ▼
              Dev A's project             Dev B's project             Dev C's project
       Cloud Run · GCE · Scheduler   Cloud Run · GCE · Scheduler   Cloud Run · GCE · Scheduler
       Build triggers · API keys     Build triggers · API keys     Build triggers · API keys
       GKE node pools · Monitoring   GKE node pools · Monitoring   GKE node pools · Monitoring
```

**Two-tier accuracy model:**

1. **Real-time tier (intent/actual protocol)** — every sub-project self-reports expected API usage *before* making calls and actual usage *after*. CloudManagement detects overruns in minutes and can kill the **specific job** that is accumulating cost, not just the whole project. This layer is **provider-agnostic** — it works for GCP, OpenStack, Cloudflare, and any third-party API (Google Places, Gemini, Hunter.io, etc.) without needing each cloud's billing API in the hot path.

2. **Reconciliation tier (cloud billing export)** — CloudManagement pulls actual billed costs from GCP BigQuery export / OpenStack metering / Cloudflare GraphQL on a 12–48h lag and cross-checks them against self-reported actuals. Discrepancies flag accuracy alerts and recalibrate each project's cost model. This is what makes the system **accurate** rather than just fast.

**Components:**
- **Cloud Billing export → BigQuery** — detailed usage cost data for queries/analysis (hub project dataset, one table per billing account)
- **Cloud Billing budget** — one per monitored project, threshold alerts (50% actual, 90% forecast, 100% actual, 150% actual), all pushed to the shared Pub/Sub topic
- **Pub/Sub topic** — receives budget notifications from every monitored project, pushes to Cloud Run (**cost path**, 12-24h lag)
- **Cloud Scheduler → `/poll`** — hits the same Cloud Run service every 5 minutes to check Cloud Monitoring quota metrics across every registered account (**real-time path**, minutes)
- **Cloud Run service** — Python Flask app, scale-to-zero, handles the Pub/Sub push, the poll trigger, and the intent/actual reporting endpoints
- **Firestore account registry** — tracks which projects are monitored, who owns them, budgets, quota caps, and allowlist status; falls back to a local YAML file for dev/tests
- **Provider adapters** (`providers/`) — pluggable backends for GCP, OpenStack, Cloudflare, and HTTP-callback kills; the intent/actual protocol itself is provider-agnostic
- **One service account, org/folder-level IAM** — a single hub identity, granted roles once at the Organization or Folder level, inherited automatically into every teammate project — no per-project IAM setup as the team grows
- **No always-on services** — the Cloud Run service scales to zero between invocations

**Data flow (three independent triggers, same action path):**
1. **Cost path:** spend crosses a threshold on any monitored project → Cloud Billing publishes to the shared Pub/Sub topic → pushed to Cloud Run `/` → `execute_killswitch(project, reason="budget_alert")`
2. **Quota path:** Cloud Scheduler invokes `/poll` every 5 minutes → for each registered, non-allowlisted account, queries Cloud Monitoring for quota-exceeded events, usage vs. a rolling 1h baseline, or an absolute rate cap → on a trip, `execute_killswitch(project, reason="quota_spike")`
3. **Intent/actual path:** a sub-project declares intent via `POST /api/v1/intent`, then reports actuals via `POST /api/v1/actual` → if actual exceeds expected by `INTENT_VARIANCE_THRESHOLD` (default 1.2×) or rate exceeds declared cap → `execute_killswitch(project, reason="intent_overrun")` with a per-job kill descriptor
4. All paths run the same idempotent actions and are gated by the same env-var flags and `DRY_RUN` default
5. Actions are logged as structured JSON to stdout, tagged with the trigger reason

## 2. Cost Estimate

| Component | Free tier | Estimated monthly cost | Notes |
|-----------|-----------|----------------------|-------|
| **Cloud Run** | 2M requests, 360,000 vCPU-seconds, 180,000 GiB-seconds | **$0** | Scale-to-zero, invoked only on alerts (a few times/month) |
| **Pub/Sub** | 10 GB/month | **$0** | Budget alerts are tiny messages (<1 KB each) |
| **BigQuery** | 1 TB queries/month, 10 GB storage/month | **$0–$0.02** | Billing export data is small (~1–5 MB/month for a small project). Queries filter to current month. |
| **Cloud Billing budget** | Free | **$0** | No charge for budgets or email alerts |
| **Artifact Registry** | 0.5 GB storage | **$0** | Single small container image (~50 MB) |
| **Cloud Build** | 120 build-minutes/day | **$0** | Occasional rebuilds only |
| **Container image storage** | Included in Artifact Registry free tier | **$0** | |
| **Logging** | 50 GB/month ingest | **$0** | Structured JSON logs, minimal volume (only on alerts) |
| **Cloud Monitoring API reads** | Generous free tier (reads are free; you only pay for custom metric ingestion, which this doesn't use) | **$0** | `/poll` runs read-only `list_time_series` queries every 5 min per account |
| **Cloud Scheduler** | 3 free jobs/month per project | **$0** | Two jobs total (budget path needs none — Pub/Sub push is event-driven; poll path uses 1) |
| **Firestore** | 1 GB storage, 50k reads/20k writes per day free | **$0** | Account registry is a handful of small documents, read once per poll cycle |
| **Total** | | **$0–$0.02/month per team, regardless of team size** | |

**Components that could exceed free tier:**
- **BigQuery storage**: if billing export exceeds 10 GB (unlikely for a small project — would need millions of resource records/month)
- **Cloud Run**: if the service is invoked thousands of times/day (budget alerts fire at most a few times/day per threshold; polling adds a steady 288 invocations/day at the default 5-min interval, still well within the 2M/month free tier)
- **Logging**: if DEBUG level is enabled permanently (use INFO in production)
- **Firestore reads**: if the team grows into the hundreds of monitored projects and `POLL_WINDOW_MINUTES` is set very low

## 3. File-by-File Implementation

### Core service

#### `main.py`
Python Flask application deployed to Cloud Run. Handles Pub/Sub push messages, parses budget alerts, evaluates thresholds, takes idempotent actions, and exposes the `/poll`, dashboard, inventory, and intent/actual endpoints.

Key functions:
- `parse_pubsub_message()` — decodes base64 Pub/Sub payload, parses Cloud Billing budget notification schema
- `should_take_action()` — threshold logic: acts on ≥100% actual, ≥90% forecast, or ≥50% actual with forecast over budget
- `disable_cloud_run_services()` — sets min instances to 0, restricts ingress to internal-only
- `pause_scheduler_jobs()` — pauses all enabled Cloud Scheduler jobs
- `disable_build_triggers()` — disables Cloud Build triggers via REST API
- `stop_compute_instances()` — stops running GCE instances
- `scale_down_gke_clusters()` — resizes GKE node pools to 0
- `revoke_api_keys()` — soft-deletes API keys in a project (recoverable for 30 days via `gcloud services api-keys undelete`)
- `disable_billing()` — nuclear option, unlinks billing account from project
- `execute_killswitch(project_id, reason)` — shared orchestrator that runs every enabled action against one project; called by the budget-alert, poller, and intent-overrun paths
- `is_project_protected()` — true if a project is protected by either the env `ALLOWLIST` or the registry's per-account `allowlist` flag
- `process_alert()` — budget-alert orchestrator: dedup → parse → evaluate → filter allowlist → `execute_killswitch()` per target project
- `_is_duplicate()` — in-memory message dedup with 10-minute TTL

#### `registry.py`
The account registry — tracks every monitored project, its owner, budget, quota cap, allowlist status, cloud provider, and job definitions. Firestore-backed in production (`USE_FIRESTORE=true`), YAML-file-backed for local dev/tests. See `config/accounts.example.yaml` for the schema available in the repo.

#### `poller.py`
Real-time detector, invoked via the `/poll` route on a 5-minute Cloud Scheduler cadence. Reads Cloud Monitoring quota metrics per registered account and trips `execute_killswitch()` on a quota-exceeded event, a usage spike vs. rolling baseline, or an absolute rate cap — see the module docstring for the exact rules.

#### `intent.py`
Implements the real-time intent/actual reporting protocol. Sub-projects declare expected API usage before making calls and report actuals after. CloudManagement validates actual vs intent, detects overruns, and can kill the specific job that is accumulating cost.

Endpoints (Flask blueprint):
- `POST /api/v1/intent` — declare intent (pre-call)
- `POST /api/v1/actual` — report actual (post-call / incremental)
- `GET /api/v1/expected-costs/<project_id>` — pull authoritative expected costs
- `GET /api/v1/intents` — list active intents (dashboard)
- `GET /api/v1/intents/<project_id>` — list intents for a project
- `POST /api/v1/kill/<intent_id>` — manual kill override (dashboard)

Data stores (Firestore in production, YAML files in dev/test):
- `api_intents` — one doc per intent declaration
- `api_actuals` — one doc per actual report (supports incremental)
- `expected_costs` — one doc per (project_id, provider) with authoritative pricing
- `kill_events` — audit log of every kill invocation

#### `inventory.py`
Unified cloud resource inventory. Reads the account registry and Terraform state to produce a single view of every cloud resource and account across your portfolio. Exposed via `GET /api/v1/inventory`. This is the "one place that understands all cloud resources and accounts" — other repos' PRDs reference this inventory for where to store data and where to run jobs.

#### `dashboard.py`
Flask blueprint for the web dashboard — renders account status, recent alerts, intent/actual activity, and team accounts view. Exposes JSON API endpoints under `/api/`.

#### `paths.py`
Project-root path resolver. All config and data file paths in this project resolve relative to the project root (auto-detected via `pyproject.toml`), not the current working directory. This ensures scripts and tests work correctly regardless of where they're invoked from. Supports `CLOUDMANAGEMENT_ROOT` env var override for Docker/Cloud Run.

### Provider abstraction

#### `providers/base.py`
Abstract `CostProvider` base class. Each provider implements two capabilities:
- `fetch_billed_costs` — the reconciliation tier: pull actual billed amounts from the cloud's billing/metering API (24-48h lag)
- `kill_job` — execute a per-job kill action described by the intent's kill descriptor

#### `providers/registry.py`
Routes kill descriptors to the right `CostProvider` based on the `type` field (`gcp`, `openstack`, `cloudflare`, `http_callback`). The `http_callback` type is portable and works for any cloud.

#### `providers/gcp.py`
GCP provider — Cloud Run scale-to-zero, Cloud Scheduler pause, GCE stop, GKE node pool scale-down, API key revocation, Cloud Build trigger disable, billing shutoff. This is the most complete provider.

#### `providers/openstack.py`
OpenStack provider — Nova server stop via `openstack server stop`.

#### `providers/cloudflare.py`
Cloudflare provider — Workers/pages disable via GraphQL API.

#### `providers/http_callback.py`
Portable HTTP callback provider — sends `POST` to a configurable URL with the kill descriptor. Works for any cloud or third-party service that exposes an HTTP kill endpoint.

### Sub-project client

#### `cloud_management_client/`
A lightweight, **stdlib-only** pip-installable client that sub-projects use to declare expected API usage before making calls and report actuals after. No Google Cloud dependencies — just `urllib` and `json`. Install via `pip install cloud-management-client`.

```python
from cloud_management_client import CloudManagementClient

cb = CloudManagementClient(
    project_id="your-project-1",
    report_token=os.environ["CLOUDMANAGEMENT_REPORT_TOKEN_AIRICHARDMOON"],
    # base_url defaults to http://127.0.0.1:8080 for local dev;
    # set to the Cloud Run URL in production.
)

# Declare intent before a batch of API calls
intent = cb.declare_intent(
    job_id="gemini-session-abc123",
    job_name="coaching-chat-batch",
    provider="google",
    endpoint="generate_content",
    expected_calls=10,
    expected_cost_usd=0.02,
    rate_limit_rpm=10,
    kill={"type": "http_callback", "url": "https://myapp.example.com/admin/kill-job"},
)

# Report actuals after (or incrementally during long jobs)
cb.report_actual(
    intent_id=intent["intent_id"],
    status="completed",
    actual_calls=8,
    actual_cost_usd=0.016,
)
```

See `docs/per-repo-api-specs.md` for per-repo integration specs (AIRichardMoon, WorldStudioFinder, FieldWorker, Security, ClipQuotes).

### Scripts

#### `scripts/register_project.py`
Self-service CLI a developer runs once to add their project to the registry:
```bash
python scripts/register_project.py --project my-proj --billing-account XXXX-XXXX-XXXX --owner me@example.com
```

#### `scripts/fix/capture_dashboard.py`
Captures a screenshot of the dashboard for audit purposes. Output: `data/audit/dashboard_screenshot.png` . Supports `CLOUDMANAGEMENT_DASHBOARD_URL` env var for the dashboard URL.

#### `scripts/fix/check_bq_billing_export.py`
Verifies BigQuery billing export is configured and producing data for each monitored billing account. Output: `data/audit/bq_billing_export_check_*.json` . Read-only by design.

### Configuration

#### `requirements.txt`
Flask, Google Cloud client libraries for Run, Scheduler, Compute, Billing, BigQuery, Firestore, Monitoring, API Keys, and Container (GKE), plus PyYAML for the local registry backend.

#### `pyproject.toml`
Package metadata for the `cloud-management-client` pip package. Optional `[dev]` dependencies include pytest:
```bash
pip install -e ".[dev]"   # install pytest for tests
```

#### `Dockerfile`
Python 3.12-slim, copies app modules (including `paths.py`), sets `CLOUDMANAGEMENT_ROOT=/app` for path resolution in the container, exposes 8080.

#### `.env.example`
Configuration template for all environment variables.

#### `terraform/`
- `main.tf` — BigQuery dataset, Firestore registry (+ seed documents from `monitored_projects`), Pub/Sub topic+subscription, Cloud Run service, per-project budgets, Cloud Scheduler `/poll` job, org/folder + billing-account IAM bindings
- `variables.tf` — all configurable parameters, including `org_id`/`folder_id` and the `monitored_projects` list
- `provider.tf` — Google provider configuration
- `terraform.tfvars.example` — example variable values for a multi-account team

#### `sql/`
- `01_daily_spend_by_project.sql` — per-project daily cost breakdown
- `02_mtd_spend_by_project.sql` — month-to-date totals
- `03_top_services.sql` — top 20 services by cost
- `04_recent_spikes.sql` — 3-day vs 7-day baseline comparison
- `05_daily_summary_table.sql` — aggregated summary table for low-cost querying

### Data artifacts

Audit outputs from fix/verification scripts are written to `data/audit/` as JSON files. These are gitignored by default — commit them if you want to track them in your fork.

### Path resolution

All config and data paths resolve relative to the project root via `paths.py`, not the current working directory. This means scripts and tests work correctly regardless of where they're invoked from. The project root is auto-detected by walking up from `paths.py` to find `pyproject.toml`; override with `CLOUDMANAGEMENT_ROOT` env var for Docker/Cloud Run.

### Cross-repo coordination

CloudManagement is the canonical source for cloud strategy across all your repos. When any repo changes where data is stored, where jobs run, adds a new cloud resource, or changes its cloud provider/region/project, it MUST update the CloudManagement inventory (`config/accounts.yaml` or via the `POST /api/v1/accounts` endpoint). See `docs/PRD.md` for the job-placement policy and `docs/per-repo-api-specs.md` for per-repo integration specs.
