# MultiCloud-MultiPass — AI Context Index

> **Revision:** e65660aca996d3aaa5cf22dd2686afa398700f55 (main)
> **Last verified:** 2026-08-22
> **Staleness check:** compare `git rev-parse HEAD` to the revision above; if
> different, re-run affected stages (see manifest.yaml pass plan).

## What this is

MultiCloud-MultiPass (formerly CloudManagement / CloudBilling) is a
**multi-cloud cost kill switch and intent/actual reporting hub**. One hub
GCP project watches every teammate's cloud project and can automatically
stop runaway spend at three levels: per-job (intent/actual overrun),
per-project (quota-based), and per-billing-account (billing shutoff).

## System shape

- **Deployable unit:** single Flask app on Cloud Run (scale-to-zero, max 1
  instance). Python 3.10+. Entry point: `main.py`.
- **Data stores:** Firestore (account registry, intents, actuals, expected
  costs, kill events) in prod; YAML files in dev/test.
- **External integrations:** GCP (Cloud Run, Scheduler, Compute, GKE, BigQuery
  billing export, API Keys, Billing), OpenStack (Nova/Ceilometer), Cloudflare
  (Pages/R2), HTTP callback (portable kill).
- **Client package:** `cloud_management_client/` — stdlib-only pip package
  (v0.12.0) for sub-projects to declare intent / report actuals.
- **IaC:** Terraform in `terraform/` (GCP provider ~> 5.0).
- **GitLab mirror:** `mirror-to-gitlab.sh`, `push-to-gitlab.sh`,
  `gitlab-migrate.sh` — push to `biofool-vig/MultiCloud-MultiPass-gitlab`.

## Navigation

| Need | Go to |
|------|-------|
| Architecture overview | [architecture/system-overview.md](architecture/system-overview.md) |
| Backend service detail | [architecture/backend.md](architecture/backend.md) |
| Infrastructure (Terraform) | [architecture/infrastructure.md](architecture/infrastructure.md) |
| Data stores & schema | [architecture/data.md](architecture/data.md) |
| Component responsibilities | [components/index.md](components/index.md) |
| End-to-end runtime paths | [workflows/index.md](workflows/index.md) |
| Change-impact relationships | [change-impact/relationships.yaml](change-impact/relationships.yaml) |
| Coding conventions | [conventions/coding-patterns.md](conventions/coding-patterns.md) |
| Test commands | [testing/test-map.yaml](testing/test-map.yaml) |
| Architecture decisions | [decisions/architecture-decisions.md](decisions/architecture-decisions.md) |
| Known contradictions | [decisions/conflicts.yaml](decisions/conflicts.yaml) |
| Technical debt | [debt/register.yaml](debt/register.yaml) |
| Unresolved unknowns | [unknowns/register.yaml](unknowns/register.yaml) |

## Highest-risk areas

1. **Kill switch execution** (`main.py:execute_killswitch`,
   `killswitch_actions.py`) — can shut down cloud projects; production-impacting.
2. **Self-protection invariant** — hub project must never kill itself
   (feedback loop). Enforced in `execute_killswitch()`, `poll_all_accounts()`,
   `handle_poll_intents()`.
3. **Intent/actual auth** (`intent_auth.py`) — per-project bearer tokens;
   missing auth = unauthorized cost control.
4. **DRY_RUN default** — defaults to `true` (safe); live mode requires
   explicit env var toggles per action type.

## Quick start

See [quickstart.md](quickstart.md) for setup, run, and test commands.
