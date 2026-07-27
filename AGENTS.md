# AGENTS.md — CloudManagement

## Rules for AI Agents

### Validation requests — do not change code

When the user asks to validate or check a conclusion, do NOT start changing
code or making edits. Investigate, verify the conclusion against the actual
state of the codebase/data, and report findings only. If the conclusion is
clearly invalid, state that and wait for instructions. Only make code changes
when the user explicitly asks for a fix or implementation.

### Never read secrets files

NEVER read, cat, print, or otherwise access `.env`, `.env.secrets`,
`.env.local`, `*.key`, `credentials*.json`, `service-account*.json`, or any
file containing API keys, tokens, or passwords. If you need a credential
value to complete a task, ask the user to provide it directly or set it as
an environment variable.

### Never commit or log secrets

Never commit credentials, private keys, tokens, or customer-identifying data
to git. Never log secrets to files, stdout, or dashboards. Treat scripts that
touch infrastructure as potentially production-impacting; prefer dry-run flags
and documented env vars. The root `.gitignore` excludes `.env`, `*.key`,
`*.pem`, `credentials.json`, and `cookies.txt` — keep those entries when
editing `.gitignore`.

### Never fail silently

Every exception, auth failure, or unavailable dependency must be logged at
WARNING or ERROR level with a specific message. Silent `pass` or bare
`except: return` blocks are forbidden. If a subsystem degrades gracefully,
log *why* it was disabled and surface the status to the UI, dashboard, or
log file.

### No backslash line continuations in shell commands

Write commands on a single line — backslash continuations break copy-paste.

### One-off fix scripts (workflow convention)

When building repair/fix scripts:

1. **Store scripts in `scripts/fix/`**
2. **Always support `--dry-run`**
3. **Write results to `data/audit/`** — JSON output with per-record details
4. **Support `--limit` and `--offset`** for testing on subsets

### Prefer stored data files over hardcoding

NEVER hardcode arrays or lookup tables with more than 15 items directly in
source files. Prefer reading from a JSON/YAML/TOML data file whenever
possible.

---

## Project-specific: CloudManagement

### Overview

Multi-cloud cost kill switch and intent/actual reporting hub. One hub
project watches every teammate's cloud project and can automatically stop
runaway spend. Sub-projects declare expected API usage before making calls
and report actuals after.

### Key files

- `main.py` — Cloud Run service entry point (poll, budget alerts, dashboard, inventory, intent/actual)
- `poller.py` — quota-based polling loop
- `registry.py` — monitored account registry (YAML or Firestore backend)
- `intent.py` — kill-switch intent/actual evaluation
- `inventory.py` — unified cloud resource inventory (reads accounts + terraform state)
- `dashboard.py` — Flask dashboard blueprint
- `paths.py` — project-root path resolver (all config/data paths resolve via this module)
- `cloud_management_client/` — pip-installable client for sub-projects (intent/actual reporting)
- `providers/` — cloud provider adapters (GCP, OpenStack, Cloudflare, HTTP callback)
- `config/` — YAML config files (accounts, intents, kill events)
- `terraform/` — GCP infrastructure provisioning
- `docs/PRD.md` — CloudManagement PRD (scope, inventory schema, job-placement policy)

### Commands

```bash
# Setup
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Run locally
.venv/bin/python main.py

# Tests
.venv/bin/python -m pytest tests/ -v

# Terraform
cd terraform && terraform init && terraform plan
```

### Security notes

- `cloud_auth/` and `config/accounts.yaml` contain secrets — gitignored, never commit.
- `config/api_actuals.yaml`, `config/api_intents.yaml`, `config/kill_events.yaml` are runtime state — gitignored.
- This project touches cloud billing and can shut down projects — treat all
  kill-switch operations as production-impacting. Always test with `--dry-run`
  where available.
