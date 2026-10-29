# File-Size Audit — dev branch (2026)

Audit of source files over 400 lines on `dev`, per
`conventions/coding-patterns.md` ("File size & responsibility").
Excludes `.ai-context/`, `vendor/`, `node_modules/`, and generated output.

This repo is already well-decomposed: the original monolithic `intent.py`
was split into `intent_models.py`, `intent_auth.py`, `intent_storage.py`,
`intent_detection.py`, `intent_kill.py`, and `intent_routes.py`. Only two
files exceed the 400-line soft threshold — both are borderline.

Legend — **AI-edit risk**: likelihood that an AI/agent edit touches
unintended code. **Priority**: refactor urgency.

## Production modules

| File | Lines | Responsibilities | AI-edit risk | Priority | Suggested split |
|---|---|---|---|---|---|
| `intent_routes.py` | 515 | All intent/actual Flask blueprint handlers in one file (create intent, report actual, expected costs, list, kill) | Med — one responsibility family (HTTP handlers), but 11 handlers share auth + storage imports | **Low** | Only if it grows past ~700: split by handler group (`intent_routes/write.py` for POSTs, `intent_routes/read.py` for GETs) |

## Vendored / canonical

| File | Lines | Note |
|---|---|---|
| `cloud_management_client/` | — | Package files individually under threshold. Canonical client; syncs to sibling repos — treat any split as coordinated, not local. |

## Tests

| File | Lines | Priority |
|---|---|---|
| `tests/test_intent.py` | 647 | Low — cohesive coverage of the intent_* module cluster; split alongside `intent_routes.py` if that ever splits |

## Top priorities

1. `intent_routes.py` (515) — monitor; no action needed yet.
2. `tests/test_intent.py` (647) — monitor; split only with its target module.

No high-priority refactors. The codebase is within target size bounds.
