# Component: Inventory

## Location
`inventory.py` (172 lines)

## Responsibility
Unified cloud resource inventory. Reads the account registry (Firestore/YAML)
and Terraform state file to produce a single view of every cloud resource and
account. Exposed via `GET /api/v1/inventory`. This is the "one place that
understands all cloud resources and accounts."

## Interfaces
- `build_inventory() -> dict` — returns `{accounts, resources, summary}`
- `GET /api/v1/inventory` — Flask endpoint

## Dependencies
- `registry.py` — `list_accounts()`
- `paths.py` — `resolve()` for terraform state path
- `os.environ` — `TERRAFORM_DIR` (default: `terraform`)
- `json` — parses `terraform.tfstate`

## Dependents
- `main.py` — registers `inventory.bp`
- `tests/test_inventory.py` — temp accounts + temp tfstate

## State/data
- No persistent state — builds on each request
- Terraform state file: `terraform/terraform.tfstate` (gitignored)

## Terraform resource type mapping
`google_cloud_run_service` → `cloud_run`, `google_cloudfunctions2_function` →
`cloud_function`, `google_cloud_scheduler_job` → `cloud_scheduler`,
`google_bigquery_dataset` → `bigquery_dataset`, `google_storage_bucket` →
`storage_bucket`, `google_firestore_database` → `firestore_database`,
`google_pubsub_topic` → `pubsub_topic`, `google_billing_budget` →
`billing_budget`, `google_service_account` → `service_account`.

## Boundaries
- Degrades gracefully: missing/malformed tfstate → empty resources + WARNING log
- Resources associated with accounts by matching `project_id`

## Security sensitivity
LOW — read-only inventory. But exposes account structure (project IDs, owners).

## Before modifying
- New Terraform resource types: add to `type_map` in `_load_terraform_resources()`
- See [architecture/data.md](../architecture/data.md)

## Test map target
`tests/test_inventory.py` (170 lines) — 9 tests with temp tfstate
