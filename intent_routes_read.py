"""Read-side and manual-override intent/actual routes: GET
/api/v1/expected-costs/<project_id>, GET /api/v1/intents(+/<project_id>),
and POST /api/v1/kill/<intent_id>.

Split out of ``intent_routes.py`` during the ≤250-line refactor — pure
structural move, no behavior change. Handlers attach to the shared
``bp`` blueprint and use the shared ``_public_intent_dict`` helper from
``intent_routes``, which imports this module at the bottom so importing
``intent`` still registers every route.

Maintenance-mode snapshot (upstream convergence): the canonical
implementation of this module is maintained upstream in
``biofool/CloudManagement``. This copy is a frozen snapshot kept so
this repo stays standalone-runnable — it receives security fixes and
upstream backports only. See docs/convergence.md.
"""

from __future__ import annotations

import flask

from intent_storage import (
    get_intent,
    list_expected_costs,
    list_intents,
    sum_actuals_for_intent,
)
from intent_kill import kill_intent
from intent_auth import _validate_token
from intent_routes import bp, _public_intent_dict


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
        "intents": [_public_intent_dict(i) for i in intents],
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
