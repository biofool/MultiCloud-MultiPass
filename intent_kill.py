"""Kill execution for the intent/actual protocol: routes a kill
descriptor to the right provider and records the outcome.

Split out of the original monolithic ``intent.py`` — pure structural
move, no behavior change. Everything this function touches
(``save_kill_event``, ``save_intent``, ``_gen_id``, ``_now_iso``,
``log``) is a plain function/logger reference — none of it is
reload-sensitive config — so plain direct imports are used rather than
the ``_intent_mod`` qualification pattern used elsewhere in this split
(and its parameter is named ``intent``, which would shadow a module
import anyway).

Maintenance-mode snapshot (upstream convergence): the canonical
implementation of this module is maintained upstream in
``biofool/CloudManagement``. This copy is a frozen snapshot kept so
this repo stays standalone-runnable — it receives security fixes and
upstream backports only. See docs/convergence.md.
"""

from __future__ import annotations

import json
from typing import Any

import registry
from intent import log
from intent_models import Intent
from intent_storage import save_kill_event, save_intent, _gen_id, _now_iso

# ---------------------------------------------------------------------------
# Kill execution — route to providers
# ---------------------------------------------------------------------------


def kill_intent(intent: Intent, reason: str, rule: str = "") -> dict[str, Any]:
    """Kill the job associated with an intent via its kill descriptor."""
    from providers import registry as provider_registry
    # Deferred — main imports intent_routes which imports this module, so a
    # top-level import would be circular. Same pattern as provider_registry.
    import main

    # Protection gate — the poll path (main.py) refuses to touch SELF_PROJECT_ID
    # or allowlisted projects; the intent path must apply the same rule, or an
    # overrun on a hub/allowlisted project would still fire the kill provider.
    if (main.SELF_PROJECT_ID and intent.project_id == main.SELF_PROJECT_ID) \
            or main.is_project_protected(intent.project_id):
        log.critical(json.dumps({
            "event": "protected_kill_blocked",
            "intent_id": intent.intent_id,
            "project_id": intent.project_id,
            "reason": reason,
            "message": "Intent-path kill blocked on protected project — "
                       "investigate its spend manually.",
        }))
        return {"killed": False, "reason": "project is protected",
                "project_id": intent.project_id}

    kill_desc = intent.kill or {}
    if not kill_desc:
        # Fall back to registry jobs config — match by job_id prefix
        acct = registry.get_account(intent.project_id)
        if acct and acct.jobs:
            for job_cfg in acct.jobs:
                prefix = job_cfg.get("job_id_prefix", "")
                if prefix and intent.job_id.startswith(prefix):
                    kill_desc = job_cfg.get("kill", {})
                    break

    if not kill_desc:
        log.warning(json.dumps({
            "event": "kill_no_descriptor",
            "intent_id": intent.intent_id,
            "project_id": intent.project_id,
        }))
        return {"killed": False, "reason": "no kill descriptor available"}

    kill_desc = {**kill_desc, "job_id": intent.job_id, "project_id": intent.project_id}
    result = provider_registry.kill_job(kill_desc, reason)

    # Record the kill event
    kill_event = {
        "kill_id": _gen_id("kill"),
        "intent_id": intent.intent_id,
        "project_id": intent.project_id,
        "job_id": intent.job_id,
        "reason": reason,
        "rule": rule,
        "kill_type": kill_desc.get("type", ""),
        "killed": result.killed,
        "detail": result.detail,
        "error": result.error,
        "timestamp": _now_iso(),
    }
    save_kill_event(kill_event)

    # Update intent status — only a confirmed kill is "killed"; a failed
    # attempt must not leave the intent reporting "killed" while the job
    # keeps running (dashboards believed it, and nothing would retry).
    intent.status = "killed" if result.killed else "kill_failed"
    intent.updated_at = _now_iso()
    save_intent(intent)

    log.warning(json.dumps({
        "event": "job_killed" if result.killed else "kill_failed",
        **kill_event,
    }))
    return kill_event
