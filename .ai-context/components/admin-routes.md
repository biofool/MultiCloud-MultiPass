# Component: Admin Routes

## Location
`admin_routes.py` (215 lines)

## Responsibility
Admin/ops Flask blueprint: quota polling, intent overrun polling, billing
reconciliation, and service-info endpoint. Registered on `main.app`.

## Interfaces (HTTP)
- `POST /poll` — Cloud Scheduler every 5 min → `poller.poll_all_accounts()`
- `POST /poll-intents` — Cloud Scheduler every 2 min → check running intents
- `POST /reconcile` — Cloud Scheduler daily → billing reconciliation
- `GET /` — service info (config flags, endpoints, registry backend)

## Dependencies
- `main` (qualified access) — `DRY_RUN`, `SELF_PROJECT_ID`, `ALLOWLIST`, config flags
- `registry.py` — `list_accounts()`, `USE_FIRESTORE`, `ACCOUNTS_FILE`
- `poller.py` — `poll_all_accounts()` (lazy import in handler)
- `intent` module — `list_intents`, `check_intent_overrun`, `check_project_budget`,
  `kill_intent`, `list_actuals`, `ExpectedCost`, `save_expected_cost` (lazy)
- `providers.registry` — `fetch_billed_costs()` (lazy import in reconcile)

## Dependents
- `main.py` — registers `admin.bp`
- `tests/test_admin_routes.py` — 13 tests

## State/data
- No persistent state

## Boundaries
- **No app-level auth** — relies on Cloud Run IAM (`roles/run.invoker` on
  `killswitch-rt` SA only). OBSERVED in docstring.
- Self-protection: intent overrun/budget checks on `SELF_PROJECT_ID` are
  logged critical but kill is blocked.
- Reconciliation: variance > 15% triggers `ExpectedCost` recalibration.

## Security sensitivity
MEDIUM — triggers kill switch via poll-intents. Reconciliation reads billing
data. No app-level auth (IAM-only).

## Before modifying
- Auth changes: see docstring — adding a shared secret would duplicate IAM
  and risk breaking Cloud Scheduler. Verify OIDC token `email` claim instead.
- Reconciliation variance threshold (15%) is hardcoded — consider env var
- See [workflows/reconciliation.md](../workflows/reconciliation.md)

## Test map target
`tests/test_admin_routes.py` (266 lines) — 13 tests
