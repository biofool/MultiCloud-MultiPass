"""Budget-alert pipeline: kill switch orchestration, project protection
checks, and the Pub/Sub alert handler.

Split out of ``main.py`` during the ≤250-line refactor — pure structural
move, no behavior change. Everything reads main's config flags and
action functions via ``import main`` + qualified access
(``main.DRY_RUN``, ``main.disable_cloud_run_services`` etc.), so
test-mutated values and ``@patch("main.<name>")`` keep working exactly as
they did when these functions lived in main — same live-value reason as
``killswitch_actions.py``.

Maintenance-mode snapshot (upstream convergence): the canonical
implementation of this module is maintained upstream in
``biofool/CloudManagement``. This copy is a frozen snapshot kept so
this repo stays standalone-runnable — it receives security fixes and
upstream backports only. See docs/convergence.md.
"""

from __future__ import annotations

import json
from typing import Any

import main
import registry

# ---------------------------------------------------------------------------
# Kill switch orchestration — shared by the budget-alert path and the
# real-time quota poller (poller.py)
# ---------------------------------------------------------------------------

def execute_killswitch(project_id: str, reason: str) -> list[dict]:
    """Run every enabled kill switch action against a single project.

    `reason` is a short machine-readable tag (e.g. "budget_alert",
    "quota_spike") included in logs so the trigger source is traceable.

    **Self-protection:** If `project_id` matches `SELF_PROJECT_ID`, the
    kill switch is NOT executed.  CloudManagement must never kill its own
    infrastructure — doing so creates a feedback loop where the monitor
    generates cost by being repeatedly invoked (Cloud Scheduler cold
    starts) while unable to monitor other projects.  A critical log is
    emitted so operators are alerted via log-based metrics.
    """
    if is_self_project(project_id):
        main.log.critical(json.dumps({
            "event": "self_kill_blocked",
            "project": project_id,
            "reason": reason,
            "message": "Kill switch blocked on self project — would create feedback loop. "
                       "Investigate hub project costs manually.",
        }))
        return []

    main.log.info(json.dumps({"event": "processing_project", "project": project_id, "reason": reason, "dry_run": main.DRY_RUN}))

    all_actions: list[dict] = []

    actions = main.disable_cloud_run_services(project_id)
    all_actions.extend({"action": "scale_to_zero", "target": a} for a in actions)

    actions = main.pause_scheduler_jobs(project_id)
    all_actions.extend({"action": "pause_scheduler", "target": a} for a in actions)

    actions = main.disable_build_triggers(project_id)
    all_actions.extend({"action": "disable_trigger", "target": a} for a in actions)

    actions = main.stop_compute_instances(project_id)
    all_actions.extend({"action": "stop_instance", "target": a} for a in actions)

    actions = main.scale_down_gke_clusters(project_id)
    all_actions.extend({"action": "scale_down_gke", "target": a} for a in actions)

    actions = main.revoke_api_keys(project_id)
    all_actions.extend({"action": "revoke_api_key", "target": a} for a in actions)

    if main.disable_billing(project_id):
        all_actions.append({"action": "disable_billing", "target": project_id})

    main.log.info(json.dumps({
        "event": "killswitch_complete",
        "project": project_id,
        "reason": reason,
        "actions_taken": len(all_actions),
        "dry_run": main.DRY_RUN,
        "actions": all_actions[:50],  # cap log size
    }))
    return all_actions


def is_project_protected(project_id: str) -> bool:
    """True if project_id must never be touched (env allowlist or registry)."""
    return project_id in main.ALLOWLIST or registry.is_allowlisted(project_id)


def is_self_project(project_id: str) -> bool:
    """True if project_id is the hub project, by logical id or by the GCP
    project id of an aliased account (CISO-118)."""
    if not main.SELF_PROJECT_ID:
        return False
    if project_id == main.SELF_PROJECT_ID:
        return True
    acct = registry.get_account(project_id)
    return bool(acct and acct.gcp_project_id == main.SELF_PROJECT_ID)


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------

def process_alert(envelope: dict[str, Any]) -> tuple[int, str]:
    """Process a Pub/Sub push envelope. Returns (HTTP status, message)."""
    # Validate envelope structure
    if not isinstance(envelope, dict) or "message" not in envelope:
        main.log.warning(json.dumps({"event": "malformed_envelope"}))
        return (400, "Malformed envelope")

    message = envelope["message"]
    msg_id = message.get("messageId", "")

    # Dedup
    if msg_id and main._is_duplicate(msg_id):
        main.log.info(json.dumps({"event": "duplicate", "message_id": msg_id}))
        return (200, "Duplicate — already processed")

    # Parse
    alert = main.parse_pubsub_message(envelope)
    if alert is None:
        return (400, "Failed to parse alert")

    main.log.info(json.dumps({
        "event": "alert_received",
        "message_id": msg_id,
        "budget": alert.budget_name,
        "type": alert.alert_type,
        "threshold": alert.threshold_percent,
        "actual": alert.actual_spend,
        "forecast": alert.forecasted_spend,
        "budget_amount": alert.budget_amount,
        "projects": alert.project_ids,
        "dry_run": main.DRY_RUN,
    }))

    # Evaluate threshold
    if not main.should_take_action(alert):
        main.log.info(json.dumps({"event": "below_threshold", "threshold": alert.threshold_percent}))
        return (200, "Below action threshold")

    # Self-protection: warn on self-project budget alerts but never kill
    self_alerts = [p for p in alert.project_ids if main.SELF_PROJECT_ID and p == main.SELF_PROJECT_ID]
    if self_alerts:
        main.log.warning(json.dumps({
            "event": "self_budget_alert",
            "projects": self_alerts,
            "threshold": alert.threshold_percent,
            "actual": alert.actual_spend,
            "budget_amount": alert.budget_amount,
            "message": "Hub project budget alert — investigate manually. "
                       "Kill switch is blocked on self project to prevent feedback loop.",
        }))

    # Determine target projects (env ALLOWLIST or registry allowlist protects a project)
    # Also exclude self project — execute_killswitch() has a hard block, but
    # filtering here avoids unnecessary log noise.
    target_projects = [
        p for p in alert.project_ids
        if not is_project_protected(p) and not is_self_project(p)
    ]
    skipped = [p for p in alert.project_ids if is_project_protected(p)]

    if skipped:
        main.log.info(json.dumps({"event": "allowlist_skip", "projects": skipped}))

    if not target_projects:
        main.log.info(json.dumps({"event": "no_targets", "reason": "all_allowlisted"}))
        return (200, "All projects allowlisted")

    all_actions: list[dict] = []
    for project_id in target_projects:
        all_actions.extend(main.execute_killswitch(project_id, reason="budget_alert"))

    return (200, f"Processed — {len(all_actions)} actions ({'dry-run' if main.DRY_RUN else 'live'})")
