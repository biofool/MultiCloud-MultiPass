# Component: Kill Switch Core

## Location
`main.py` (286 lines), `killswitch_actions.py` (314 lines)

## Responsibility
Flask app entry point + kill switch orchestration. Receives budget alerts
via Pub/Sub push, evaluates thresholds, and executes all enabled kill
actions against target projects. Also the hub for self-protection logic.

## Interfaces
- `POST /` → `handle_pubsub()` → `process_alert(envelope)` — budget alert
- `GET /health` — health check
- `execute_killswitch(project_id, reason) -> list[dict]` — called by budget
  alert path, quota poller, and intent poll
- `is_project_protected(project_id) -> bool` — allowlist check

## Dependencies
- `alerts.py` — `parse_pubsub_message`, `should_take_action`, `BudgetAlert`
- `dedup.py` — `_is_duplicate`, `_processed_messages`
- `killswitch_actions.py` — all 7 kill action functions
- `registry.py` — `is_allowlisted()`
- `google.cloud.{run_v2, scheduler_v1, compute_v1, billing_v1}` — GCP clients
- Flask blueprints: `dashboard`, `intent`, `inventory`, `admin_routes`

## Dependents
- `killswitch_actions.py` — imports `main` (qualified access to `DRY_RUN` etc.)
- `alerts.py` — imports `main` (for `main.log`)
- `poller.py` — `execute_killswitch` injected into `poll_all_accounts()`
- `admin_routes.py` — imports `main` (config flags, `execute_killswitch`)
- All tests that mock `google.cloud.*` in `sys.modules` before importing `main`

## State/data
- In-memory config flags from env vars (read at import time)
- No persistent state (delegated to registry/intent modules)

## Boundaries
- Self-protection: `SELF_PROJECT_ID` hard-block in `execute_killswitch()`
- Allowlist: env `ALLOWLIST` + `registry.is_allowlisted()`
- DRY_RUN defaults to `true` — all actions log without executing

## Security sensitivity
**HIGH** — can disable billing, stop compute, revoke API keys, scale GKE to 0.
All kill actions are production-impacting. DRY_RUN is the primary safety guard.

## Before modifying
- Never remove the `SELF_PROJECT_ID` hard-block — it prevents feedback loops
- Any new kill action must check `main.DRY_RUN` and `main.ENABLE_*` flag
- Any new kill action must be added to `execute_killswitch()` sequence
- Test with `DRY_RUN=true` — mock `google.cloud.*` in `sys.modules`
- See [workflows/budget-alert-kill.md](../workflows/budget-alert-kill.md)

## Test map target
`tests/test_killswitch.py` (370 lines) — parsing, threshold, dedup, config
