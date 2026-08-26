# Workflow: Budget Alert → Kill

## Entry Point
`POST /` in `main.py` — Pub/Sub push from Cloud Billing budget alert.

## Execution Path

1. **Cloud Billing** fires budget alert → Pub/Sub topic `budget-alerts`
2. **Pub/Sub push subscription** → `POST /` on Cloud Run (OIDC-authenticated)
3. `main.py:handle_pubsub()` → `process_alert(envelope)`
4. **Envelope validation** — check `isinstance(envelope, dict)` and `"message"` key
5. **Dedup** — `dedup._is_duplicate(msg_id)` against in-memory cache (10-min TTL)
6. **Parse** — `alerts.parse_pubsub_message(envelope)` → `BudgetAlert` dataclass
7. **Threshold evaluation** — `alerts.should_take_action(alert)`:
   - actual >= 100% → act
   - forecast >= 90% → act
   - actual >= 50% AND forecast > budget → act
8. **Self-protection check** — warn if `SELF_PROJECT_ID` in `alert.project_ids`
   (never kill self)
9. **Target filtering** — exclude allowlisted (`is_project_protected()`) and self
10. **Kill execution** — `execute_killswitch(project_id, "budget_alert")` per target:
    - `disable_cloud_run_services` — scale to 0, internal ingress
    - `pause_scheduler_jobs` — if `ENABLE_RUN_PAUSE`
    - `disable_build_triggers` — if `ENABLE_TRIGGER_DISABLE`
    - `stop_compute_instances` — if `STOP_COMPUTE_INSTANCES`
    - `scale_down_gke_clusters` — if `ENABLE_GKE_SCALE_DOWN`
    - `revoke_api_keys` — if `ENABLE_API_KEY_REVOKE`
    - `disable_billing` — if `ENABLE_BILLING_SHUTOFF`
11. **Return** — `(200, "Processed — N actions (dry-run|live)")`

## Evidence
- `main.py:handle_pubsub()` (line ~230), `process_alert()` (line ~140)
- `alerts.py:parse_pubsub_message()`, `should_take_action()`
- `dedup.py:_is_duplicate()`
- `killswitch_actions.py` — all 7 action functions
- `terraform/pubsub.tf` — push subscription config
- `terraform/budgets.tf` — budget alert thresholds (50%, 90%, 100%, 150%)
- `tests/test_killswitch.py` — `make_envelope()` helper, threshold tests

## Failure Paths
- **Malformed envelope** → 400 "Malformed envelope"
- **Parse failure** → 400 "Failed to parse alert" (logged at ERROR)
- **Below threshold** → 200 "Below action threshold"
- **All allowlisted** → 200 "All projects allowlisted"
- **Self-project alert** → logged WARNING, not killed
- **GCP API error per action** → logged ERROR, action skipped, continues to next
- **DRY_RUN=true** → all actions log without executing

## Change Guidance
- **Threshold changes:** `alerts.should_take_action()` + `terraform/budgets.tf`
- **New kill action:** add to `killswitch_actions.py`, add to `execute_killswitch()`,
  add `ENABLE_*` env var, add to Terraform `cloud_run.tf` env vars
- **Dedup TTL:** `dedup._DEDUP_TTL_SECONDS` (currently 600s)
- **Allowlist:** env `ALLOWLIST` or registry `allowlist: true` field
- **Self-protection:** `SELF_PROJECT_ID` env var — never remove the hard-block
