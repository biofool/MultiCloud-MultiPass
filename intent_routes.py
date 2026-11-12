"""Flask blueprint for the intent/actual reporting protocol.

Split out of the original monolithic ``intent.py`` — structural move,
plus one security fix on top (see below). Every name each handler needs
(dataclasses, storage functions, detection functions, ``kill_intent``,
``_validate_token``, ``log``) is imported directly by name — none of it
needs the ``_intent_mod`` qualification used in the leaf implementation
modules, since none of these handlers read a raw env-derived config
constant directly (they all go through function calls that handle that
internally), and several of them use ``intent`` as a local variable
name (e.g. ``intent = get_intent(intent_id)``), which would shadow a
module import anyway.

Security fix (post-refactor review): ``get_expected_costs`` and
``list_project_intents`` are project-scoped GET endpoints that used to
perform no auth check at all, unlike every other route in this
blueprint. docs/per-repo-api-specs.md has always documented
`Authorization: Bearer <token>` as required on the expected-costs
endpoint (and it's the same class of per-project data as
list_project_intents/kill), so both now call ``_validate_token``
exactly like ``declare_intent``/``report_actual``/``manual_kill``.
``list_all_intents`` is intentionally left as-is — see its own
docstring for why.

Further split during the ≤250-line refactor: this module keeps the
blueprint, the status sets, and the shared parsers/serializers; the
route handlers moved to ``intent_routes_write.py`` (declare/report),
``intent_routes_read.py`` (list/expected-costs/manual-kill), and
``intent_routes_client.py`` (the cloud_management_client issue-#1
endpoints). They attach to the same ``bp`` via the bottom-of-file
imports below, so ``from intent_routes import bp`` still registers
every route.

Maintenance-mode snapshot (upstream convergence): the canonical
implementation of this module is maintained upstream in
``biofool/CloudManagement``. This copy is a frozen snapshot kept so
this repo stays standalone-runnable — it receives security fixes and
upstream backports only. See docs/convergence.md.
"""

from __future__ import annotations

import flask

from intent_models import Intent

# ---------------------------------------------------------------------------
# Flask blueprint
# ---------------------------------------------------------------------------

bp = flask.Blueprint("intent", __name__)

# Client-reported job outcomes on POST /api/v1/actual. Hub lifecycle states
# (stale/deferred/denied/expired/killed/kill_failed) are assigned by the hub,
# never by the caller (CloudManagement #198).
_VALID_REPORTED_STATUSES = {"running", "completed", "failed", "cancelled"}
# A killed/denied/expired intent must not be regressed by a later report.
# The actual is still stored; only the intent status write is refused (#198).
_TERMINAL_INTENT_STATUSES = {"killed", "kill_failed", "denied", "expired"}


def _int_field(data: dict, key: str, default=0):
    """Parse an int field or 400 — bare int() on user input 500s."""
    try:
        return int(data.get(key, default))
    except (TypeError, ValueError):
        flask.abort(400, f"{key} must be an integer")


def _float_field(data: dict, key: str, default=0.0):
    try:
        return float(data.get(key, default))
    except (TypeError, ValueError):
        flask.abort(400, f"{key} must be a number")


def _public_intent_dict(intent: Intent) -> dict:
    """to_dict() with the kill descriptor's credentials stripped.

    ``kill.url``/``kill.headers`` carry callback auth material declared by
    the project — endpoints readable without the project's token
    (unguessable-id GET, dashboard list) must not serve them. ``type`` is
    enough for dashboards.
    """
    d = intent.to_dict()
    kill = d.get("kill") or {}
    d["kill"] = {"type": kill.get("type", "")} if kill else {}
    return d


# ---------------------------------------------------------------------------
# Route modules — importing them attaches their handlers to ``bp``. Kept
# at the bottom so this module's blueprint and helpers exist before the
# siblings' ``from intent_routes import ...`` lines run.
# ---------------------------------------------------------------------------

import intent_routes_write  # noqa: E402,F401
import intent_routes_read  # noqa: E402,F401
import intent_routes_client  # noqa: E402,F401
