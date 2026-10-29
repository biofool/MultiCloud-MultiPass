# Architecture — MultiCloud-MultiPass

- `main.py` — Cloud Run service entry point (POST / for budget alerts,
  POST /poll for quota polling, GET /dashboard, GET /api/v1/inventory,
  POST /api/v1/intent, POST /api/v1/actual)
- `poller.py` — quota-based polling loop (5-min Cloud Scheduler)
- `registry.py` — monitored project registry (Firestore or YAML backend)
- `intent.py` — intent/actual reporting protocol
- `inventory.py` — unified cloud resource inventory (accounts + terraform state)
- `dashboard.py` — Flask dashboard blueprint
- `paths.py` — project-root path resolver
- `cloud_auth/` — per-project GCP credential management (NEVER commit)
- `cloud_management_client/` — pip-installable client for sub-projects
- `config/` — YAML config files (accounts, intents, kill events)
- `terraform/` — GCP infrastructure provisioning
- `providers/` — cloud provider adapters (GCP, OpenStack, Cloudflare, HTTP callback)

## Top-level layout
- `cloud_management_client/`
- `config/`
- `docs/`
- `providers/`
- `scripts/`
- `sql/`
- `templates/`
- `terraform/`
- `tests/`

**Key files:** `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`, `Dockerfile`, `LICENSE`, `README.md`, `TEST_PLAN.md`, `TechnicalMarketingReadMe.md`, `admin_routes.py`, `alerts.py`, `dashboard.py`, `dedup.py`, `deploy_quantum_aikido_terraform.sh`, `intent.py`
