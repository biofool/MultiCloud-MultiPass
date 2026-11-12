"""Write-side intent/actual routes: POST /api/v1/intent (declare) and
POST /api/v1/actual (report).

Split out of ``intent_routes.py`` during the ≤250-line refactor — pure
structural move, no behavior change. Both handlers attach to the shared
``bp`` blueprint and use the shared ``_int_field``/``_float_field``
parsers from ``intent_routes``, which imports this module at the bottom
so importing ``intent`` still registers every route.

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
from intent_models import Intent, Actual
from intent_storage import (
    _gen_id,
    _now_iso,
    get_intent,
    list_actuals,
    save_actual,
    save_declared_intent,
    save_intent,
)
from intent_detection import check_intent_overrun, check_project_budget, project_month_spend
from intent_kill import kill_intent
from intent_auth import _validate_token
from intent_routes import bp, _VALID_REPORTED_STATUSES, _TERMINAL_INTENT_STATUSES, _int_field, _float_field


@bp.route("/api/v1/intent", methods=["POST"])
def declare_intent():
    """Declare expected API usage before making calls."""
    data = flask.request.get_json(silent=True)
    if not data:
        return flask.jsonify({"error": "not JSON"}), 400

    project_id = data.get("project_id", "")
    if not project_id:
        return flask.jsonify({"error": "missing project_id"}), 400

    if not _validate_token(project_id):
        return flask.jsonify({"error": "unauthorized"}), 401

    intent_id = data.get("intent_id") or _gen_id("int")
    # A caller-chosen intent_id must not overwrite another project's intent
    # (CISO-117). The check runs inside save_declared_intent's write — on
    # Firestore it is one transaction, because a read here would race a
    # concurrent declare for the same id (issue #16).
    now = _now_iso()

    if min(_int_field(data, "expected_calls"), _int_field(data, "rate_limit_rpm"),
           _float_field(data, "expected_cost_usd")) < 0:
        return flask.jsonify({"error": "numeric fields must be >= 0"}), 400

    intent = Intent(
        intent_id=intent_id,
        project_id=project_id,
        source_repo=data.get("source_repo", ""),
        job_id=data.get("job_id", ""),
        job_name=data.get("job_name", ""),
        provider=data.get("provider", ""),
        api=data.get("api", ""),
        expected_calls=_int_field(data, "expected_calls"),
        expected_cost_usd=_float_field(data, "expected_cost_usd"),
        expected_tokens=data.get("expected_tokens"),
        rate_limit_rpm=_int_field(data, "rate_limit_rpm"),
        window_start=data.get("window_start", ""),
        window_end=data.get("window_end", ""),
        kill=data.get("kill", {}),
        metadata=data.get("metadata", {}),
        approved=True,
        status="declared",
        created_at=now,
        updated_at=now,
    )

    # Check project budget — deny if already exceeded
    budget_check = check_project_budget(project_id)
    if budget_check:
        intent.approved = False
        intent.status = "denied"
        if save_declared_intent(intent) is not None:
            return flask.jsonify({"error": "intent_id already in use"}), 409
        log.warning(json.dumps({"event": "intent_denied", "intent_id": intent_id, "reason": budget_check}))
        return flask.jsonify({
            "intent_id": intent_id,
            "approved": False,
            "reason": budget_check["rule"],
            "budget_remaining_usd": 0,
        }), 200

    if save_declared_intent(intent) is not None:
        return flask.jsonify({"error": "intent_id already in use"}), 409
    log.info(json.dumps({"event": "intent_declared", "intent_id": intent_id, "project": project_id, "provider": intent.provider}))

    # Compute budget remaining — same month-scoped latest-per-intent
    # accounting as check_project_budget (issue #5: a naive sum over every
    # report double-counts cumulative incremental reports).
    acct = registry.get_account(project_id)
    spent = project_month_spend(project_id)
    budget_remaining = (acct.budget_amount_usd - spent) if acct else 0

    return flask.jsonify({
        "intent_id": intent_id,
        "approved": True,
        "budget_remaining_usd": round(budget_remaining, 2),
        "kill_switch_armed": True,
        "warnings": [],
    }), 200


@bp.route("/api/v1/actual", methods=["POST"])
def report_actual():
    """Report actual API usage (post-call or incremental)."""
    data = flask.request.get_json(silent=True)
    if not data:
        return flask.jsonify({"error": "not JSON"}), 400

    project_id = data.get("project_id", "")
    intent_id = data.get("intent_id", "")
    if not project_id or not intent_id:
        return flask.jsonify({"error": "missing project_id or intent_id"}), 400

    if not _validate_token(project_id):
        return flask.jsonify({"error": "unauthorized"}), 401

    intent = get_intent(intent_id)
    if intent is None or intent.project_id != project_id:
        # Cross-tenant check: the token is scoped to the body's project_id —
        # without this, project A's token could report against (and trigger
        # kill_intent on) project B's job. 404, not 403, so probing an
        # unguessable id leaks nothing.
        return flask.jsonify({"error": "intent not found", "intent_id": intent_id}), 404

    # Determine sequence number for incremental reports
    existing_actuals = list_actuals(intent_id=intent_id)
    seq = max((a.sequence for a in existing_actuals), default=-1) + 1

    # Status is the client's job outcome only. Hub lifecycle states must not
    # be writable by the caller (#198).
    reported_status = str(data.get("status", "completed"))
    if reported_status not in _VALID_REPORTED_STATUSES:
        return flask.jsonify({"error": "invalid status", "valid": sorted(_VALID_REPORTED_STATUSES)}), 400
    actual_calls = _int_field(data, "actual_calls")
    actual_cost_usd = _float_field(data, "actual_cost_usd")
    if actual_calls < 0 or actual_cost_usd < 0:
        return flask.jsonify({"error": "numeric fields must be >= 0"}), 400

    actual = Actual(
        actual_id=_gen_id("act"),
        intent_id=intent_id,
        project_id=project_id,
        job_id=data.get("job_id", intent.job_id),
        provider=data.get("provider", intent.provider),
        api=data.get("api", intent.api),
        actual_calls=actual_calls,
        actual_cost_usd=actual_cost_usd,
        actual_tokens=data.get("actual_tokens"),
        status=reported_status,
        started_at=data.get("started_at", ""),
        ended_at=data.get("ended_at", ""),
        sequence=seq,
        client_seq=_int_field(data, "client_seq"),
        created_at=_now_iso(),
    )
    save_actual(actual)

    # Update intent status, except when the intent already reached a terminal
    # state. A later report must not regress it (#198).
    if intent.status not in _TERMINAL_INTENT_STATUSES:
        intent.status = actual.status
        intent.updated_at = _now_iso()
        save_intent(intent)
    else:
        log.warning(json.dumps({
            "event": "intent_status_regression_blocked",
            "intent_id": intent_id,
            "current_status": intent.status,
            "reported_status": actual.status,
        }))

    log.info(json.dumps({
        "event": "actual_reported",
        "intent_id": intent_id,
        "project": project_id,
        "calls": actual.actual_calls,
        "cost": actual.actual_cost_usd,
        "status": actual.status,
        "sequence": seq,
    }))

    # Check for overrun — kill if exceeded (regardless of status;
    # a completed job that made 2x the expected calls still needs killing)
    overrun = check_intent_overrun(intent)
    if overrun:
        kill_result = kill_intent(intent, reason=overrun["rule"], rule=overrun["rule"])
        return flask.jsonify({
            "actual_id": actual.actual_id,
            "overrun_detected": True,
            "overrun": overrun,
            "kill_result": kill_result,
        }), 200

    # Check project budget
    budget_check = check_project_budget(project_id)
    if budget_check:
        kill_result = kill_intent(intent, reason=budget_check["rule"], rule=budget_check["rule"])
        return flask.jsonify({
            "actual_id": actual.actual_id,
            "budget_exceeded": True,
            "budget_check": budget_check,
            "kill_result": kill_result,
        }), 200

    return flask.jsonify({
        "actual_id": actual.actual_id,
        "overrun_detected": False,
        "status": actual.status,
    }), 200
