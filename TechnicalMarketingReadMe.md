# Technical Marketing Summary — MultiCloud-MultiPass

> **One-line positioning:** A multi-cloud cost kill switch and intent/actual
> reporting hub that optimizes free-tier usage across GCP, OpenStack,
> Cloudflare, and third-party APIs — stopping runaway spend in seconds
> before you pay.

<!-- exec-summary: begin -->
MultiCloud-MultiPass is the provider-agnostic evolution of the GCP Cost Kill
Switch. It watches every cloud project across a small team's portfolio,
tracks free-tier consumption in real time via an intent/actual protocol,
and can kill specific jobs, projects, or billing accounts when spend
spikes — protecting free-tier budgets across GCP, OpenStack, Cloudflare,
and HuggingFace.
<!-- exec-summary: end -->

## Target Users / Personas

| Persona | Description | Primary Need |
|---------|-------------|--------------|
| **Indie cloud developer** | Runs projects across multiple free-tier cloud accounts | Maximize free-tier usage without surprise charges |
| **Small team lead** | Coordinates cloud spending across teammates | Centralized kill switch to stop runaway spend |
| **FinOps engineer** | Tracks and optimizes multi-cloud costs | Real-time intent/actual reporting with reconciliation |
| **Project maintainer** | Builds sub-projects that call paid APIs | Declare API usage intent and report actuals to avoid overruns |

## Key Features & Capabilities

*(Grounded in `main.py`, `poller.py`, `registry.py`, `dashboard.py`, `providers/`, `tests/`, and the README.)*

- **Three-level kill switch escalation:**
  - **Per-job** (intent/actual) — kills the specific job accumulating cost in
    seconds when actual exceeds expected by 1.2×.
  - **Per-project** (quota poller) — stops all billable services in a project
    in minutes when quota spikes are detected.
  - **Per-billing-account** (budget alert) — unlinks billing as a last-resort
    nuclear option (12–24h lag).
- **Intent/actual reporting protocol** — Sub-projects declare expected API
  usage before making calls (`POST /api/v1/intent`) and report actuals after
  (`POST /api/v1/actual`). CloudManagement validates and detects overruns.
- **Real-time free-tier tracking** — Aggregates self-reported actuals across
  all projects per API, tracking free-tier burn rate in real time (faster
  than billing exports which lag 24–48h).
- **Reconciliation tier** — Daily `/reconcile` pulls actual billed costs
  from GCP BigQuery, OpenStack metering, and Cloudflare GraphQL analytics,
  cross-checks against self-reported actuals, and recalibrates.
- **Multi-cloud provider adapters** — `providers/` includes GCP, OpenStack,
  Cloudflare, and HTTP callback adapters for kill-switch actions.
- **Unified resource inventory** — `inventory.py` maintains a registry of
  every cloud project, billing account, service, and job across the portfolio.
- **Flask dashboard** — `dashboard.py` serves a web UI and JSON API
  (`GET /dashboard`, `GET /api/v1/inventory`, `GET /api/v1/expected-costs`)
  with free-tier progress bars per API.
- **Pip-installable client** — `cloud_management_client/` package for
  sub-projects to declare intent and report actuals.
- **Terraform infrastructure** — `terraform/` provisions GCP infrastructure
  (Cloud Run, Scheduler, Pub/Sub, Firestore).
- **Comprehensive test suite** — Tests for registry, providers, poller,
  killswitch, inventory, intent, dashboard, and client.

## Technical Differentiators

| Differentiator | Detail |
|----------------|--------|
| **Provider-agnostic** | Works for GCP, OpenStack, Cloudflare, and any HTTP-callback target — not GCP-only like the predecessor. |
| **Intent/actual protocol** | Real-time cost tracking via self-reporting (seconds) vs billing-export lag (24–48h). |
| **Reconciliation tier** | Cross-checks self-reported actuals against real billing data for accuracy. |
| **Per-job kill** | Kills the specific runaway job, not the whole project — surgical cost control. |
| **Free-tier optimization** | Tracks per-API free-tier consumption and routes to alternatives when exhausted. |
| **Self-service registry** | Projects register via `register_project.py` — no per-project IAM setup needed. |

## Technology Stack

- **Language**: Python 3.10+
- **Framework**: Flask (Cloud Run service)
- **Cloud**: GCP (Cloud Run, Cloud Scheduler, Pub/Sub, Firestore, BigQuery)
- **Providers**: GCP, OpenStack, Cloudflare, HTTP callback
- **IaC**: Terraform
- **Client**: pip-installable `cloud_management_client`
- **Testing**: pytest
- **License**: AGPL-3.0

## Use Cases / Scenarios

1. **Runaway loop protection** — A retry storm hammers a paid API. The
   intent/actual protocol detects the overrun in seconds and kills the
   specific job before it exhausts the free tier.
2. **Free-tier routing** — When Google Places free tier is exhausted for the
   day, the dashboard surfaces the remaining count and sub-projects
   automatically fall back to OpenCage or HERE.
3. **Multi-cloud cost visibility** — A small team with GCP, OpenStack, and
   Cloudflare accounts sees all costs in one dashboard with per-API
   free-tier progress bars.
4. **Budget backstop** — A project's spend crosses 90% of budget; the kill
   switch pauses Cloud Run, Scheduler, and GCE to prevent overage.

## Benefits / Value Proposition

- **Stop paying for mistakes** — Per-job kill in seconds catches runaway
  loops before they cost money.
- **Maximize free tiers** — Real-time tracking and routing ensure you use
  free-tier capacity fully before paying.
- **Multi-cloud visibility** — One dashboard for all cloud accounts and
  APIs across providers.
- **Accurate, not just fast** — Reconciliation tier corrects self-reporting
  drift with real billing data.
- **Surgical, not nuclear** — Kills the specific job, not the whole project,
  minimizing disruption.
- **Self-service** — Projects register themselves; no per-project IAM setup.

## Known Limitations (evidenced in the repo)

- `pyproject.toml` declares no dependencies (deps are in `requirements.txt`).
- Budget alert path has 12–24h lag (Cloud Billing limitation).
- Requires org/folder-level IAM grants for cross-project kill actions.
- `cloud_auth/` and `config/accounts.yaml` contain secrets (gitignored).

## Related Repositories

- **[biofool/CloudBilling](https://github.com/biofool/CloudBilling)** —
  The GCP-only predecessor (now superseded by MultiCloud-MultiPass).
- **[biofool/CloudManagement](https://github.com/biofool/CloudManagement)** —
  The renamed/evolved version with full inventory and PRD.
