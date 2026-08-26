# Component: Intent/Actual Protocol

## Location
`intent.py` (103 lines, facade), `intent_routes.py` (515 lines),
`intent_models.py` (145 lines), `intent_storage.py` (250 lines),
`intent_detection.py` (104 lines), `intent_kill.py` (77 lines),
`intent_auth.py` (46 lines)

## Responsibility
Real-time intent/actual API usage reporting. Sub-projects declare expected
usage before calls and report actuals after. Hub validates actual vs intent,
detects overruns, and kills the specific job. Exposed as Flask blueprint.

## Interfaces (HTTP)
- `POST /api/v1/intent` — declare intent (pre-call)
- `POST /api/v1/actual` — report actual (post-call / incremental)
- `GET /api/v1/expected-costs/<project_id>` — pull authoritative costs
- `GET /api/v1/intents` — list all active intents
- `GET /api/v1/intents/<project_id>` — list intents for a project
- `POST /api/v1/kill/<intent_id>` — manual kill override
- `GET /api/v1/budget/<project_id>` — read-only budget status
- `POST /api/v1/budget/<project_id>` — budget admission decision
- `GET /api/v1/intent/<intent_id>` — fetch single intent
- `GET /api/v1/kill-orders` — client-polled kill orders
- `POST /api/v1/exposure` — report key exposure for rotation

## Module split (refactored from monolithic `intent.py`)

| Module | Role | Config access pattern |
|--------|------|----------------------|
| `intent.py` | Config + re-exports (facade) | Direct env read |
| `intent_models.py` | Dataclasses | None (self-contained) |
| `intent_storage.py` | Firestore/YAML persistence | `import intent as _intent_mod` |
| `intent_detection.py` | Overrun/budget detection | `import intent as _intent_mod` |
| `intent_kill.py` | Kill execution | Direct imports (no reload-sensitive config) |
| `intent_auth.py` | Bearer token validation | `import intent as _intent_mod` |
| `intent_routes.py` | Flask blueprint | Direct imports by name |

**Why `_intent_mod`:** `importlib.reload(intent)` in tests resets env-derived
config in-place. Sibling modules read config through `_intent_mod` at call
time so they pick up fresh values without needing their own reload.

## Dependencies
- `registry.py` — account lookup for budget/token
- `providers.registry` — `kill_job()` for kill execution
- `paths.py` — path resolution for YAML files
- `google.cloud.firestore` (lazy, when `USE_FIRESTORE=true`)
- `flask` — blueprint, request/response
- `secrets.compare_digest` — constant-time token comparison

## Dependents
- `main.py` — registers `intent.bp`
- `admin_routes.py` — `handle_poll_intents()` calls `list_intents`, `check_intent_overrun`, `kill_intent`
- `cloud_management_client/` — calls the HTTP endpoints

## State/data
- Firestore: `api_intents`, `api_actuals`, `expected_costs`, `kill_events`
- YAML fallbacks: `config/api_intents.yaml`, `config/api_actuals.yaml`, etc.
- `_fs_client` — lazy singleton (stays in `intent.py` for reload behavior)

## Security sensitivity
HIGH — kill endpoint can shut down jobs. Auth via per-project bearer tokens
(`_validate_token`). `list_all_intents` is intentionally unauthenticated (see
docstring). `get_single_intent` uses unguessable `intent_id` as auth token.

## Before modifying
- New endpoints need `_validate_token(project_id)` unless there's a documented
  reason not to (see `list_all_intents` docstring)
- Detection threshold changes (`INTENT_VARIANCE_THRESHOLD`) affect kill sensitivity
- `client_seq` enforcement prevents stale replays — don't break this
- See [workflows/intent-actual-reporting.md](../workflows/intent-actual-reporting.md)

## Test map target
`tests/test_intent.py` (647 lines) — 25+ tests (currently 45 errors due to
pytest-flask version incompatibility, pre-existing)
