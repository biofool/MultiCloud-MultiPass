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
    list_expected_costs,
    list_intents,
    save_actual,
    save_intent,
    sum_actuals_for_intent,
)
from intent_detection import check_intent_overrun, check_project_budget
from intent_kill import kill_intent
from intent_auth import _validate_token

# ---------------------------------------------------------------------------
# Flask blueprint
# ---------------------------------------------------------------------------

bp = flask.Blueprint("intent", __name__)


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
    now = _now_iso()

    intent = Intent(
        intent_id=intent_id,
        project_id=project_id,
        source_repo=data.get("source_repo", ""),
        job_id=data.get("job_id", ""),
        job_name=data.get("job_name", ""),
        provider=data.get("provider", ""),
        api=data.get("api", ""),
        expected_calls=int(data.get("expected_calls", 0)),
        expected_cost_usd=float(data.get("expected_cost_usd", 0)),
        expected_tokens=data.get("expected_tokens"),
        rate_limit_rpm=int(data.get("rate_limit_rpm", 0)),
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
        save_intent(intent)
        log.warning(json.dumps({"event": "intent_denied", "intent_id": intent_id, "reason": budget_check}))
        return flask.jsonify({
            "intent_id": intent_id,
            "approved": False,
            "reason": budget_check["rule"],
            "budget_remaining_usd": 0,
        }), 200

    save_intent(intent)
    log.info(json.dumps({"event": "intent_declared", "intent_id": intent_id, "project": project_id, "provider": intent.provider}))

    # Compute budget remaining
    acct = registry.get_account(project_id)
    actuals = list_actuals(project_id=project_id)
    spent = sum(a.actual_cost_usd for a in actuals)
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
    if intent is None:
        return flask.jsonify({"error": "intent not found", "intent_id": intent_id}), 404

    # Determine sequence number for incremental reports
    existing_actuals = list_actuals(intent_id=intent_id)
    seq = max((a.sequence for a in existing_actuals), default=-1) + 1

    actual = Actual(
        actual_id=_gen_id("act"),
        intent_id=intent_id,
        project_id=project_id,
        job_id=data.get("job_id", intent.job_id),
        provider=data.get("provider", intent.provider),
        api=data.get("api", intent.api),
        actual_calls=int(data.get("actual_calls", 0)),
        actual_cost_usd=float(data.get("actual_cost_usd", 0)),
        actual_tokens=data.get("actual_tokens"),
        status=data.get("status", "completed"),
        started_at=data.get("started_at", ""),
        ended_at=data.get("ended_at", ""),
        sequence=seq,
        created_at=_now_iso(),
    )
    save_actual(actual)

    # Update intent status
    intent.status = actual.status
    intent.updated_at = _now_iso()
    save_intent(intent)

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


@bp.route("/api/v1/expected-costs/<project_id>", methods=["GET"])
def get_expected_costs(project_id: str):
    """Pull authoritative expected costs for a project.

    Requires the same per-project bearer token as declare/report/kill —
    per docs/per-repo-api-specs.md this endpoint has always been
    documented as requiring `Authorization: Bearer <token>`; the
    implementation was simply missing the check.
    """
    if not _validate_token(project_id):
        return flask.jsonify({"error": "unauthorized"}), 401

    costs = list_expected_costs(project_id=project_id)
    return flask.jsonify({
        "project_id": project_id,
        "updated_at": costs[0].updated_at if costs else "",
        "providers": {
            c.provider: {
                "unit_cost_usd": c.unit_cost_usd,
                "free_tier_remaining_calls": c.free_tier_remaining_calls,
                "free_tier_reset": c.free_tier_reset,
                "expected_remaining_monthly_usd": c.expected_remaining_monthly_usd,
                "calibration_delta": c.calibration_delta,
                "pricing": c.pricing,
            }
            for c in costs
        },
    }), 200


@bp.route("/api/v1/intents", methods=["GET"])
def list_all_intents():
    """List active intents (for dashboard). Optional ?project_id= and ?status= filters.

    Deliberately left without a per-project bearer-token check: unlike
    expected-costs/list_project_intents/kill (which are always scoped to
    one project_id and part of the documented sub-project contract in
    docs/per-repo-api-specs.md), this is a cross-project dashboard
    listing with no single project to check a token against — the same
    trust boundary as dashboard.py's /api/accounts, /api/summary, etc.,
    which rely entirely on the edge auth gate in front of this service.
    Left as-is rather than bolted onto a token model that doesn't fit;
    flagged for a future decision if this needs its own admin auth.
    """
    project_id = flask.request.args.get("project_id")
    status = flask.request.args.get("status")
    intents = list_intents(project_id=project_id, status=status)
    return flask.jsonify({
        "intents": [i.to_dict() for i in intents],
        "count": len(intents),
    }), 200


@bp.route("/api/v1/intents/<project_id>", methods=["GET"])
def list_project_intents(project_id: str):
    """List intents for a specific project.

    Project-scoped, like /api/v1/kill and /api/v1/expected-costs — same
    bearer-token check applies so one project can't enumerate another's
    intent/spend data by guessing project_id.
    """
    if not _validate_token(project_id):
        return flask.jsonify({"error": "unauthorized"}), 401

    intents = list_intents(project_id=project_id)
    result = []
    for intent in intents:
        summed = sum_actuals_for_intent(intent.intent_id)
        result.append({
            **intent.to_dict(),
            "actual_calls": summed["actual_calls"],
            "actual_cost_usd": round(summed["actual_cost_usd"], 4),
            "actual_tokens": summed["actual_tokens"],
            "variance_pct": round(
                (summed["actual_calls"] / intent.expected_calls * 100) if intent.expected_calls else 0, 1
            ),
        })
    return flask.jsonify({"intents": result, "count": len(result)}), 200


@bp.route("/api/v1/kill/<intent_id>", methods=["POST"])
def manual_kill(intent_id: str):
    """Manual kill override (dashboard button).

    Requires the same bearer token auth as declare/report endpoints —
    the token must match the intent's project_id.
    """
    intent = get_intent(intent_id)
    if intent is None:
        return flask.jsonify({"error": "intent not found"}), 404

    if not _validate_token(intent.project_id):
        return flask.jsonify({"error": "unauthorized"}), 401

    result = kill_intent(intent, reason="manual_override", rule="manual")
    return flask.jsonify(result), 200
