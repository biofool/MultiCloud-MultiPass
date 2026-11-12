"""Highest-blast-radius GCP kill switch actions: API key revocation,
GKE node-pool scale-down, and billing shutoff (the nuclear option).

Split out of ``killswitch_actions.py`` during the ≤250-line refactor —
pure structural move, no behavior change. Same ``import main`` +
qualified-access convention as that module: each function must see live,
possibly test-mutated, values of main's config flags (``main.DRY_RUN``,
``main.ENABLE_*``), and ``billing_v1`` is the same cached module object
main.py imports (or tests replace in ``sys.modules``), so patching either
import path patches the same target. ``killswitch_actions`` re-exports
these names so existing call sites keep working.

Maintenance-mode snapshot (upstream convergence): the canonical
implementation of this module is maintained upstream in
``biofool/CloudManagement``. This copy is a frozen snapshot kept so
this repo stays standalone-runnable — it receives security fixes and
upstream backports only. See docs/convergence.md.
"""

from __future__ import annotations

import json

from google.cloud import billing_v1

import main


def revoke_api_keys(project_id: str) -> list[str]:
    """Delete all active API keys in a project. Recoverable via UndeleteKey for 30 days."""
    if not main.ENABLE_API_KEY_REVOKE:
        return []
    from google.cloud import api_keys_v2

    actions = []
    client = api_keys_v2.ApiKeysClient()
    parent = f"projects/{project_id}/locations/global"
    try:
        keys = client.list_keys(parent=parent)
    except Exception as exc:
        main.log.warning(json.dumps({"event": "list_keys_error", "project": project_id, "error": str(exc)}))
        return []

    for key in keys:
        key_name = key.name  # projects/{proj}/locations/global/keys/{id}
        if getattr(key, "delete_time", None):
            continue  # already soft-deleted
        if main.DRY_RUN:
            main.log.info(json.dumps({"event": "dry_run", "action": "revoke_api_key", "key": key_name}))
            actions.append(key_name)
        else:
            try:
                client.delete_key(name=key_name)
                main.log.info(json.dumps({"event": "action", "action": "revoke_api_key", "key": key_name, "status": "done"}))
                actions.append(key_name)
            except Exception as exc:
                main.log.error(json.dumps({"event": "action_error", "action": "revoke_api_key", "key": key_name, "error": str(exc)}))
    return actions


def scale_down_gke_clusters(project_id: str, zones_or_regions: list[str] | None = None) -> list[str]:
    """Resize all GKE node pools in a project to 0 nodes."""
    if not main.ENABLE_GKE_SCALE_DOWN:
        return []
    from google.cloud import container_v1

    actions = []
    client = container_v1.ClusterManagerClient()
    parent = f"projects/{project_id}/locations/-"  # "-" = all locations
    try:
        resp = client.list_clusters(parent=parent)
        clusters = resp.clusters
    except Exception as exc:
        main.log.warning(json.dumps({"event": "list_clusters_error", "project": project_id, "error": str(exc)}))
        return []

    for cluster in clusters:
        for pool in cluster.node_pools:
            if pool.initial_node_count == 0 and not any(
                ig for ig in getattr(pool, "instance_group_urls", [])
            ):
                continue
            pool_id = f"{cluster.name}/{pool.name}"
            if main.DRY_RUN:
                main.log.info(json.dumps({"event": "dry_run", "action": "scale_down_gke", "pool": pool_id}))
                actions.append(pool_id)
            else:
                try:
                    pool_name = (
                        f"projects/{project_id}/locations/{cluster.location}"
                        f"/clusters/{cluster.name}/nodePools/{pool.name}"
                    )
                    client.set_node_pool_size(name=pool_name, node_count=0)
                    main.log.info(json.dumps({"event": "action", "action": "scale_down_gke", "pool": pool_id, "status": "done"}))
                    actions.append(pool_id)
                except Exception as exc:
                    main.log.error(json.dumps({"event": "action_error", "action": "scale_down_gke", "pool": pool_id, "error": str(exc)}))
    return actions


def disable_billing(project_id: str) -> bool:
    """Disable billing on a project. Requires ENABLE_BILLING_SHUTOFF=true."""
    if not main.ENABLE_BILLING_SHUTOFF:
        main.log.warning(json.dumps({"event": "skip", "action": "disable_billing", "reason": "not_enabled"}))
        return False

    if main.DRY_RUN:
        main.log.info(json.dumps({"event": "dry_run", "action": "disable_billing", "project": project_id}))
        return True

    client = billing_v1.CloudBillingClient()
    try:
        # Get the billing account name for this project
        proj_billing = client.get_project_billing_info(name=f"projects/{project_id}")
        if not proj_billing.billing_account_name:
            main.log.info(json.dumps({"event": "skip", "action": "disable_billing", "project": project_id, "reason": "no_billing_account"}))
            return False

        # Disabling billing = unlink the billing account
        updated = billing_v1.ProjectBillingInfo()
        updated.name = f"projects/{project_id}"
        updated.billing_account_name = ""  # empty = disable
        client.update_project_billing_info(
            name=f"projects/{project_id}",
            project_billing_info=updated,
        )
        main.log.info(json.dumps({"event": "action", "action": "disable_billing", "project": project_id, "status": "done"}))
        return True
    except Exception as exc:
        main.log.error(json.dumps({"event": "action_error", "action": "disable_billing", "project": project_id, "error": str(exc)}))
        return False
