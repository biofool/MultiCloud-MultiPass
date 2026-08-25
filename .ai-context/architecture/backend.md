# Backend Architecture

## Flask app structure

Single Flask app in `main.py` registers four blueprints:

| Blueprint | Module | Routes |
|-----------|--------|--------|
| `dashboard` | `dashboard.py` | `GET /dashboard`, `GET /api/{summary,daily,services,projects,spike,accounts}` |
| `intent` | `intent_routes.py` (re-exported via `intent.py`) | `POST /api/v1/intent`, `POST /api/v1/actual`, `GET /api/v1/expected-costs/<pid>`, `GET /api/v1/intents`, `GET /api/v1/intents/<pid>`, `POST /api/v1/kill/<iid>`, `GET/POST /api/v1/budget/<pid>`, `GET /api/v1/intent/<iid>`, `GET /api/v1/kill-orders`, `POST /api/v1/exposure` |
| `inventory` | `inventory.py` | `GET /api/v1/inventory` |
| `admin` | `admin_routes.py` | `POST /poll`, `POST /poll-intents`, `POST /reconcile`, `GET /` |

Plus direct routes: `POST /` (budget alert), `GET /health`.

## Kill switch orchestration

`main.py:execute_killswitch(project_id, reason)` runs all enabled actions in
sequence against a single project:

1. `disable_cloud_run_services` — scale to 0 min instances + internal ingress
2. `pause_scheduler_jobs` — pause all Cloud Scheduler jobs (if `ENABLE_RUN_PAUSE`)
3. `disable_build_triggers` — disable Cloud Build triggers (if enabled)
4. `stop_compute_instances` — stop GCE instances (if enabled)
5. `scale_down_gke_clusters` — scale GKE node pools to 0 (if enabled)
6. `revoke_api_keys` — soft-delete API keys (if enabled)
7. `disable_billing` — disable billing on project (if `ENABLE_BILLING_SHUTOFF`)

Each action checks `main.DRY_RUN` and logs without executing when true.
All actions are in `killswitch_actions.py`.

**Self-protection:** If `project_id == SELF_PROJECT_ID`, returns `[]` and logs
critical. This prevents the feedback loop where the monitor kills itself.

## Intent/actual protocol

The real-time tier. Sub-projects declare expected API usage before making
calls and report actuals after (or incrementally). The hub validates actual
vs intent and kills the specific job on overrun.

**Module split** (refactored from monolithic `intent.py`):

| Module | Responsibility |
|--------|---------------|
| `intent.py` | Config + re-exports (facade) |
| `intent_models.py` | `Intent`, `Actual`, `ExpectedCost` dataclasses |
| `intent_storage.py` | Firestore/YAML persistence |
| `intent_detection.py` | `check_intent_overrun`, `check_project_budget` |
| `intent_kill.py` | `kill_intent` — routes to provider |
| `intent_auth.py` | `_validate_token` — per-project bearer auth |
| `intent_routes.py` | Flask blueprint with all endpoints |

**Detection rules** (`intent_detection.py`):
- `actual_exceeds_intent_calls`: actual_calls / expected_calls > `INTENT_VARIANCE_THRESHOLD` (default 1.2)
- `actual_exceeds_intent_cost`: actual_cost / expected_cost > threshold
- `project_budget_exceeded`: rolling monthly spend > `budget_amount_usd`

**Kill routing** (`intent_kill.py`): uses `intent.kill` descriptor or falls
back to registry `jobs` config (matched by `job_id_prefix`). Routes to
`providers.registry.kill_job()`.

## Provider abstraction

`providers/base.py` defines `CostProvider` ABC with:
- `kill_job(kill_descriptor, reason) -> KillResult`
- `fetch_billed_costs(project_id, since, until) -> list[BilledCost]` (optional)

`providers/registry.py` maps kill descriptor `type` to provider:
- `cloud_run` / `cloud_scheduler` / `gce` / `gke` → `GcpProvider`
- `openstack` → `OpenStackProvider`
- `cloudflare` → `CloudflareProvider`
- `http_callback` / unknown → `HttpCallbackProvider`

## Client package (`cloud_management_client/`)

Stdlib-only pip package (v0.12.0) for sub-projects. Assembled from mixins:
- `_lifecycle.py` — construction, async worker thread, spool setup
- `_transport.py` — HTTP GET/POST, identity token, bearer auth, gate token
- `_intent_ops.py` — `declare_intent`, `get_intent`, `wait_for_reschedule`, `check_budget`, `can_run`
- `_actual_ops.py` — `report_actual` (async by default), `check_kill_orders`, `report_exposure`, `flush`
- `spool.py` — durable on-disk spool for report_actual entries (issue #12)
- `context.py` — `IntentContext` context manager
- `models.py` — `IntentResponse`, `ActualResponse`, `BudgetCheck`
- `errors.py` — `CloudManagementError`, `JobKilledError`, `KillOrder`, `_fail`

**Key design:** errors are logged at WARNING and never raised unless
`CLOUDMANAGEMENT_STRICT=true`. `report_actual` is async (background thread)
by default; `declare_intent` is always synchronous.
