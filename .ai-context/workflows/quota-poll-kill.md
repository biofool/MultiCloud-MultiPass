# Workflow: Quota Poll → Kill

## Entry Point
`POST /poll` in `admin_routes.py` — Cloud Scheduler every 5 minutes.

## Execution Path

1. **Cloud Scheduler** fires `POST /poll` (OIDC-authenticated via `killswitch-rt` SA)
2. `admin_routes.py:handle_poll()` → `poller.poll_all_accounts(main.execute_killswitch)`
3. **For each account** in `registry.list_accounts()`:
   a. Skip if `account.allowlist` is True
   b. `poller.check_account(account)` — evaluate 3 detection rules:
      - **quota_exceeded:** `serviceruntime.googleapis.com/quota/exceeded` > 0
        in last `POLL_WINDOW_MINUTES` (default 5)
      - **baseline_ratio:** recent rate > `BASELINE_MULTIPLIER` (default 5) ×
        trailing 1h baseline rate
      - **absolute_cap:** recent rate > `account.quota_rpm_cap` (skipped when 0)
   c. If trip detected:
      - **Self-protection:** if `account.project_id == SELF_PROJECT_ID`,
        log critical, append trip with `self_blocked: True`, do NOT kill
      - Otherwise: log WARNING, call `execute_killswitch(project_id, "quota_spike")`
4. **Return** — JSON `{checked: true, trips: [...], dry_run: ...}`

## Evidence
- `admin_routes.py:handle_poll()` (line ~50)
- `poller.py:poll_all_accounts()` (line ~300), `check_account()` (line ~262)
- `poller._sum_metric()` — Cloud Monitoring `MetricServiceClient`
- Metrics: `serviceruntime.googleapis.com/quota/exceeded`, `.../quota/rate/net_usage`
- `terraform/scheduler.tf` — `google_cloud_scheduler_job.poll_trigger` (*/5 * * * *)
- `tests/test_poller.py` — 7 tests, mock `_sum_metric` and `registry.list_accounts`

## Failure Paths
- **Monitoring API error** → `_sum_metric` returns 0.0, logged WARNING, no trip
- **Registry load error** → `list_accounts()` returns `[]`, logged ERROR
- **Self-project trip** → logged critical, kill blocked
- **DRY_RUN=true** → `execute_killswitch` logs actions without executing
- **No trips** → returns `{checked: true, trips: [], dry_run: ...}`

## Change Guidance
- **Detection sensitivity:** `BASELINE_MULTIPLIER`, `POLL_WINDOW_MINUTES`,
  `BASELINE_WINDOW_MINUTES` env vars
- **New detection rule:** add to `check_account()`, return dict with `rule` key
- **Poll frequency:** `terraform/scheduler.tf` schedule expression
- **`execute_killswitch` is injected** — tests pass a mock; keep this pattern
