# Test Plan — MultiCloud-MultiPass-gitlab

Reflects the file-layout refactor landed on `dev` (5 commits on top of
`main`@`b2d07078fef84449871ef9aed4601c3710a3f351`, `dev` HEAD
`a68c6f5ccf9f45a78e6d3a071b37273f9e8daf3e`). The refactor was a pure
structural split of five oversized files into smaller modules/partials/
Terraform files — no behavior changes. This plan maps the new layout to
existing test coverage, records the current pass/fail baseline, and
calls out what is and isn't covered.

## How to run

```
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt pytest
pytest -v
```

No live GCP credentials, Firestore, or BigQuery are required — every test
below either mocks `google.cloud.*` client libraries at import time or
uses the YAML-file storage backend against a `tmp_path`. The 8 skipped
tests (see "Known gaps") do require a live CloudManagement instance and
are expected to stay skipped in this environment.

## Result baseline

```
125 passed, 8 skipped
```

(112 passed / 8 skipped pre-existing tests, unchanged, plus 13 new tests
in `tests/test_admin_routes.py` added by this test pass — see below.)

## Coverage by refactored area

### `cloud_management_client/` (was one 1288-line `__init__.py`)

| New file | Covered by |
|---|---|
| `client.py` (construction, `CloudManagementClient`) | `tests/test_cloud_management_client_offline.py` (construction, disabled-client path, corrupt-spool tolerance) |
| `spool.py` (durable spool, `client_seq` bookkeeping) | `tests/test_cloud_management_client_offline.py` — thoroughly: restart continuity, per-intent sequencing, disabled spooling, corrupt entries, spool-off mode |
| `models.py`, `errors.py`, `_lifecycle.py` (context manager), `_transport.py` (HTTP delivery), `_intent_ops.py` (declare/report), `_actual_ops.py`, `context.py` | Only by `tests/test_cloud_management_client.py`, which is **skipped in this environment** — see Known gaps |
| `__init__.py` (re-export facade) | Implicitly exercised — both test files import top-level names (`CloudManagementClient`, `__version__`) through it |

### `main.py` + new siblings (was one 852-line file)

| New file | Covered by |
|---|---|
| `dedup.py` | `tests/test_killswitch.py::TestDedup` |
| `alerts.py` (parsing, threshold logic) | `tests/test_killswitch.py::TestParsePubsubMessage`, `::TestShouldTakeAction` |
| `killswitch_actions.py` | `tests/test_killswitch.py::TestBillingShutoffGuard`, `::TestApiKeyRevokeGuard`, `::TestGkeScaleDownGuard`, `::TestExecuteKillswitch` |
| `admin_routes.py` (new blueprint: `/poll`, `/poll-intents`, `/reconcile`, info `/`) | **Previously untested** — no existing test hit any of these four routes. Added `tests/test_admin_routes.py` (13 tests) covering: service-info payload shape, quota-poll pass-through to `poller.poll_all_accounts`, the intent-overrun/budget-overrun/self-project-protection branches of `/poll-intents`, and the allowlist-skip / no-billing-data / variance-recalibration branches of `/reconcile`. All collaborators (`poller`, `intent`, `registry`, `providers.registry`) are mocked — no live calls. |
| `main.py` orchestrator (`process_alert`, `execute_killswitch`, `is_project_protected`, Flask routes `/` POST, `/health`) | `tests/test_killswitch.py::TestProcessAlert`, `::TestFlaskEndpoints`, `::TestIsProjectProtected` |

### `intent.py` + new siblings (was one 828-line file)

| New file | Covered by |
|---|---|
| `intent_models.py` (`Intent`, `Actual`, `ExpectedCost`) | Indirectly via every `tests/test_intent.py` case (constructed/round-tripped through the HTTP API) and directly by `tests/test_admin_routes.py`'s use of `intent.Actual` |
| `intent_storage.py` (YAML/Firestore-backed CRUD) | `tests/test_intent.py::TestExpectedCosts`, `::TestListIntents`, plus every declare/report test |
| `intent_detection.py` (`check_intent_overrun`, `check_project_budget`) | `tests/test_intent.py::TestReportActual::test_actual_triggers_overrun_kill`, `::test_intent_denied_when_budget_exceeded`; branch behavior further exercised (mocked) in `tests/test_admin_routes.py::TestPollIntentsEndpoint` |
| `intent_auth.py` (`_validate_token`) | `tests/test_intent.py::TestDeclareIntent::test_unauthorized`, `::test_no_auth_header` |
| `intent_kill.py` (`kill_intent`) | `tests/test_intent.py::TestManualKill`; also exercised (mocked) in `tests/test_admin_routes.py` |
| `intent_routes.py` (Flask blueprint, all `/api/v1/*` endpoints) | Every case in `tests/test_intent.py` hits these routes through `main.app.test_client()` |
| `intent.py` (env config + re-exports) | Implicitly — `importlib.reload(intent)` in the `yaml_backend` fixture confirms the config re-derivation path still works after reload |

### `terraform/*.tf` (was one 482-line `main.tf`, now deleted)

Not exercised by `pytest` — this is an infrastructure-as-code directory
with no application test runner attached (`terraform validate`/`plan`
would require a Terraform binary and cloud credentials, and running it
was out of scope for this pass; it was also not part of the "run
pytest" instruction). Coverage for this split came from the refactor's
own verification step: every one of the 27 resource/output/locals blocks
was diffed byte-for-byte between the original `main.tf` and the new
files, confirming no resource/attribute was dropped, renamed, or
duplicated. **Gap**: no automated regression test exists for the
Terraform layer; a `terraform validate` (or `terraform plan` against a
scratch project) run would be the natural follow-up if CI budget allows.

### `templates/dashboard.html` + `templates/partials/*`

Covered indirectly: `tests/test_dashboard.py::TestDashboardPage` renders
the real template (via `main.app.test_client().get("/dashboard")`) under
several `budget`/`bq_configured` combinations and asserts on rendered
content, so a broken `{% include %}` or missing partial would fail these
tests today. It does not, however, byte-diff old vs. new output — that
verification (Jinja2 rendered directly, whitespace-normalized,
byte-identical) was done once during the refactor itself and is not
captured as a regression test. **Gap**: no standing test pins the
rendered HTML output; a template partial could silently change output
composition while still returning 200 with the strings the current
assertions check for.

## Known gaps (not addressed in this pass)

1. **`tests/test_cloud_management_client.py` (8 tests, all skipped)** —
   integration tests that need a running CloudManagement instance and a
   real report token (`CLOUDMANAGEMENT_REPORT_TOKEN_AIRICHARDMOON`).
   These are the only tests touching `_lifecycle.py` (context-manager
   enter/exit/flush-on-exit), `_transport.py` (HTTP delivery/retry),
   `_intent_ops.py`, `_actual_ops.py`, `errors.py`, and most of
   `context.py`. This is a pre-existing gap, unchanged by the refactor —
   flagging it here because the refactor split these modules apart, so
   the gap now spans more files than before, but the effective test
   coverage is identical (same 8 tests, same skip reason, before and
   after).
2. **Terraform** — no automated test of any kind; see above.
3. **Dashboard HTML** — no byte-level regression test of rendered output;
   see above.

## What changed in this test pass

- Reconstructed the `dev` branch tree from the GitLab REST API
  (`repository/tree` + `repository/blobs/:sha`, 94 blobs) into a local
  checkout since `git clone` is not usable in this environment.
- Ran the existing suite unmodified against that checkout: confirmed the
  refactor's claimed baseline (112 passed / 8 skipped) exactly.
- Added `tests/test_admin_routes.py` (13 tests, all passing) to close the
  coverage gap on `admin_routes.py`, the one new file introduced by the
  refactor that had zero prior test coverage (it is a straight
  extraction of routes that lived inline in the old `main.py`, but no
  test happened to exercise them before or after the move).
- Added this `TEST_PLAN.md`.
- No production code was modified in this pass.
