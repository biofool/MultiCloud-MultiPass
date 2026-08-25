# Workflow: Intent/Actual Reporting

## Entry Point
Sub-project client (`cloud_management_client`) calls hub HTTP endpoints.

## Execution Path — Declare Intent

1. Sub-project calls `CloudManagementClient.declare_intent(job_id, ...)`
2. Client sends `POST /api/v1/intent` with bearer token
3. `intent_routes.py:declare_intent()`:
   a. Validate JSON body, check `project_id` present
   b. `_validate_token(project_id)` — per-project bearer token check
      (`intent_auth.py`): per-project token from registry's
      `report_token_secret` (env var lookup), fallback to
      `CLOUDMANAGEMENT_REPORT_TOKEN`, using `secrets.compare_digest`
   c. Generate `intent_id` if not provided (`int_` + 16 hex chars)
   d. Build `Intent` dataclass, set `status="declared"`
   e. **Budget check:** `check_project_budget(project_id)` — if over budget,
      deny (`approved=False`, `status="denied"`)
   f. `save_intent(intent)` — Firestore or YAML
   g. Compute `budget_remaining_usd` from account budget - spent actuals
   h. Return `{intent_id, approved, budget_remaining_usd, kill_switch_armed}`

## Execution Path — Report Actual

1. Sub-project calls `report_actual(intent_id, ...)` (async by default)
2. Client enqueues to background thread + durable spool
3. Client sends `POST /api/v1/actual` with bearer token
4. `intent_routes.py:report_actual()`:
   a. Validate JSON, check `project_id` + `intent_id`
   b. `_validate_token(project_id)`
   c. `get_intent(intent_id)` — 404 if not found
   d. Determine `sequence` (max existing + 1) for incremental reports
   e. Build `Actual` dataclass with `client_seq` (monotonic per-intent)
   f. `save_actual(actual)` — Firestore or YAML
   g. Update intent `status` and `updated_at`
   h. **Overrun check:** `check_intent_overrun(intent)`:
      - `actual_exceeds_intent_calls`: calls ratio > `INTENT_VARIANCE_THRESHOLD` (1.2)
      - `actual_exceeds_intent_cost`: cost ratio > threshold
   i. If overrun: `kill_intent(intent, reason, rule)`:
      - Get kill descriptor from `intent.kill` or registry `jobs` fallback
      - Route to `providers.registry.kill_job()`
      - `save_kill_event()` — audit log
      - Update intent `status="killed"`
   j. Return `{actual_id, overrun_detected, overrun, kill_result}`

## Execution Path — Intent Overrun Poll

1. Cloud Scheduler fires `POST /poll-intents` every 2 min
2. `admin_routes.py:handle_poll_intents()`:
   a. For each intent with `status="running"`:
      - `check_intent_overrun(intent)` → kill if exceeded
      - `check_project_budget(intent.project_id)` → kill if budget exceeded
      - Self-protection: self-project intents logged but not killed

## Evidence
- `intent_routes.py` — all endpoint handlers (515 lines)
- `intent_auth.py:_validate_token()` — bearer token validation
- `intent_detection.py:check_intent_overrun()`, `check_project_budget()`
- `intent_kill.py:kill_intent()` — routes to providers
- `intent_storage.py` — Firestore/YAML persistence
- `cloud_management_client/_intent_ops.py`, `_actual_ops.py` — client side
- `terraform/scheduler.tf` — `poll_intents_trigger` (*/2 * * * *)
- `tests/test_intent.py` — 25+ tests (45 currently erroring due to pytest-flask)
- `tests/test_cloud_management_client_offline.py` — spool, client_seq tests

## Failure Paths
- **Missing auth** → 401 unauthorized
- **Missing project_id** → 400
- **Intent not found** → 404
- **No kill descriptor** → `{killed: false, reason: "no kill descriptor"}`
- **Provider kill error** → `KillResult(killed=False, error=...)`, logged
- **Client network error** → logged WARNING, spooled for retry
- **Permanent HTTP error (4xx)** → spool entry dropped (not retried)
- **Transient error (5xx, 408, 429)** → retried with exponential backoff

## Change Guidance
- **New endpoint:** add to `intent_routes.py`, add `_validate_token()` unless
  documented exception (see `list_all_intents` docstring)
- **Variance threshold:** `INTENT_VARIANCE_THRESHOLD` env var (default 1.2)
- **New detection rule:** add to `intent_detection.py`
- **Client API change:** bump `cloud_management_client.__version__`, update
  both hub endpoints and client methods together
- **Kill descriptor schema:** affects `intent.kill`, registry `jobs`, and
  `providers/` — change all three together
