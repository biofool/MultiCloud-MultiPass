# Component: Dashboard

## Location
`dashboard.py` (336 lines), `templates/` (10 HTML files + partials)

## Responsibility
Lightweight web UI and JSON API for cloud billing spend visualization.
Queries BigQuery billing export data. Designed for Cloud Run scale-to-zero:
no always-on polling, queries only on page load, 5-minute cache.

## Interfaces (HTTP)
- `GET /dashboard` — rendered HTML page
- `GET /api/summary` — MTD summary
- `GET /api/daily` — daily spend by project
- `GET /api/services` — top services by cost
- `GET /api/projects` — spend by project
- `GET /api/spike` — recent spend spike detection
- `GET /api/accounts` — account registry with spend

## Dependencies
- `registry.py` — `list_accounts()` for team view
- `google.cloud.bigquery` (lazy) — billing queries
- `flask` — blueprint, render_template, jsonify
- `os.environ` — `BQ_BILLING_TABLE`, `BQ_DATASET`, `BUDGET_AMOUNT_USD`, `HUB_PROJECT_ID`

## Dependents
- `main.py` — registers `dashboard.bp`
- `tests/test_dashboard.py` — mocks BigQuery client

## State/data
- `_cache: dict[str, tuple[float, Any]]` — in-memory, 5-min TTL
- `_CACHE_TTL = 300`
- SQL templates in `sql/` (5 files)

## Boundaries
- No app-level auth — relies on Cloud Run IAM in prod. OBSERVED (no auth checks).
- Degrades gracefully when BigQuery is not configured (shows warning banner)
- Caching avoids repeated BigQuery scans (cost control)

## Security sensitivity
LOW — read-only. But BigQuery queries scan billing data (cost implications).

## Before modifying
- New API endpoints should use `_cached()` decorator
- SQL changes should filter on `invoice.month` partition to minimize scan cost
- Template changes: partials in `templates/partials/`

## Test map target
`tests/test_dashboard.py` (190 lines) — 14 tests, mocks `_query_bq`
