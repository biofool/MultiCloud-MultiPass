# Upstream convergence status

This repo is a downstream distribution of CloudManagement. The canonical
implementation of the hub services is maintained upstream in
`biofool/CloudManagement`; this repo tracks it for the pieces it shares
and keeps a frozen copy of the rest so it stays standalone-runnable.

## What "convergence" means here

The upstream rearchitecture (upstream `docs/rearchitecture-services.md`)
splits the hub monolith into independently deployable services. This repo
does **not** run that service fleet — it is not operated as the reporting
hub for the upstream deployment, and nothing in the upstream fleet
reports to a hub deployed from this repo. Convergence therefore takes the
conservative form: shared components are aligned on the canonical
implementation, and the diverged server-side modules are frozen as a
maintenance-mode snapshot rather than deleted — external users can still
self-host this repo as a standalone hub.

## Converged (canonical implementation in use)

- `secret_loader.py` — thin shim over the vendored
  `packages/biofool-secrets` snapshot. `USE_SECRET_MANAGER` stays opt-in.
- `cloud_management_client/` — canonical client, re-vendored at v0.17.1
  (VENDORED-FROM stamp in `cloud_management_client/__init__.py`).

## Frozen snapshot (maintenance mode)

These modules form the standalone hub (`main.py` mounts the blueprints;
`admin_routes.py` lazily imports `poller`; the `intent_*` modules are
re-exported through the `intent.py` facade). Each carries a
"Maintenance-mode snapshot" note in its module docstring:

- `main.py`, `alerts.py`, `dedup.py`, `killswitch_actions.py`
- `admin_routes.py`, `poller.py`, `registry.py`
- `intent.py` + `intent_models`, `intent_storage`, `intent_auth`,
  `intent_detection`, `intent_kill`, `intent_routes`
- `dashboard.py`, `inventory.py` (+ `templates/`)

Policy for these files: security fixes and upstream backports only. Do
not extend or refactor them here — make the change upstream and let it
flow back through the normal fork sync.

## Known divergence from upstream

| Area | This repo | Upstream |
|------|-----------|----------|
| intent/actual | split across `intent.py` facade + 6 `intent_*` modules | single monolithic `intent.py` |
| `registry.py` | ~173 lines, account registry only | ~509 lines, additional store/backends |
| kill switch | `main.py` + `killswitch_actions`/`alerts`/`dedup` | `alert_pipeline.py` + `killswitch_routes.py` + `services/killswitch` |
| admin/poller | `admin_routes.py` + `poller.py` in the monolith | `services/poller` container |
| dashboard/inventory | `dashboard.py`, `inventory.py` in the monolith | `services/dashboard`, `services/inventory` |

The divergence is accepted: the frozen snapshot is the standalone
product; the service split is the upstream deployment architecture.

## Removal candidates (needs owner decision)

- `deploy_quantum_aikido_terraform.sh` — wrapper that invokes a deploy
  script in a sibling repo checkout. Zero consumers inside this repo;
  kept because it is listed in the wiki's key-files map and may be used
  as an operator convenience script.
