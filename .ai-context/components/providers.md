# Component: Provider Adapters

## Location
`providers/base.py` (84 lines), `providers/registry.py` (85 lines),
`providers/gcp.py` (~350 lines), `providers/openstack.py` (~120 lines),
`providers/cloudflare.py` (~100 lines), `providers/http_callback.py` (~80 lines)

## Responsibility
Pluggable multi-cloud cost control. Each provider implements `kill_job()` and
optionally `fetch_billed_costs()`. The registry routes kill descriptors to
the right provider based on the `type` field.

## Interfaces
- `CostProvider` ABC (`providers/base.py`):
  - `cloud` property — provider identifier
  - `kill_job(kill_descriptor, reason) -> KillResult`
  - `fetch_billed_costs(project_id, since, until) -> list[BilledCost]` (default: empty)
- `providers.registry.kill_job(kill_descriptor, reason) -> KillResult`
- `providers.registry.fetch_billed_costs(cloud, project_id, since, until) -> list`
- `KillResult` dataclass: `killed`, `job_id`, `action`, `detail`, `error`
- `BilledCost` dataclass: `project_id`, `provider`, `api`, `cost_usd`, etc.

## Provider implementations

| Provider | Kill types | Billing source |
|----------|-----------|----------------|
| `GcpProvider` | `cloud_run`, `cloud_scheduler`, `gce`, `gke` | BigQuery billing export |
| `OpenStackProvider` | `openstack` (Nova stop) | Ceilometer/Gnocchi (if available) |
| `CloudflareProvider` | `cloudflare` (disable_pages) | GraphQL Analytics API |
| `HttpCallbackProvider` | `http_callback` (POST to URL) | N/A |

## Dependencies
- `google.cloud.run_v2`, `compute_v1`, `container_v1` (GcpProvider, lazy)
- `subprocess` + `openstack` CLI (OpenStackProvider)
- `urllib.request` (HttpCallbackProvider — stdlib only)
- `os.environ` — `DRY_RUN`, `BQ_DATASET`, `HUB_PROJECT_ID`

## Dependents
- `intent_kill.py` — `providers.registry.kill_job()`
- `admin_routes.py:handle_reconcile()` — `providers.registry.fetch_billed_costs()`

## State/data
- `_instances: dict[str, CostProvider]` — lazy singletons in registry
- No persistent state

## Boundaries
- Each provider checks `DRY_RUN` independently (reads env at call time)
- Unknown kill type falls back to `HttpCallbackProvider`
- GCP provider reads `DRY_RUN` via `os.environ` (not `main.DRY_RUN`) — decoupled

## Security sensitivity
HIGH — executes kill actions against cloud resources. HTTP callback provider
sends authenticated POST to arbitrary URLs (from kill descriptor).

## Before modifying
- New provider: implement `CostProvider`, register in `providers/registry.py`,
  add tests in `tests/test_providers.py`, update `docs/PRD.md`
- Kill descriptor schema changes affect both hub and client
- See [workflows/reconciliation.md](../workflows/reconciliation.md)

## Test map target
`tests/test_providers.py` (226 lines) — 18 tests, all mock GCP clients
