# Data Architecture

## Data stores

| Store | Backend (prod) | Backend (dev/test) | Module |
|-------|----------------|--------------------|--------|
| Account registry | Firestore `accounts` collection | `config/accounts.yaml` | `registry.py` |
| Intents | Firestore `api_intents` collection | `config/api_intents.yaml` | `intent_storage.py` |
| Actuals | Firestore `api_actuals` collection | `config/api_actuals.yaml` | `intent_storage.py` |
| Expected costs | Firestore `expected_costs` collection | `config/expected_costs.yaml` | `intent_storage.py` |
| Kill events | Firestore `kill_events` collection | `config/kill_events.yaml` | `intent_storage.py` |
| Billing data | BigQuery `cloud_billing_export` dataset | N/A (dashboard degrades gracefully) | `dashboard.py`, `providers/gcp.py` |
| Terraform state | `terraform/terraform.tfstate` (local) | same | `inventory.py` |
| Client spool | `~/.cache/cloud_management_client/spool/` | same | `cloud_management_client/spool.py` |

Selection: `USE_FIRESTORE=true` (prod) vs `false` (dev). OBSERVED in
`registry.py:34`, `intent.py:41`.

## Account schema (`registry.py:Account`)

```python
@dataclass
class Account:
    project_id: str               # logical key for intent/actual
    billing_account_id: str = ""
    owner_email: str = ""
    allowlist: bool = False        # never touched by kill switch
    budget_amount_usd: float = 5.0
    quota_rpm_cap: int = 0         # 0 = no absolute cap
    cloud: str = "gcp"             # gcp|openstack|cloudflare|generic
    gcp_project_id: str = ""
    openstack_project: str = ""
    openstack_regions: list[str] | None = None
    report_token_secret: str = ""  # Secret Manager ref or env var name
    jobs: list[dict] | None = None # per-job kill descriptors (fallback)
```

Example: `config/accounts.example.yaml` shows GCP, OpenStack, and
allowlisted accounts.

## Intent schema (`intent_models.py:Intent`)

Key fields: `intent_id`, `project_id`, `source_repo`, `job_id`, `job_name`,
`provider`, `api`, `expected_calls`, `expected_cost_usd`, `expected_tokens`,
`rate_limit_rpm`, `window_start`, `window_end`, `kill` (dict descriptor),
`metadata`, `approved`, `status` (declared|running|completed|failed|killed).

## Actual schema (`intent_models.py:Actual`)

Key fields: `actual_id`, `intent_id`, `project_id`, `job_id`, `provider`,
`api`, `actual_calls`, `actual_cost_usd`, `actual_tokens`, `status`,
`sequence` (incremental ordering), `client_seq` (monotonic per-intent,
issue #1), `reconciled_cost_usd`, `reconciled_at`.

**Cumulative semantics:** clients send cumulative totals (not deltas).
`sum_actuals_for_intent()` picks the latest by `(client_seq, sequence)`.

## ExpectedCost schema (`intent_models.py:ExpectedCost`)

Fields: `project_id`, `provider`, `unit_cost_usd`,
`free_tier_remaining_calls`, `free_tier_reset`,
`expected_remaining_monthly_usd`, `calibration_delta`, `pricing` (dict),
`updated_at`.

## Firestore query patterns

- **Single-field queries** to avoid composite index requirements. OBSERVED
  in `intent_storage.py:list_actuals()` — queries by `intent_id` OR
  `project_id`, filters secondary field in memory.
- **Kill events:** `order_by("timestamp", DESCENDING).limit(n*5)` then
  filter in memory. OBSERVED in `intent_storage.py:list_kill_events()`.

## YAML fallback

All YAML files use `yaml.safe_load` / `yaml.safe_dump`. Paths resolved via
`paths.resolve()` (project root). Runtime state files are gitignored:
`config/accounts.yaml`, `config/api_intents.yaml`, `config/api_actuals.yaml`,
`config/kill_events.yaml`, `config/expected_costs.yaml`.
