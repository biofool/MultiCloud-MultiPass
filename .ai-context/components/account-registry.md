# Component: Account Registry

## Location
`registry.py` (173 lines)

## Responsibility
Monitored account registry. Tracks which cloud projects are monitored, who
owns them, per-project budget/quota caps, and allowlist status. Dual backend:
Firestore (prod) or YAML file (dev/test).

## Interfaces
- `list_accounts() -> list[Account]`
- `get_account(project_id) -> Account | None`
- `is_allowlisted(project_id) -> bool`
- `Account` dataclass with `to_dict()` / `from_dict()`

## Dependencies
- `paths.py` — `resolve()` for `ACCOUNTS_FILE` path
- `google.cloud.firestore` (lazy, when `USE_FIRESTORE=true`)
- `yaml` (lazy, when `USE_FIRESTORE=false`)
- `os.environ` — `USE_FIRESTORE`, `ACCOUNTS_FILE`, `FIRESTORE_PROJECT`

## Dependents
- `main.py` — `is_allowlisted()` for `is_project_protected()`
- `intent_auth.py` — `get_account()` for per-project token lookup
- `intent_detection.py` — `get_account()` for budget check
- `intent_kill.py` — `get_account()` for jobs fallback
- `intent_routes.py` — `get_account()` for budget remaining
- `poller.py` — `list_accounts()` for poll loop
- `admin_routes.py` — `list_accounts()` for reconcile
- `dashboard.py` — `list_accounts()` for team view
- `inventory.py` — `list_accounts()` for inventory build

## State/data
- Firestore `accounts` collection (prod) or `config/accounts.yaml` (dev)
- `_fs_client` — lazy singleton Firestore client

## Boundaries
- `list_accounts()` catches all errors and returns `[]` (graceful degradation)
- Firestore doc ID = `project_id`
- YAML schema: `{accounts: [Account.to_dict(), ...]}`

## Security sensitivity
MEDIUM — `allowlist: true` prevents kill switch from touching a project.
`report_token_secret` references env vars / Secret Manager for auth tokens.

## Before modifying
- Adding a new `Account` field requires updating `from_dict()`, `to_dict()`,
  `config/accounts.example.yaml`, and Terraform seed docs
- See [architecture/data.md](../architecture/data.md) for schema

## Test map target
`tests/test_registry.py` (68 lines) — 6 tests, YAML backend
