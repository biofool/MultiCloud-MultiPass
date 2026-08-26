# Component: Quota Poller

## Location
`poller.py` (165 lines)

## Responsibility
Real-time quota-spike detection. Checks every registered, non-allowlisted
account against three detection rules using Cloud Monitoring metrics. Trips
the kill switch on runaway consumption. Called via `POST /poll` by Cloud
Scheduler every 5 minutes.

## Interfaces
- `poll_all_accounts(execute_killswitch) -> list[dict]` — checks all accounts,
  calls injected `execute_killswitch` on trip
- `check_account(account) -> dict | None` — evaluates one account

## Detection rules
1. **quota_exceeded** — `serviceruntime.googleapis.com/quota/exceeded` > 0
2. **baseline_ratio** — recent rate > `BASELINE_MULTIPLIER` (default 5x) ×
   trailing 1h baseline rate
3. **absolute_cap** — recent rate > `account.quota_rpm_cap` (skipped when 0)

## Dependencies
- `registry.py` — `list_accounts()`
- `google.cloud.monitoring_v3` — `MetricServiceClient` (lazy)
- `os.environ` — `BASELINE_MULTIPLIER`, `POLL_WINDOW_MINUTES`,
  `BASELINE_WINDOW_MINUTES`, `SELF_PROJECT_ID`

## Dependents
- `admin_routes.py:handle_poll()` — calls `poll_all_accounts(main.execute_killswitch)`
- `tests/test_poller.py` — mocks `_sum_metric`, `registry.list_accounts`

## State/data
- `_monitoring_client` — lazy singleton
- No persistent state

## Boundaries
- `execute_killswitch` is injected (not imported) for testability
- Self-protection: hub project is checked but kill is blocked (critical log)
- Errors in metric queries return 0.0 (graceful degradation with WARNING log)

## Security sensitivity
MEDIUM — triggers kill switch. Incorrect detection could cause false kills.

## Before modifying
- Detection rule changes affect kill sensitivity
- `POLL_WINDOW_MINUTES` and `BASELINE_WINDOW_MINUTES` are env-configurable
- See [workflows/quota-poll-kill.md](../workflows/quota-poll-kill.md)

## Test map target
`tests/test_poller.py` (111 lines) — 7 tests, all mock `_sum_metric`
