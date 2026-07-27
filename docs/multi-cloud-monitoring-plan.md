# Multi-Cloud API Billing Monitor — Implementation Plan

**Status:** Draft for approval
**Date:** 2026-07-24
**Author:** Devin

## 0. Executive summary

CloudManagement today is a GCP-only, project-wide cost kill switch. This plan extends it into a **multi-cloud, per-job API billing monitor** with a two-tier accuracy model:

1. **Real-time tier (intent/actual protocol)** — every sub-project self-reports expected API usage *before* making calls and actual usage *after*. CloudManagement detects overruns in minutes and can kill the **specific job** that is accumulating cost, not just the whole project. This layer is **provider-agnostic** — it works for GCP, OpenStack, Cloudflare, and any third-party API (Google Places, Gemini, Hunter.io, etc.) without needing each cloud's billing API in the hot path.
2. **Reconciliation tier (cloud billing export)** — CloudManagement pulls actual billed costs from GCP BigQuery export / OpenStack metering / Cloudflare GraphQL on a 12–48h lag and cross-checks them against self-reported actuals. Discrepancies flag accuracy alerts and recalibrate each project's cost model. This is what makes the system **accurate** rather than just fast.

The plan also opens GitHub issues in every sub-project that uses paid APIs, asking each to implement a thin **reporting client** (declare intent → report actual → expose a kill endpoint → pull back authoritative expected-cost records).

---

## 1. Cloud account inventory (findings)

### 1.1 Accounts that incur usage-based API billing (monitor these)

| # | Cloud / provider | Account / project ID | Owner repo(s) | Paid APIs / resources | Existing cost tracking |
|---|---|---|---|---|---|
| A | **GCP** | `your-hub-project` | `your-org/AIRichardMoon` | Gemini Developer API (token-priced), Cloud Run, Firestore, Pub/Sub, Cloud Storage, Cloud Build, Cloud Functions | `backend/app/costs.py` (GeminiCostTracker → Firestore `api_costs`), `backend/app/provider_costs.py` (free-tier guardrails), `backend/app/rate_limit.py` |
| B | **GCP** | `your-gcp-project-1` (a.k.a. quantum_aikido / your-project-6) | `your-org/WorldStudioFinder`, `your-org/your-repo-6`, `your-org/your-security-repo` (UnusedOS) | Google Places (New) $0.035/call, Geocoding, Knowledge Graph, Outscraper, SerpAPI, Hunter.io, Snov.io, Apollo.io, NeverBounce, ZeroBounce, SendGrid, Gemini, Brave Search, PhantomBuster, Facebook Graph, Compute Engine VMs, GCS | `src/costs/provider_cost_tracker.py`, `src/costs/gemini_cost_tracker.py`, `src/utils/api_usage.py`, `src/utils/api_usage_alerts.py` (SQLite `pipeline.db`), `scripts/summarize_cloud_spend.py` |
| C | **GCP** | `your-gcp-project-2` | `your-org/WorldStudioFinder` (`cloud-auth.sh` default), `your-org/your-repo-6` | Same API set as B (shared scraper stack) | Same as B |
| D | **GCP** | `your-deprecated-project-id` (your-deprecated-project-label, deprecated) | `your-org/WorldStudioFinder` | Possibly residual Places/Geocoding calls | Same code as B; verify still in use |
| E | **OpenStack (your-openstack-provider)** | project `your-openstack-project-id` (id `e836b162…`), regions `your-region-1` + `your-region-2` | `your-org/FieldWorker`, `your-org/FieldAppAndroid` | Compute instances, volumes, snapshots (usage-based) | **None** — no cost tracking today |
| F | **Cloudflare** | zone `your-domain.com`, Pages project `your-pages-project` | `your-org/your-security-repo` | Pages bandwidth, redirect rules (mostly free tier) | None |
| G | **Cloudflare R2** (optional) | per-user account | `your-org/ClipQuotes` | Object storage (S3-compatible) if rclone R2 configured | None |
| H | **HuggingFace** | per-user token | `your-org/ClipQuotes` | `pyannote` diarization models (free tier; paid inference possible) | None |

### 1.2 Accounts excluded (no usage-based API billing)

| Repo | Why excluded |
|---|---|
| `your-org/your-domain.com` | PHP frontend on shared hosting (your-shared-hosting.com); no paid APIs. Proxies to AIRichardMoon backend, which is monitored. |
| `your-org/your-domain.com` | Static HTML site on your-shared-hosting.com; no APIs. |
| `your-domain.com` | PHP landing page on your-shared-hosting.com; no git repo, no paid APIs. |
| `your-org/your-repo-6-merged` | Local-only SQLite merge tool; no cloud/API calls. |
| `WorldStudioFinder-bugfix`, `WorldStudioFinder-fix-*` | Development copies of WorldStudioFinder (no separate `.git`); share account B/C. |
| `studio-discovery-dev` | Stripped copy of your-project-6; shares account B/C. Covered by the WorldStudioFinder/your-project-6 ticket. |

### 1.3 Cross-cutting note on B/C/D

WorldStudioFinder, your-project-6, and Security/UnusedOS all touch the same GCP projects (`your-org-project-id…`, `your-gcp-project-2`). They are effectively one billing surface with multiple codebases writing to it. CloudManagement should register the **GCP project** once and tag each intent/actual record with the **source repo + job_id** so cost can be attributed back to the responsible codebase even when they share a billing account.

---

## 2. Design: the intent/actual protocol

### 2.1 Goals

- **Before** a job makes API calls, it declares intent (expected calls, expected cost, time window, kill descriptor).
- **After** (or incrementally during) the job, it reports actuals.
- CloudManagement **validates** actual vs intent, detects overruns and runaway loops, and can **kill the specific job**.
- CloudManagement is the **source of truth** for expected costs; projects pull authoritative expected-cost records back from CloudManagement to update their local cost trackers.

### 2.2 Wire protocol (HTTP/JSON, shared-secret auth)

All endpoints live on the CloudManagement service. Auth: `Authorization: Bearer <CLOUDMANAGEMENT_REPORT_TOKEN>` (per-project token stored in each project's secret store; rotates via CloudManagement admin endpoint).

#### 2.2.1 Declare intent (pre-call)

```
POST /api/v1/intent
{
  "project_id": "your-project-2",          // logical project key (registry key)
  "source_repo": "your-org/WorldStudioFinder",
  "job_id": "scrape-phase1-2026-07-24-001",   // caller-generated, unique per run
  "job_name": "Phase 1 scraping — US studios",
  "provider": "google_places",                // API provider key
  "api": "places.text_search",                // specific endpoint
  "expected_calls": 500,
  "expected_cost_usd": 17.50,                 // caller's estimate using its pricing config
  "expected_tokens": null,                    // for token-priced APIs (Gemini)
  "rate_limit_rpm": 100,
  "window_start": "2026-07-24T16:00:00Z",
  "window_end": "2026-07-24T18:00:00Z",
  "kill": {                                   // how CloudManagement can stop THIS job
    "type": "http_callback",
    "url": "https://app.your-project-2.example/admin/kill-job",
    "method": "POST",
    "headers": {"X-Kill-Token": "<job-scoped secret>"}
  },
  "metadata": {}                              // free-form
}
```

Response:
```
{
  "intent_id": "int_abc123",
  "approved": true,                           // false if project over budget / kill-switch armed
  "budget_remaining_usd": 82.50,
  "quota_remaining_calls": 5750,
  "kill_switch_armed": false,
  "warnings": []
}
```

If `approved: false`, the caller SHOULD abort or proceed knowing the kill switch may fire. CloudManagement records the intent regardless (so an unapproved-but-proceeded job is still killable and auditable).

#### 2.2.2 Report actual (post-call or incremental)

```
POST /api/v1/actual
{
  "intent_id": "int_abc123",                  // ties back to the intent
  "project_id": "your-project-2",
  "job_id": "scrape-phase1-2026-07-24-001",
  "provider": "google_places",
  "api": "places.text_search",
  "actual_calls": 487,
  "actual_cost_usd": 17.05,                   // caller's computed cost
  "actual_tokens": null,
  "status": "completed",                      // running | completed | failed | killed
  "started_at": "2026-07-24T16:00:00Z",
  "ended_at": "2026-07-24T17:42:00Z"
}
```

Incremental reports (status `running`) let CloudManagement detect overruns mid-job and kill before completion.

#### 2.2.3 Pull authoritative expected costs (project → CloudManagement)

```
GET /api/v1/expected-costs/<project_id>
→ {
  "project_id": "your-project-2",
  "updated_at": "2026-07-24T18:00:00Z",
  "providers": {
    "google_places": {
      "unit_cost_usd": 0.035,                 // CloudManagement's authoritative pricing
      "free_tier_remaining_calls": 6250,
      "free_tier_reset": "2026-08-01T00:00:00Z",
      "expected_remaining_monthly_usd": 45.00,
      "calibration_delta": -0.002             // reconciliation correction vs self-reported
    },
    "gemini": { "input_cost_per_million_usd": 0.10, "output_cost_per_million_usd": 0.40, ... }
  }
}
```

Each project's local cost tracker fetches this on a schedule (e.g., every 15 min) and updates its local pricing/expected-cost records. This is how "CloudManagement updates API expected costs records in the individual projects" — via pull, which is robust to firewalls/NAT (projects are often behind shared hosting or on-demand VMs).

#### 2.2.4 Kill a job (CloudManagement → project)

CloudManagement invokes the `kill` descriptor from the intent. For `http_callback`:
```
POST <url>   (with the job-scoped headers)
{ "intent_id": "int_abc123", "reason": "actual_exceeds_intent_1.5x", "threshold": 1.5 }
```
The project's kill endpoint stops the job (cancel Celery task, stop systemd unit, set a kill flag the job polls, etc.) and returns `{"killed": true, "job_id": "..."}`.

For GCP-native jobs the `kill.type` can instead be `cloud_run` / `cloud_scheduler` / `gce` / `gke` and CloudManagement executes the existing GCP action directly (no project endpoint needed). For OpenStack, `kill.type: "openstack"` with `instance_id` + `region`. This keeps the per-job kill capability where GCP already has it, while adding a portable `http_callback` for everything else.

### 2.3 Detection rules (when CloudManagement kills a job)

Extends the existing `poller.py` rule set. Per job, evaluated on each `/poll` tick and on each actual report:

1. **actual_exceeds_intent** — `actual_calls > expected_calls * INTENT_VARIANCE_THRESHOLD` (default 1.2) OR `actual_cost > expected_cost * INTENT_VARIANCE_THRESHOLD`. First-line rule; catches a job that blew past its own estimate.
2. **rate_cap_exceeded** — observed calls/min > `rate_limit_rpm` from the intent. Catches retry storms.
3. **project_budget_exceeded** — project's rolling MTD spend (from reconciliation tier + summed actuals) > `budget_amount_usd`. Project-wide; kills all running jobs for the project.
4. **quota_exceeded** — existing GCP Cloud Monitoring rule (kept for GCP projects without intent reporting yet).
5. **reconciliation_variance** — |billed − self-reported| / billed > `RECONCILIATION_VARIANCE_THRESHOLD` (default 0.15). Does NOT kill; raises an accuracy alert and recalibrates the project's pricing config.

Rules 1–3 are provider-agnostic and run off the intent/actual store. Rule 4 is the legacy GCP backstop. Rule 5 is the accuracy backstop.

### 2.4 Data stores (new Firestore collections)

CloudManagement already uses Firestore `accounts`. Add:

- **`api_intents`** — one doc per intent. Keyed by `intent_id`. Fields: all of §2.2.1 plus `status`, `approved`, `created_at`, `updated_at`.
- **`api_actuals`** — one doc per actual report (supports incremental: multiple docs per `intent_id` with a sequence number). Fields: all of §2.2.2 plus `reconciled_cost_usd`, `reconciled_at`.
- **`kill_events`** — audit log of every kill invocation: `intent_id`, `job_id`, `reason`, `rule_fired`, `kill_type`, `result`, `timestamp`.
- **`expected_costs`** — one doc per `(project_id, provider)` with the authoritative pricing + remaining budget + calibration delta. Updated by the reconciliation job and by each actual report.

YAML-file fallback (for local dev/tests) mirrors these as flat files under `config/`, matching the existing `accounts.yaml` pattern.

---

## 3. Design: multi-cloud + per-job kill in CloudManagement

### 3.1 Provider abstraction

New module `providers/` with a base interface and concrete implementations:

```python
# providers/base.py
class CostProvider(ABC):
    @abstractmethod
    def fetch_billed_costs(self, since: datetime, until: datetime) -> list[BilledCost]:
        """Reconciliation tier: actual billed amounts from the cloud."""
    @abstractmethod
    def kill_job(self, kill_descriptor: dict, reason: str) -> KillResult:
        """Execute a per-job kill action."""

# providers/gcp.py — wraps existing main.py actions + BigQuery billing export
# providers/openstack.py — Nova compute stop + Ceilometer/Gnocchi metering
# providers/cloudflare.py — GraphQL analytics for R2/Pages bandwidth
# providers/http_callback.py — generic POST to a project's kill endpoint (portable)
# providers/registry.py — maps (provider_key) → CostProvider instance
```

`execute_killswitch()` in `main.py` is refactored to: look up the job's `kill` descriptor → route to the right `CostProvider.kill_job()`. The existing project-wide GCP actions remain as the fallback when a job has no `kill` descriptor (backwards compatible).

### 3.2 Per-job kill configuration

Each registered account gains optional `jobs` config in the registry:

```yaml
# config/accounts.example.yaml (extended)
accounts:
  - project_id: your-project-2
    cloud: gcp
    gcp_project_id: your-gcp-project-1
    billing_account_id: 01AB-23CD-EF45
    owner_email: owner@example.com
    budget_amount_usd: 50.0
    quota_rpm_cap: 6000
    report_token_secret: "sm://cloudbilling-report-token-your-project-2"
    jobs:
      - job_id_prefix: "scrape-phase1-"
        kill: {type: cloud_scheduler, job: phase1-cron, location: us-central1}
      - job_id_prefix: "gemini-enrich-"
        kill: {type: http_callback, url: "https://...", method: POST, headers: {...}}
  - project_id: your-project-3
    cloud: openstack
    openstack_project: "your-openstack-project-id"
    openstack_regions: [your-region-1, your-region-2]
    owner_email: ...
    budget_amount_usd: 20.0
    jobs:
      - job_id_prefix: "gps-tracker-"
        kill: {type: openstack, instance_id: "...", region: your-region-1}
```

The `kill` descriptor on the **intent** (§2.2.1) takes precedence over the registry default, so a job can self-describe its kill mechanism at declaration time. The registry entry is the fallback/allowlist for jobs that don't declare one.

### 3.3 Reconciliation job (accuracy tier)

New Cloud Scheduler job `/reconcile` (daily + on-demand):
1. For each registered account, `CostProvider.fetch_billed_costs(since=now-48h, until=now-24h)`.
2. Join billed costs to `api_actuals` by `(project_id, provider, api, day)`.
3. Compute variance per `(project, provider)`. If > threshold, write a recalibration to `expected_costs` (adjust `unit_cost_usd` and `calibration_delta`) and emit an accuracy alert.
4. This is the only path that touches cloud billing APIs — keeping it off the hot path means real-time monitoring never blocks on a 24h-lag export.

### 3.4 Dashboard (clean interface)

Extend `dashboard.py` + `templates/dashboard.html`:

- **Project cards** — budget ring, MTD spend, running jobs count, kill-switch armed flag.
- **Jobs table** — per running job: job_name, provider/api, expected vs actual calls, expected vs actual cost, variance %, status, kill button (manual override).
- **Provider view** — aggregate across projects per API (e.g., all Gemini usage, all Google Places usage) with free-tier progress bars.
- **Reconciliation view** — billed vs self-reported per provider, variance, last reconciliation timestamp, calibration deltas.
- **Kill history** — chronological log with reason/rule fired, reversible actions highlighted (e.g., "API key revoked — recoverable for 30 days").
- All existing endpoints (`/api/summary`, `/api/daily`, etc.) kept; new endpoints under `/api/v1/` for the protocol, `/api/v2/` for the richer dashboard views.

---

## 4. Implementation phases

### Phase 1 — CloudManagement core extensions (this repo)

1. `providers/base.py`, `providers/gcp.py` (extract existing actions), `providers/http_callback.py`.
2. `intent.py` — Flask blueprint with `/api/v1/intent`, `/api/v1/actual`, `/api/v1/expected-costs/<project>`.
3. Firestore collections `api_intents`, `api_actuals`, `expected_costs`, `kill_events` (+ YAML fallback).
4. Extend `registry.py` `Account` with `cloud`, `gcp_project_id`, `openstack_*`, `report_token_secret`, `jobs`.
5. Refactor `execute_killswitch()` to route via `providers/`.
6. Add detection rules 1–3 to `poller.py`; keep rule 4.
7. `/reconcile` endpoint + Cloud Scheduler job + `providers/openstack.py`, `providers/cloudflare.py`.
8. Dashboard v2 views + jobs table + reconciliation view.
9. Tests: `test_intent.py`, `test_providers.py`, `test_reconcile.py`; extend `test_poller.py`, `test_registry.py`, `test_dashboard.py`.
10. Update `README.md`, `.env.example`, `terraform/`, `scripts/register_project.py` for new fields.

### Tickets opened (2026-07-24)

| Repo | Issue |
|---|---|
| `your-org/AIRichardMoon` | https://github.com/your-org/AIRichardMoon/issues/44 |
| `your-org/WorldStudioFinder` | https://github.com/your-org/WorldStudioFinder/issues/162 |
| `your-org/FieldWorker` | https://github.com/your-org/FieldWorker/issues/231 |
| `your-org/FieldAppAndroid` | https://github.com/your-org/FieldAppAndroid/issues/1 |
| `your-org/your-security-repo` | https://github.com/your-org/your-security-repo/issues/73 |
| `your-org/ClipQuotes` | https://github.com/your-org/ClipQuotes/issues/42 |

### Phase 2 — Shared reporting client

New tiny package `clients/cloudbilling_reporter/` (Python; PHP and JS ports documented in the ticket template):
- `declare_intent(...)` → POST `/api/v1/intent`.
- `report_actual(...)` → POST `/api/v1/actual`.
- `pull_expected_costs(project_id)` → GET `/api/v1/expected-costs/<project>`.
- `kill_endpoint_handler` — a Flask/PHP handler blueprint implementing the `http_callback` kill contract, drop-in for each project.
- Retry/backoff, offline buffer (queue intents/actuals locally if CloudManagement unreachable, flush on reconnect).

### Phase 3 — Sub-project integrations (via tickets below)

Each project implements: declare intent before API batches, report actual after, expose kill endpoint, pull expected costs into its local tracker. Existing cost trackers (AIRichardMoon's `GeminiCostTracker`, WorldStudioFinder's `provider_cost_tracker`) become the integration points — they already compute per-call costs, so they just gain a "report to CloudManagement" sink.

### Phase 4 — Reconciliation tuning

After 2 weeks of dual-running, tune `INTENT_VARIANCE_THRESHOLD` and `RECONCILIATION_VARIANCE_THRESHOLD` per provider based on observed variance. Lock down pricing configs in `expected_costs`.

---

## 5. Tickets to open

One ticket per repo that has paid APIs. Each ticket uses a shared template (§5.1) plus repo-specific sections (§5.2–§5.7). I will open these via the GitHub MCP `create_issue` tool upon approval of this plan.

### 5.1 Shared ticket template

> **Title:** Integrate with CloudManagement intent/actual API usage reporting
>
> **Body:**
> CloudManagement (`your-org/CloudManagement`) is being extended into a multi-cloud API billing monitor. To enable real-time cost observation and per-job kill capability, this repo needs to implement a thin reporting client.
>
> **What to implement:**
> 1. **Declare intent before API batches.** Before any code path that makes paid API calls (a scrape run, a Gemini batch, an email-verify batch), POST an intent to CloudManagement `POST /api/v1/intent` with: `project_id`, `source_repo`, `job_id`, `provider`, `api`, `expected_calls`, `expected_cost_usd`, `rate_limit_rpm`, time window, and a `kill` descriptor (how CloudManagement can stop this job). Do not proceed if `approved: false`.
> 2. **Report actuals after (and incrementally during long jobs).** POST to `/api/v1/actual` with the actual call count, cost, tokens, and status. For long-running jobs, send periodic `status: running` reports so CloudManagement can detect overruns mid-job.
> 3. **Expose a job-kill endpoint.** Implement an authenticated endpoint (e.g., `POST /admin/kill-job`) that CloudManagement can call to stop a specific job. It should cancel the in-flight work, mark the job killed, and return `{"killed": true, "job_id": ...}`. The `kill` descriptor in the intent points here.
> 4. **Pull authoritative expected costs.** Every 15 min, GET `/api/v1/expected-costs/<project_id>` and update the local cost tracker's pricing/expected-cost records with CloudManagement's authoritative values (which include reconciliation corrections from cloud billing export).
> 5. **Wire into the existing cost tracker** (if present) so the per-call cost computation already in this repo feeds the actual reports — don't duplicate cost math.
>
> **Shared client:** a Python reference client lives at `clients/cloudbilling_reporter/` in the CloudManagement repo. PHP/JS ports: see the protocol spec in `CloudManagement/docs/multi-cloud-monitoring-plan.md` §2.2.
>
> **Auth:** a per-project bearer token (`CLOUDMANAGEMENT_REPORT_TOKEN`) will be provisioned in Secret Manager / local config. Do not commit the token.
>
> **Why:** this enables CloudManagement to (a) show real-time expected vs actual API cost per job across all our projects, (b) kill the specific job that is accumulating cost rather than nuking the whole project, and (c) keep each project's local cost estimates accurate by feeding back reconciliation-corrected pricing.
>
> **Acceptance:**
> - Every paid API call path declares intent and reports actual.
> - Kill endpoint works and is tested.
> - Local expected-cost records update from CloudManagement's pull endpoint.
> - No tokens committed; no secrets read by CloudManagement.

### 5.2 `your-org/AIRichardMoon`

Extra sections:
- **Integration point:** `backend/app/costs.py` (`GeminiCostTracker`) already records per-call token costs to Firestore `api_costs`. Add a CloudManagement reporter sink alongside it: each `record()` call also POSTs an actual report keyed by the coaching-session `job_id`. Declare intent in `backend/app/service.py` before the Gemini call in `generate_response()`.
- **Kill endpoint:** add `POST /v1/admin/kill-session` to `backend/app/main.py` (admin-auth via `DASHBOARD_ADMIN_KEY`) that cancels the in-flight generation and marks the session killed.
- **Provider:** `gemini` (token-priced). Also declare intent for Twilio SMS if `TWILIO_ENABLED=true`.
- **Project id for CloudManagement:** `your-project-1` (logical) → GCP project `your-hub-project`.
- **Expected cost model:** use `GEMINI_INPUT_COST_PER_MILLION_USD` / `GEMINI_OUTPUT_COST_PER_MILLION_USD` from `backend/app/config.py` for the intent estimate; CloudManagement will reconcile against the Gemini API's own usage dashboard via the reconciliation tier.

### 5.3 `your-org/WorldStudioFinder`

Extra sections:
- **Integration point:** `src/costs/provider_cost_tracker.py` and `src/utils/api_usage.py` already log per-call usage to SQLite `pipeline.db`. Add a CloudManagement reporter that mirrors each `log_api_usage()` call to `/api/v1/actual`. Declare intent at the start of each scrape phase (`src/scrapers/` entrypoints) and each Gemini batch (`src/ai/gemini_batch_client.py`).
- **Providers to report:** `google_places`, `google_geocoding`, `google_kg`, `outscraper`, `serpapi`, `hunter`, `snov`, `apollo`, `neverbounce`, `zerobounce`, `sendgrid`, `gemini`, `brave`, `phantombuster`, `facebook_graph`, `here`, `azure_maps`, `opencage`, `geoapify`.
- **Kill endpoint:** add `POST /admin/kill-job` to `web/app.py` (Flask) that sets a kill flag in the SQLite `api_call_log` the running scraper polls, plus stops the systemd unit if the job is a systemd service.
- **Job scheduling:** systemd services (`scraper-phase1.service`, `global-monitor.service`) — the `kill` descriptor should be `{type: http_callback, url: .../admin/kill-job}` so CloudManagement doesn't need SSH access to the VM.
- **GCP projects:** register `your-gcp-project-1` and `your-gcp-project-2` separately in CloudManagement; tag each intent with which project the calls bill to.
- **Note:** `your-org/your-repo-6` shares this stack and GCP project — coordinate so both repos use the same `project_id` and kill endpoint convention. A single ticket may cover both; open in WorldStudioFinder (active repo) and cross-reference your-project-6.

### 5.4 `your-org/your-repo-6`

Cross-reference the WorldStudioFinder ticket. If your-project-6 is still independently deployed, it needs the same reporter + kill endpoint. Confirm with owner whether your-project-6 is still active or superseded by WorldStudioFinder before implementing.

### 5.5 `your-org/FieldWorker`

Extra sections:
- **No existing cost tracker** — this is greenfield. Add a small `cloudbilling_reporter.py` module that declares intent before OpenStack instance operations and reports actual instance-hours/storage daily.
- **Provider:** `openstack` (compute hours, volume GB-hours, snapshot GB). CloudManagement's `providers/openstack.py` will reconcile against your-openstack-provider metering.
- **Kill endpoint:** `POST /admin/kill-job` in the Flask app that shuts down the named OpenStack instance via the existing `openstack_shutdown_instances.sh` logic.
- **Note:** this repo has no paid *APIs* in the HTTP sense, but OpenStack compute is usage-based and the user wants it monitored. Intent here = "I'm starting instance X for an expected Y hours at $Z/hr"; actual = "instance ran Y' hours, cost $Z'".

### 5.6 `your-org/FieldAppAndroid`

Shares the FieldWorker backend. If the Android app makes no direct paid API calls (it talks to the FieldWorker backend), then the FieldWorker ticket covers it. Open a lightweight ticket asking to confirm no direct paid API calls from the Android client, and to wire the kill endpoint if any background work runs on-device.

### 5.7 `your-org/your-security-repo`

Extra sections:
- **Scope:** only the UnusedOS GCP compute path is usage-based. Cloudflare Pages/redirects are free-tier. GithubLeak/Truffle use free-tier GitHub API.
- **Integration:** declare intent before UnusedOS Terraform test VM creation; report actual VM-hours after teardown. Kill endpoint = the existing teardown script invoked via HTTP.
- **Priority:** low (on-demand test VMs, small spend). Still worth wiring for completeness.

### 5.8 `your-org/ClipQuotes`

Extra sections:
- **Scope:** HuggingFace (free tier, paid if using inference endpoints) and optional Cloudflare R2 storage.
- **Integration:** declare intent before HuggingFace model downloads/inference; report actual tokens/requests. For R2, report storage GB daily.
- **Priority:** low. Implement only if HF paid inference or R2 is actually in use.

### 5.9 Repos excluded from tickets

`your-org/your-domain.com`, `your-org/your-domain.com`, `your-domain.com`, `your-org/your-repo-6-merged`, and the `WorldStudioFinder-fix-*`/`bugfix`/`studio-discovery-dev` copies get **no ticket** — no paid APIs or are dev copies covered by their parent repo's ticket. Documented in §1.2.

---

## 6. Accuracy safeguards

- **Two-tier model:** real-time self-reports + lagged cloud billing reconciliation. Neither tier is trusted alone.
- **Per-provider pricing in CloudManagement registry**, not hardcoded in projects. Projects fetch authoritative pricing from CloudManagement; CloudManagement recalibrates pricing from reconciliation.
- **Variance thresholds tunable per provider** (default 1.2x intent, 0.15 reconciliation). Phase 4 tunes from observed data.
- **Idempotent actual reports:** re-reporting the same `intent_id`+sequence updates rather than duplicates.
- **Offline buffering in the client:** if CloudManagement is unreachable, the reporter queues locally and flushes on reconnect — no lost actuals, no blocked jobs.
- **Kill is reversible-by-default:** `http_callback` kills cancel the job but don't destroy resources; GCP API-key revoke stays soft-delete (30-day recovery); billing shutoff stays off by default.

## 7. Clean-interface safeguards

- Dashboard v2 is additive — existing `/dashboard` and `/api/*` endpoints keep working.
- Jobs table is the primary view: one row per running job, sortable by variance %, with a kill button.
- No cloud credentials in the browser; all provider actions server-side.
- Mobile-friendly cards (the dashboard is often checked from a phone).

## 8. Risks & open questions

### 8.1 GCP structure (discovered 2026-07-24 via gcloud)

The GCP footprint is **fragmented across two billing accounts, two Google accounts, and one partial org** — not the single-org/folder model the existing CloudManagement README assumes.

| Project ID | Owner account | Billing account | Parent | Notes |
|---|---|---|---|---|
| `your-hub-project` | `owner@example.com` | `01AB-23CD-EF45` | none (standalone) | CloudManagement hub deployment target |
| `your-gcp-project-1` | `owner@example.com` | `01AB-23CD-EF45` | org `123456789012` | WorldStudioFinder/your-project-6; shares billing with your-hub-project |
| `your-gcp-project-2` | `owner@example.com` | `01AB-23CD-EF45` | none (standalone) | Scraper stack alt project; different billing account |
| `your-deprecated-project-id` | unknown (not accessible by any tested account) | unknown | unknown | your-deprecated-project-label, deprecated; may be deleted |

**Implications for the design:**
1. **No single org/folder IAM inheritance.** Only `your-org-project-id` is under an org (`123456789012`); `your-hub-project` and `your-gcp-project-2` are standalone. The runtime SA needs **per-project IAM grants** on the standalone projects, and either org-level or per-project IAM on `your-org-project-id`. The Terraform must grant roles per-project, not per-org.
2. **Two billing accounts** → two BigQuery billing export datasets (one per billing account): `01AB-23CD-EF45` (your-hub-project + your-org-project-id) and `01AB-23CD-EF45` (your-gcp-project-2 + others). The dashboard's `_billing_table_for()` already supports per-billing-account table paths; the registry must store the billing account per project.
3. **Two owner accounts** (`owner@example.com`, `owner@example.com`) → the deployer must have access to both, or IAM grants on `your-org-project-id` must be made by `owner@example.com` (or via the org admin). The runtime SA is created in `your-hub-project` and granted per-project roles by each project's owner.
4. **`your-deprecated-project` (your-deprecated-project-label)** is not accessible by any tested account — likely deleted or owned by an untested account. Treat as out-of-scope unless the user can access it; do not register it in CloudManagement.

### 8.2 Other risks

1. **OpenStack metering access** — does your-openstack-provider expose Ceilometer/Gnocchi? If not, reconciliation for OpenStack falls back to self-reports only (accuracy tier degraded for account E). Need to confirm with the provider.
2. **PHP projects** — your-domain.com is excluded (no paid APIs), but if it ever proxies paid API calls directly, it'll need a PHP port of the reporter. The protocol spec is HTTP/JSON so a PHP client is straightforward.
3. **Token rotation** — per-project `CLOUDMANAGEMENT_REPORT_TOKEN` rotation needs an admin endpoint + a rotation runbook. Add to Phase 1.
4. **Multi-account gcloud auth** — the deployer needs `gcloud auth login` on both `owner@example.com` (for your-hub-project + your-gcp-project-2) and `owner@example.com` (for your-org-project-id) to grant IAM. Document this in the deploy runbook.

---

## 9. Decisions (approved 2026-07-24)

- [x] Approve the two-tier (real-time intent/actual + lagged reconciliation) design.
- [x] Approve the per-job kill via `http_callback` + GCP/OpenStack native descriptors.
- [x] Approve the ticket list (§5.2–§5.8) and open them via GitHub.
- [x] `your-org/your-repo-6` is superseded by WorldStudioFinder — ticket only in WorldStudioFinder.
- [x] GCP org/billing structure discovered via gcloud — see §8.1 (fragmented, per-project IAM, two billing accounts).
- [x] CloudManagement service will be deployed to `your-hub-project` GCP project (Cloud Run).
