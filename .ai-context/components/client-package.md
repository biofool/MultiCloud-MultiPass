# Component: Cloud Management Client

## Location
`cloud_management_client/` (11 files, ~1400 lines total)

## Responsibility
Lightweight stdlib-only pip package (v0.12.0) for sub-projects to declare
intent, report actuals, check budget, poll kill orders, and report key
exposure. Best-effort: errors logged at WARNING, never raised unless
`CLOUDMANAGEMENT_STRICT=true`.

## Files

| File | Responsibility |
|------|---------------|
| `__init__.py` | Public API + version (0.12.0) |
| `client.py` | `CloudManagementClient` class (assembles mixins) |
| `_lifecycle.py` | Construction, async worker, spool setup |
| `_transport.py` | HTTP GET/POST, identity token, bearer auth, gate token |
| `_intent_ops.py` | `declare_intent`, `get_intent`, `wait_for_reschedule`, `check_budget`, `can_run` |
| `_actual_ops.py` | `report_actual` (async), `check_kill_orders`, `report_exposure`, `flush` |
| `spool.py` | Durable on-disk spool for report_actual (issue #12) |
| `context.py` | `IntentContext` context manager |
| `models.py` | `IntentResponse`, `ActualResponse`, `BudgetCheck` |
| `errors.py` | `CloudManagementError`, `JobKilledError`, `KillOrder`, `_fail` |

## Interfaces (public API)
- `CloudManagementClient(project_id, report_token, base_url, ...)` — constructor
- `declare_intent(job_id, ...) -> IntentResponse` — synchronous
- `report_actual(intent_id, ...) -> ActualResponse` — async by default, `sync=True` for final
- `intent(job_id, ...) -> IntentContext` — context manager
- `check_budget(expected_cost_usd) -> BudgetCheck`
- `can_run(expected_cost_usd) -> BudgetCheck`
- `get_intent(intent_id) -> dict | None`
- `wait_for_reschedule(intent_id, timeout, poll_interval) -> dict | None`
- `check_kill_orders() -> list[KillOrder]`
- `report_exposure(display_name, ...) -> dict`
- `flush()` — wait for pending async reports

## Dependencies
- **stdlib only** — `urllib`, `json`, `threading`, `queue`, `os`, `time`, `secrets`
- Zero deps on hub codebase, Flask, or google-cloud-*

## Dependents
- Sub-projects (AIRichardMoon, WorldStudioFinder, etc.) — `pip install cloud-management-client`
- `tests/test_cloud_management_client.py` — integration tests (skipped without token)
- `tests/test_cloud_management_client_offline.py` — offline tests (spool, client_seq)

## State/data
- On-disk spool: `~/.cache/cloud_management_client/spool/` (JSON files)
- Background daemon thread for async `report_actual`
- `_id_token` + `_id_token_expiry` — cached GCP OIDC token (identity mode)

## Key design decisions
- **Best-effort:** errors logged, not raised (unless `STRICT=true`)
- **Async report_actual:** background thread, never blocks caller
- **Durable spool:** entries written before HTTP attempt, deleted on success,
  retried with exponential backoff + jitter
- **client_seq:** monotonic per-intent counter prevents stale replays
- **Identity mode:** GCP OIDC ID token from metadata server (issue #10)
- **Gate token:** `X-Gate-Token` header for Cloudflare Worker auth gate

## Security sensitivity
MEDIUM — carries report tokens. Spool files contain payloads (no tokens).
Identity mode eliminates shared secrets for GCP-resident clients.

## Before modifying
- Must remain stdlib-only — no new dependencies
- `report_actual` async behavior: don't block the caller
- Spool format changes break in-flight entries from old clients
- Version bump `__version__` on any API change

## Test map target
`tests/test_cloud_management_client_offline.py` (198 lines) — 12 tests
`tests/test_cloud_management_client.py` (238 lines) — integration (8 skipped)
