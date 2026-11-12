"""Client-facing intent/actual routes required by
cloud_management_client v0.12.0 (issue #1): budget status/admission,
single-intent fetch, kill orders, and exposure reporting.

Split out of ``intent_routes.py`` during the ≤250-line refactor — pure
structural move, no behavior change. The original module comment:
the client calls four endpoints that were not previously implemented on
the hub side. Without them, check_budget()/can_run() fail-closed to
"denied" (indistinguishable from "budget exhausted"), get_intent()/
wait_for_reschedule() 404, check_kill_orders() returns empty, and
report_exposure() 404. These implementations close that gap.

Handlers attach to the shared ``bp`` blueprint and use the shared
``_float_field``/``_public_intent_dict`` helpers from ``intent_routes``,
which imports this module at the bottom so importing ``intent`` still
registers every route.

Maintenance-mode snapshot (upstream convergence): the canonical
implementation of this module is maintained upstream in
``biofool/CloudManagement``. This copy is a frozen snapshot kept so
this repo stays standalone-runnable — it receives security fixes and
upstream backports only. See docs/convergence.md.
"""

from __future__ import annotations

import json

import flask

import registry
from intent import log
from intent_storage import (
    get_intent,
    list_kill_events,
)
from intent_detection import project_month_spend
from intent_auth import _validate_token
from intent_routes import bp, _float_field, _public_intent_dict


@bp.route("/api/v1/budget/<project_id>", methods=["GET"])
def get_project_budget(project_id: str):
    """Read-only budget status for a project.

    Returns the project's configured budget, month-to-date spend, and
    whether it is over budget. Used by ``check_budget()`` with
    ``expected_cost_usd == 0`` and by ``can_run()`` for zero-cost probes.
    """
    if not _validate_token(project_id):
        return flask.jsonify({"error": "unauthorized"}), 401

    acct = registry.get_account(project_id)
    if not acct or acct.budget_amount_usd <= 0:
        return flask.jsonify({
            "project_id": project_id,
            "budget_configured": False,
            "budget_amount_usd": 0,
            "spent_this_month_usd": 0,
            "budget_remaining_usd": 0,
            "over_budget": False,
        }), 200

    spent = project_month_spend(project_id)
    remaining = acct.budget_amount_usd - spent
    over = spent > acct.budget_amount_usd

    return flask.jsonify({
        "project_id": project_id,
        "budget_configured": True,
        "budget_amount_usd": acct.budget_amount_usd,
        "spent_this_month_usd": round(spent, 4),
        "budget_remaining_usd": round(remaining, 4),
        "over_budget": over,
    }), 200


@bp.route("/api/v1/budget/<project_id>", methods=["POST"])
def check_project_budget_admission(project_id: str):
    """Admission decision: can the project afford ``expected_cost_usd`` right now?

    Used by ``check_budget()`` with ``expected_cost_usd > 0`` and by
    ``can_run()``. Returns ``admit`` (True/False), ``deferred`` (True if
    the cost would push the project over but it is not yet over), and
    ``suggested_retry_at`` (empty for now — no scheduling logic yet).
    """
    if not _validate_token(project_id):
        return flask.jsonify({"error": "unauthorized"}), 401

    data = flask.request.get_json(silent=True) or {}
    expected_cost = _float_field(data, "expected_cost_usd")

    acct = registry.get_account(project_id)
    if not acct or acct.budget_amount_usd <= 0:
        # No budget configured — admit (no budget gate to enforce).
        return flask.jsonify({
            "project_id": project_id,
            "admit": True,
            "deferred": False,
            "budget_configured": False,
            "budget_amount_usd": 0,
            "spent_this_month_usd": 0,
            "budget_remaining_usd": 0,
            "budget_short_usd": 0,
            "suggested_retry_at": "",
        }), 200

    spent = project_month_spend(project_id)
    remaining = acct.budget_amount_usd - spent
    projected = remaining - expected_cost

    if remaining <= 0:
        # Already over budget — deny.
        admit = False
        deferred = False
    elif projected < 0:
        # Not yet over, but this cost would push it over — defer.
        admit = False
        deferred = True
    else:
        admit = True
        deferred = False

    return flask.jsonify({
        "project_id": project_id,
        "admit": admit,
        "deferred": deferred,
        "budget_configured": True,
        "budget_amount_usd": acct.budget_amount_usd,
        "spent_this_month_usd": round(spent, 4),
        "budget_remaining_usd": round(remaining, 4),
        "budget_short_usd": round(max(0, -projected), 4),
        "suggested_retry_at": "",
    }), 200


@bp.route("/api/v1/intent/<intent_id>", methods=["GET"])
def get_single_intent(intent_id: str):
    """Fetch a single intent by ID.

    Used by ``get_intent()`` and ``wait_for_reschedule()``. The intent_id
    is an unguessable token (``int_`` + 16 hex chars), so no project-scoped
    bearer token is required — same trust model as the client docstring
    describes.
    """
    intent = get_intent(intent_id)
    if intent is None:
        return flask.jsonify({"error": "intent not found", "intent_id": intent_id}), 404
    return flask.jsonify(_public_intent_dict(intent)), 200


@bp.route("/api/v1/kill-orders", methods=["GET"])
def list_kill_orders():
    """List kill orders targeting a project (client-polled kill channel).

    Used by ``check_kill_orders()``. The client polls this between
    reports for long-running jobs. Requires the per-project bearer token
    since it is scoped to one project_id.
    """
    project_id = flask.request.args.get("project_id", "")
    if not project_id:
        return flask.jsonify({"error": "missing project_id"}), 400

    if not _validate_token(project_id):
        return flask.jsonify({"error": "unauthorized"}), 401

    since = flask.request.args.get("since", "")
    events = list_kill_events(project_id=project_id, limit=200)
    if since:
        events = [e for e in events if e.get("timestamp", "") >= since]
    return flask.jsonify({
        "project_id": project_id,
        "kill_orders": events,
        "count": len(events),
    }), 200


@bp.route("/api/v1/exposure", methods=["POST"])
def report_exposure():
    """Report that an API key has been exposed and request rotation.

    Used by ``report_exposure()``. Logs the exposure event for manual
    follow-up — automated key rotation is not yet implemented, so this
    endpoint acknowledges the report and logs it at WARNING level.
    """
    data = flask.request.get_json(silent=True) or {}
    project_id = data.get("project_id", "")
    if not project_id:
        return flask.jsonify({"error": "missing project_id"}), 400

    if not _validate_token(project_id):
        return flask.jsonify({"error": "unauthorized"}), 401

    display_name = data.get("display_name", "")
    dry_run = data.get("dry_run", False)
    all_keys = data.get("all", True)
    enable_api = data.get("enable_api", False)

    log.warning(json.dumps({
        "event": "key_exposure_reported",
        "project_id": project_id,
        "display_name": display_name,
        "all_keys": all_keys,
        "dry_run": dry_run,
        "enable_api": enable_api,
        "action": "logged_only",
        "note": "automated rotation not yet implemented — manual follow-up required",
    }))

    return flask.jsonify({
        "project_id": project_id,
        "acknowledged": True,
        "dry_run": dry_run,
        "rotated": False,
        "new_keys": {},
        "note": "Exposure logged. Automated key rotation is not yet implemented — "
                "manual rotation via the provider console is required.",
    }), 200
