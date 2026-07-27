# CLAUDE.md

This file provides guidance to Claude Code when working in this repository.

## Project Overview

CloudManagement — multi-cloud cost kill switch and intent/actual reporting
hub. One hub project watches every teammate's cloud project and can
automatically stop runaway spend. Sub-projects declare expected API usage
before making calls and report actuals after.

## Environment

- Shell: bash (Linux)
- Python: 3.10+ via `.venv`
- GCP: multiple projects (hub + monitored teammates)
- Terraform: infrastructure in `terraform/`

## Commands

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

## Architecture

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

## Conventions

- `cloud_auth/` and `config/accounts.yaml` contain secrets — gitignored.
- This project can shut down cloud projects — treat all kill-switch operations
  as production-impacting. Always test with `--dry-run` where available.
- Never fail silently — log every exception at WARNING/ERROR level.
- Prefer stored data files over hardcoding (>15-item lookup tables belong in
  JSON/YAML, not source).
