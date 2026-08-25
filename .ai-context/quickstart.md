# Quickstart — MultiCloud-MultiPass

## System shape

Single Flask app on GCP Cloud Run (scale-to-zero, max 1 instance). Python
3.10+. Multi-cloud cost kill switch + intent/actual reporting hub. Firestore
in prod, YAML files in dev. Terraform IaC. `cloud_management_client/` pip
package for sub-projects.

## Major entry points

| Entry | File | Route/Trigger |
|-------|------|---------------|
| Budget alert handler | `main.py` | `POST /` (Pub/Sub push) |
| Quota poll | `admin_routes.py` | `POST /poll` (Cloud Scheduler every 5 min) |
| Intent overrun poll | `admin_routes.py` | `POST /poll-intents` (every 2 min) |
| Reconciliation | `admin_routes.py` | `POST /reconcile` (daily 06:00 UTC) |
| Declare intent | `intent_routes.py` | `POST /api/v1/intent` |
| Report actual | `intent_routes.py` | `POST /api/v1/actual` |
| Dashboard | `dashboard.py` | `GET /dashboard` |
| Inventory | `inventory.py` | `GET /api/v1/inventory` |
| Service info | `admin_routes.py` | `GET /` |

## Architectural boundaries

- **Hub service** (`main.py` + blueprints) — the only code that executes kills.
- **Provider adapters** (`providers/`) — pluggable per-cloud kill + billing
  fetch. GCP, OpenStack, Cloudflare, HTTP callback.
- **Client package** (`cloud_management_client/`) — stdlib-only, shipped to
  sub-projects. Never imports Flask or google-cloud-*.
- **Terraform** (`terraform/`) — provisions Cloud Run, Pub/Sub, Scheduler,
  BigQuery dataset, Firestore, budgets, IAM.

## Dependency rules

- `main.py` imports `alerts`, `dedup`, `killswitch_actions`, `registry`, and
  registers blueprints from `dashboard`, `intent`, `inventory`, `admin_routes`.
- `killswitch_actions.py` reads `main.DRY_RUN` etc. via `import main` (qualified
  access) — never `from main import DRY_RUN` (would freeze stale value).
- `intent_storage.py` / `intent_detection.py` / `intent_auth.py` access
  reload-sensitive config via `import intent as _intent_mod` (qualified).
- `cloud_management_client/` has zero deps on the hub codebase — stdlib only.

## Essential commands

```bash
# Setup
python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"

# Run locally (DRY_RUN=true by default)
.venv/bin/python main.py

# Tests (no live GCP needed — all clients mocked)
.venv/bin/python -m pytest tests/ -v

# Terraform
cd terraform && terraform init && terraform plan
```

## Highest-risk areas

1. **Kill switch** — can shut down cloud projects; always test with `DRY_RUN=true`.
2. **Self-protection** — hub must never kill itself (feedback loop).
3. **Intent auth** — per-project bearer tokens; missing auth = unauthorized control.
4. **Reconciliation** — compares billed vs self-reported costs; variance > 15%
   triggers expected-cost recalibration.

## Navigation

`index.md` → architecture/ → components/ → workflows/ → change-impact/ →
conventions/ → testing/ → source code.
