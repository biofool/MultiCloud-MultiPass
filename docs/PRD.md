# CloudManagement — Product Requirements Document

**Status:** Active
**Last updated:** 2026-07-26
**GitHub:** `https://github.com/your-org/CloudManagement`
**Former name:** CloudBilling (GCP Cost Kill Switch)

---

## 1. Summary

CloudManagement is the **one place that understands all cloud resources and
accounts** across the your-org project portfolio. It serves three roles:

1. **Resource & account inventory** — a unified registry of every cloud
   project, billing account, service, and job across all your-org repos.
   Every other repo's PRD references this inventory for where to store data
   and where to run jobs.
2. **Cost kill switch** — centralized cost-control that can automatically
   stop runaway spend, from a fast quota-based trip in minutes to a nuclear
   billing shutoff.
3. **Intent/actual reporting hub** — sub-projects declare expected API usage
   before making calls and report actuals after. CloudManagement validates
   actual vs intent, detects overruns, and can kill the specific job.

This PRD defines the inventory schema, the job-placement policy (where to
store / where to run), and the coordination rules that every other repo must
follow.

---

## 2. Goals

- Maintain a single, authoritative inventory of all cloud resources and
  accounts across the your-org portfolio, readable by both humans (dashboard)
  and machines (API).
- Define and enforce a job-placement policy that tells every repo where to
  store data and where to run jobs, based on cost, free-tier limits, and
  operational constraints.
- Provide a cost kill switch that can stop runaway spend at three levels:
  per-job (intent/actual overrun), per-project (quota-based), and
  per-billing-account (billing shutoff).
- Provide an intent/actual reporting protocol that sub-projects integrate
  via the `cloud_management_client` pip package.
- Keep the inventory and strategy docs in sync: when any repo changes its
  cloud footprint, CloudManagement is updated; when CloudManagement's
  strategy changes, every affected repo's PRD is updated.

## 3. Non-goals

- Replacing each cloud provider's native billing console or cost-explorer
  UI. CloudManagement is a control plane, not a full BI dashboard.
- Managing infrastructure provisioning for other repos. Each repo manages
  its own Terraform/OpenTofu; CloudManagement only inventories what exists.
- Enforcing the job-placement policy at the code level. The policy is
  documented guidance; repos are expected to follow it, and PRs that
  deviate should explain why.

---

## 4. Cloud account inventory

### 4.1 Accounts that incur usage-based billing (monitored)

| # | Cloud | Account / project ID | Owner repo(s) | Paid APIs / resources |
|---|-------|---------------------|---------------|----------------------|
| A | GCP | `your-hub-project` | `your-org/AIRichardMoon` | Gemini Developer API (token-priced), Cloud Run, Firestore, Pub/Sub, Cloud Storage, Cloud Build, Cloud Functions |
| B | GCP | `your-gcp-project-1` | `your-org/WorldStudioFinder`, `your-org/your-repo-6`, `your-org/your-security-repo` | Google Places ($0.035/call), Geocoding, Knowledge Graph, Outscraper, SerpAPI, Hunter.io, Snov.io, Apollo.io, NeverBounce, ZeroBounce, SendGrid, Gemini, Brave Search, PhantomBuster, Facebook Graph, Compute Engine, GCS |
| C | GCP | `your-gcp-project-2` | `your-org/WorldStudioFinder`, `your-org/your-repo-6` | Same API set as B (shared scraper stack) |
| D | GCP | `your-deprecated-project-id` (deprecated) | `your-org/WorldStudioFinder` | Possibly residual Places/Geocoding calls |
| E | OpenStack (your-openstack-provider) | project `your-openstack-project-id`, regions `your-region-1` + `your-region-2` | `your-org/FieldWorker`, `your-org/FieldAppAndroid` | Compute instances, volumes, snapshots |
| F | Cloudflare | zone `your-domain.com`, Pages project `your-pages-project` | `your-org/your-security-repo` | Pages bandwidth, redirect rules (mostly free tier) |
| G | Cloudflare R2 | per-user account | `your-org/ClipQuotes` | Object storage (S3-compatible) if rclone R2 configured |
| H | HuggingFace | per-user token | `your-org/ClipQuotes` | `pyannote` diarization models (free tier; paid inference possible) |

### 4.2 Accounts excluded (no usage-based billing)

| Repo | Why excluded |
|------|-------------|
| `your-org/your-domain.com` | PHP frontend on shared hosting (your-shared-hosting.com); no paid APIs. Proxies to AIRichardMoon backend, which is monitored. |
| `your-org/your-domain.com` | Static HTML site on your-shared-hosting.com; no APIs. |
| `your-org/your-domain.com` | PHP landing page on your-shared-hosting.com; no paid APIs. |

### 4.3 Cross-cutting note on B/C/D

WorldStudioFinder, your-project-6, and Security/UnusedOS all touch the same
GCP projects (`your-org-project-id…`, `your-gcp-project-2`). They are effectively
one billing surface with multiple codebases writing to it. CloudManagement
registers the **GCP project** once and tags each intent/actual record with
the **source repo + job_id** so cost can be attributed back to the
responsible codebase even when they share a billing account.

---

## 5. Inventory schema

The inventory is stored in `config/accounts.yaml` (local dev) or Firestore
`accounts` collection (production). Each entry is an `Account` dataclass
(see `registry.py`):

```yaml
accounts:
  - project_id: your-project-1          # logical key used in intent/actual reports
    cloud: gcp                          # "gcp" | "openstack" | "cloudflare" | "generic"
    gcp_project_id: your-hub-project
    billing_account_id: "01AB-23CD-EF45"
    owner_email: owner@example.com
    allowlist: true                     # never touched by kill switch
    budget_amount_usd: 10
    currency_code: USD
    quota_rpm_cap: 6000
    report_token_secret: CLOUDMANAGEMENT_REPORT_TOKEN_AIRICHARDMOON
    jobs:                               # per-job kill descriptors
      - job_id_prefix: "gemini-session-"
        kill:
          type: http_callback
          url: "https://your-service-url.a.run.app/v1/admin/kill-session"
          method: POST
          headers:
            X-Kill-Token: "<set via Secret Manager>"
```

### 5.1 Resource inventory (Phase 2 — planned)

The `inventory.py` module extends the account registry with a per-resource
view: Cloud Run services, Cloud Functions, Compute instances, Firestore
databases, Pub/Sub topics, BigQuery datasets, and Storage buckets. This is
populated by reading Terraform state files and/or live cloud API queries.
The `GET /api/v1/inventory` endpoint returns the unified view.

---

## 6. Job-placement policy (where to store / where to run)

This section is the canonical guidance that every other repo's PRD should
reference. When a repo needs to decide where to store data or where to run a
job, it follows this policy. **When this policy changes, every affected
repo's PRD must be updated.**

### 6.1 Where to store data

| Data type | Where to store | Why |
|-----------|---------------|-----|
| **Application state** (sessions, users, messages) | Firestore (GCP `your-hub-project` or per-project) | Free tier generous (50K reads/day, 20K writes/day), native to GCP, no ops |
| **Large binary / media files** (audio, video, images) | Cloud Storage (GCP) or Cloudflare R2 | GCS: free tier 5GB + $0.020/GB-month beyond. R2: zero egress fees, good for media-heavy workloads |
| **Structured datasets** (scrape results, CSV exports) | BigQuery (GCP) or SQLite (local/DVC) | BigQuery: 1TB free queries/month, good for analytics. SQLite: zero cost, DVC-tracked, good for <1M rows |
| **Corpus / knowledge base** (Markdown, FTS indexes) | Git + DVC (small) or Cloud Storage (large) | DVC gives version control without bloating git; Cloud Storage for >100MB indexes |
| **Cost / billing records** | Firestore `api_costs` (per-project) + BigQuery `cloud_billing_export` (hub) | Firestore for real-time per-call tracking; BigQuery for reconciliation against cloud billing export |
| **Secrets** | GCP Secret Manager | Never in git, never in env files committed to repo. $0.03 per secret version per month |
| **Cache / ephemeral** | In-memory or Redis (if available) | Don't persist what you can recompute cheaply |

### 6.2 Where to run jobs

| Job type | Where to run | Why |
|----------|-------------|-----|
| **HTTP request handlers** (chat API, webhooks) | Cloud Run (GCP) | Scale-to-zero, pay per request, generous free tier (2M requests/month). `your-hub-project` project |
| **Background pollers / schedulers** | Cloud Scheduler → Cloud Run (GCP) | 3 free jobs/month per project; 5-min cron is standard |
| **Event-driven functions** (Pub/Sub → email/SMS) | Cloud Functions 2nd gen (GCP) | Pub/Sub-triggered, scale-to-zero, 2M invocations/month free |
| **Long-running batch jobs** (scraping, data processing) | Compute Engine VM (GCP) or OpenStack instance | Cloud Run max timeout is 60min. For >60min jobs, use a VM. OpenStack for cost-sensitive batch (your-openstack-provider) |
| **ML inference** (transcription, diarization) | Local GPU or HuggingFace free tier | Cloud GPU is expensive ($0.50+/hr). Use local GPU or HF free tier for development; only use cloud GPU for production-scale |
| **One-off scripts / fix scripts** | Local workstation | `scripts/fix/` convention with `--dry-run`. Never run fix scripts in production without testing |
| **Static websites** | Cloudflare Pages or your-shared-hosting.com shared hosting | Free, no compute cost. Cloudflare for modern stacks; your-shared-hosting.com for PHP |
| **Cron jobs < 15 min** | Cloud Scheduler → Cloud Run | Free tier covers most cron workloads |

### 6.3 Free-tier budget tracker

| Resource | Free tier limit | Where to track |
|----------|----------------|----------------|
| Cloud Run requests | 2M/month | CloudManagement dashboard |
| Cloud Run vCPU-seconds | 360,000/month | CloudManagement dashboard |
| Cloud Run GB-seconds | 180,000/month | CloudManagement dashboard |
| Firestore reads | 50,000/day | Per-project `ProviderCostTracker` |
| Firestore writes | 20,000/day | Per-project `ProviderCostTracker` |
| Cloud Storage | 5GB + 5GB egress/month | CloudManagement BigQuery reconciliation |
| BigQuery queries | 1TB/month | CloudManagement BigQuery reconciliation |
| Cloud Functions invocations | 2M/month | CloudManagement dashboard |
| Cloud Scheduler jobs | 3/month free per project | Terraform `google_cloud_scheduler_job` count |
| Gemini Developer API | 10 RPM, 1,500 RPD (free tier) | Per-project `GeminiCostTracker` + CloudManagement intent/actual |
| Secret Manager | 6 secret versions/month free | Terraform `google_secret_manager_secret` count |
| Cloudflare Pages | 500 builds/month, unlimited bandwidth | Cloudflare dashboard |

---

## 7. The cloud-strategy coordination rule

### 7.1 When a repo changes its cloud footprint

**Every your-org repo MUST update CloudManagement and the `your-org/starter`
template when it:**

1. Adds, removes, or changes a cloud resource (project, service, bucket, etc.)
2. Changes where data is stored (e.g. moves from Firestore to BigQuery)
3. Changes where jobs run (e.g. moves from Cloud Run to Compute Engine)
4. Adds a new paid API or changes an existing one's usage pattern
5. Changes cloud provider, region, or project

**The update process is:**

1. Update `config/accounts.yaml` (or Firestore in production) in
   CloudManagement to reflect the new resource.
2. Update `docs/PRD.md` in CloudManagement if the change affects the
   job-placement policy or resource taxonomy (sections 5–6).
3. Update the `your-org/starter` template's cloud-strategy section so
   future repos inherit the latest guidance.
4. Update the repo's own PRD (if it has one) with the new where-to-store /
   where-to-run details.

### 7.2 When CloudManagement's strategy changes

When CloudManagement's inventory schema, job-placement policy, or
kill-switch behavior changes:

1. Update `docs/PRD.md` in CloudManagement.
2. Update every affected repo's PRD with the new where-to-store /
   where-to-run guidance.
3. Update the `your-org/starter` template.
4. Bump the `cloud_management_client` package version if the API protocol
   changed, and update consumers.

### 7.3 The template repo (`your-org/starter`)

The `your-org/starter` template repo is the source of shared AI coding
config (AGENTS.md, CLAUDE.md, .devin/skills/). It also carries the
canonical cloud-strategy guidance that new repos inherit. When
CloudManagement's job-placement policy or inventory schema changes, the
template must be updated so that repos created after the change start with
the latest guidance.

---

## 8. Intent/actual reporting protocol

Sub-projects integrate with CloudManagement via the `cloud_management_client`
pip package (see `cloud_management_client/__init__.py`). The protocol:

1. **Declare intent** (`POST /api/v1/intent`) — before making API calls,
   the sub-project declares expected usage (calls, cost, tokens, rate
   limit). CloudManagement responds with `approved: true/false` based on
   remaining budget.
2. **Report actual** (`POST /api/v1/actual`) — after (or during) the calls,
   the sub-project reports actual usage. CloudManagement detects overruns
   and can trigger a kill.
3. **Pull expected costs** (`GET /api/v1/expected-costs/<project_id>`) —
   the sub-project pulls authoritative expected-cost records.
4. **Manual kill** (`POST /api/v1/kill/<intent_id>`) — dashboard-triggered
   kill of a specific job.

All requests require `Authorization: Bearer <CLOUDMANAGEMENT_REPORT_TOKEN>`
(per-project token). See `docs/per-repo-api-specs.md` for per-repo
specifications.

---

## 9. Architecture

See `README.md` for the architecture diagram. Key components:

- `main.py` — Cloud Run service entry point (budget alerts, quota polling,
  dashboard, inventory endpoint)
- `poller.py` — quota-based polling loop (5-min Cloud Scheduler)
- `registry.py` — monitored account registry (YAML or Firestore)
- `intent.py` — kill-switch intent evaluation
- `inventory.py` — unified cloud resource inventory (accounts + terraform state)
- `cloud_management_client/` — pip-installable client for sub-projects
- `cloud_auth/` — per-project GCP credential management (NEVER commit)
- `config/` — YAML config files (accounts, intents, kill events)
- `terraform/` — GCP infrastructure provisioning
- `providers/` — cloud provider adapters (GCP, OpenStack, Cloudflare, HTTP callback)

---

## 10. Open items / Phase 2

- **Resource inventory** — `inventory.py` currently reads accounts + basic
  terraform state. Phase 2 adds live cloud API queries (list Cloud Run
  services, Compute instances, Storage buckets) for a real-time view.
- **Multi-cloud reconciliation** — GCP BigQuery export is wired; OpenStack
  metering and Cloudflare GraphQL reconciliation are planned.
- **Token rotation** — per-project `CLOUDMANAGEMENT_REPORT_TOKEN` rotation
  needs an admin endpoint + runbook.
- **Dashboard inventory view** — the dashboard HTML needs a resource
  inventory tab showing all resources grouped by account/project.
