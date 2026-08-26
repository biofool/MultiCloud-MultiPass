# Component: Budget Alert Parser

## Location
`alerts.py` (116 lines), `dedup.py` (27 lines)

## Responsibility
Parse Cloud Billing Pub/Sub budget alert envelopes into `BudgetAlert`
dataclass, evaluate whether the alert warrants action, and deduplicate
Pub/Sub messages (in-memory, 10-min TTL).

## Interfaces
- `parse_pubsub_message(envelope) -> BudgetAlert | None`
- `should_take_action(alert) -> bool` — act on actual >= 100%, forecast >= 90%,
  or actual >= 50% with forecast > budget
- `BudgetAlert` dataclass: `alert_type`, `budget_name`, `threshold_percent`,
  `actual_spend`, `forecasted_spend`, `budget_amount`, `currency`, `project_ids`
- `_is_duplicate(msg_id) -> bool` — in-memory dedup cache
- Constants: `ALERT_TYPE_BUDGET`, `ALERT_TYPE_FORECAST`

## Dependencies
- `main` (lazy `import main` for `main.log`) — OBSERVED in `alerts.py:356`

## Dependents
- `main.py` — imports `parse_pubsub_message`, `should_take_action`, constants
- `tests/test_killswitch.py` — tests parsing and threshold logic

## State/data
- `_processed_messages: dict[str, float]` — in-memory, per-container dedup
- `_DEDUP_TTL_SECONDS = 600` (10 min)

## Boundaries
- Dedup is per-container, not persistent — redelivery after cold start is possible
- Alert parsing tolerates multiple schema variants (`thresholdPercent`/`threshold`,
  `actualCost`/`costAmount`, etc.)

## Security sensitivity
LOW — parsing only. But incorrect threshold logic could cause false kills or
missed alerts.

## Before modifying
- Threshold changes affect when kills fire — test with `should_take_action()`
- Dedup TTL change affects redelivery behavior
- See [workflows/budget-alert-kill.md](../workflows/budget-alert-kill.md)

## Test map target
`tests/test_killswitch.py` — `make_envelope()` helper, threshold tests
