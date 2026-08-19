"""GCP resource-shutdown actions for the cost kill switch.

Split out of the original monolithic ``main.py`` — pure structural move,
no behavior change. Each function is called either directly (some tests
call e.g. ``main.disable_billing(...)`` after reassigning
``main.ENABLE_BILLING_SHUTOFF``) or via ``main.execute_killswitch``, and
in both cases must see live, possibly test-mutated, values of main's
config flags — hence ``import main`` + qualified ``main.DRY_RUN`` etc.
throughout, rather than ``from main import DRY_RUN`` (which would freeze
a stale copy at import time). ``run_v2``/``scheduler_v1``/``compute_v1``/
``billing_v1`` are the same cached module objects main.py imports (or
tests replace in ``sys.modules``), so patching either import path
patches the same target.
"""

from __future__ import annotations

import json

from google.cloud import run_v2, scheduler_v1, compute_v1, billing_v1

import main

# ---------------------------------------------------------------------------
# Action: scale Cloud Run services to 0 min instances
# ---------------------------------------------------------------------------

def _run_parent(project_id: str, region: str) -> str:
    return f"projects/{project_id}/locations/{region}"


def disable_cloud_run_services(project_id: str, regions: list[str] | None = None) -> list[str]:
    """Set min instances to 0 on all Cloud Run services in a project."""
    regions = regions or ["us-central1", "us-east1", "europe-west1"]
    actions = []
    client = run_v2.ServicesClient()

    for region in regions:
        parent = _run_parent(project_id, region)
        try:
            services = client.list_services(request={"parent": parent})
        except Exception as exc:
            main.log.warning(json.dumps({"event": "list_services_error", "project": project_id, "region": region, "error": str(exc)}))
            continue

        for svc in services:
            svc_name = svc.name  # projects/{proj}/locations/{reg}/services/{name}
            changed = False
            template = svc.template
            if template.scaling and template.scaling.min_instance_count and template.scaling.min_instance_count > 0:
                template.scaling.min_instance_count = 0
                changed = True

            # Restrict ingress to internal-only as a belt-and-suspenders measure
            if svc.ingress != run_v2.IngressTraffic.INTERNAL_ONLY:
                svc.ingress = run_v2.IngressTraffic.INTERNAL_ONLY
                changed = True

            if changed:
                if main.DRY_RUN:
                    main.log.info(json.dumps({"event": "dry_run", "action": "scale_to_zero", "service": svc_name}))
                else:
                    try:
                        req = run_v2.UpdateServiceRequest(
                            service=svc,
                            field_mask="template.scaling.min_instance_count,ingress",
                        )
                        client.update_service(request=req)
                        main.log.info(json.dumps({"event": "action", "action": "scale_to_zero", "service": svc_name, "status": "done"}))
                    except Exception as exc:
                        main.log.error(json.dumps({"event": "action_error", "action": "scale_to_zero", "service": svc_name, "error": str(exc)}))
                actions.append(svc_name)
    return actions


# ---------------------------------------------------------------------------
# Action: pause Cloud Scheduler jobs
# ---------------------------------------------------------------------------

def pause_scheduler_jobs(project_id: str, regions: list[str] | None = None) -> list[str]:
    """Pause all Cloud Scheduler jobs in a project."""
    if not main.ENABLE_RUN_PAUSE:
        return []
    regions = regions or ["us-central1", "us-east1", "europe-west1"]
    actions = []
    client = scheduler_v1.CloudSchedulerClient()

    for region in regions:
        parent = _run_parent(project_id, region)
        try:
            jobs = client.list_jobs(request={"parent": parent})
        except Exception as exc:
            main.log.warning(json.dumps({"event": "list_jobs_error", "project": project_id, "region": region, "error": str(exc)}))
            continue

        for job in jobs:
            if job.state == scheduler_v1.Job.State.ENABLED:
                job_name = job.name
                if main.DRY_RUN:
                    main.log.info(json.dumps({"event": "dry_run", "action": "pause_scheduler", "job": job_name}))
                else:
                    try:
                        paused = scheduler_v1.Job()
                        paused.name = job_name
                        paused.state = scheduler_v1.Job.State.PAUSED
                        client.update_job(request={"job": paused, "update_mask": {"paths": ["state"]}})
                        main.log.info(json.dumps({"event": "action", "action": "pause_scheduler", "job": job_name, "status": "done"}))
                    except Exception as exc:
                        main.log.error(json.dumps({"event": "action_error", "action": "pause_scheduler", "job": job_name, "error": str(exc)}))
                actions.append(job_name)
    return actions


# ---------------------------------------------------------------------------
# Action: disable Cloud Build triggers
# ---------------------------------------------------------------------------

def disable_build_triggers(project_id: str) -> list[str]:
    """Disable all Cloud Build triggers in a project."""
    if not main.ENABLE_TRIGGER_DISABLE:
        return []
    actions = []
    # Use REST via requests to avoid heavy client dependency
    import requests
    import google.auth.transport.requests as tr
    import google.auth

    creds, _ = google.auth.default()
    creds.refresh(tr.Request())

    url = f"https://cloudbuild.googleapis.com/v1/projects/{project_id}/triggers"
    headers = {"Authorization": f"Bearer {creds.token}"}
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        triggers = resp.json().get("triggers", [])
    except Exception as exc:
        main.log.warning(json.dumps({"event": "list_triggers_error", "project": project_id, "error": str(exc)}))
        return []

    for trig in triggers:
        tid = trig.get("id")
        if not tid or not trig.get("disabled", False) is False:
            continue
        if main.DRY_RUN:
            main.log.info(json.dumps({"event": "dry_run", "action": "disable_trigger", "trigger": tid}))
        else:
            try:
                patch_url = f"https://cloudbuild.googleapis.com/v2/projects/{project_id}/triggers/{tid}"
                patch_resp = requests.patch(patch_url, headers=headers, json={"disabled": True}, timeout=30)
                patch_resp.raise_for_status()
                main.log.info(json.dumps({"event": "action", "action": "disable_trigger", "trigger": tid, "status": "done"}))
            except Exception as exc:
                main.log.error(json.dumps({"event": "action_error", "action": "disable_trigger", "trigger": tid, "error": str(exc)}))
        actions.append(tid)
    return actions


# ---------------------------------------------------------------------------
# Action: stop GCE instances
# ---------------------------------------------------------------------------

def stop_compute_instances(project_id: str, zones: list[str] | None = None) -> list[str]:
    """Stop all running GCE instances in configured zones."""
    if not main.STOP_COMPUTE_INSTANCES:
        return []
    actions = []
    client = compute_v1.InstancesClient()

    # If no zones specified, list all zones then iterate
    if not zones:
        try:
            zones_client = compute_v1.ZonesClient()
            zone_list = list(zones_client.list(project=project_id))
            zones = [z.name for z in zone_list]
        except Exception as exc:
            main.log.warning(json.dumps({"event": "list_zones_error", "project": project_id, "error": str(exc)}))
            return []

    for zone in zones:
        try:
            instances = client.list(project=project_id, zone=zone)
        except Exception as exc:
            main.log.warning(json.dumps({"event": "list_instances_error", "project": project_id, "zone": zone, "error": str(exc)}))
            continue

        for inst in instances:
            if inst.status == compute_v1.Instance.Status.RUNNING:
                inst_name = inst.name
                if main.DRY_RUN:
                    main.log.info(json.dumps({"event": "dry_run", "action": "stop_instance", "instance": inst_name, "zone": zone}))
                else:
                    try:
                        client.stop(project=project_id, zone=zone, instance=inst_name)
                        main.log.info(json.dumps({"event": "action", "action": "stop_instance", "instance": inst_name, "zone": zone, "status": "done"}))
                    except Exception as exc:
                        main.log.error(json.dumps({"event": "action_error", "action": "stop_instance", "instance": inst_name, "error": str(exc)}))
                actions.append(f"{zone}/{inst_name}")
    return actions


# ---------------------------------------------------------------------------
# Action: revoke API keys
# ---------------------------------------------------------------------------

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
        else:
            try:
                client.delete_key(name=key_name)
                main.log.info(json.dumps({"event": "action", "action": "revoke_api_key", "key": key_name, "status": "done"}))
            except Exception as exc:
                main.log.error(json.dumps({"event": "action_error", "action": "revoke_api_key", "key": key_name, "error": str(exc)}))
        actions.append(key_name)
    return actions


# ---------------------------------------------------------------------------
# Action: scale down GKE node pools
# ---------------------------------------------------------------------------

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
            else:
                try:
                    pool_name = (
                        f"projects/{project_id}/locations/{cluster.location}"
                        f"/clusters/{cluster.name}/nodePools/{pool.name}"
                    )
                    client.set_node_pool_size(name=pool_name, node_count=0)
                    main.log.info(json.dumps({"event": "action", "action": "scale_down_gke", "pool": pool_id, "status": "done"}))
                except Exception as exc:
                    main.log.error(json.dumps({"event": "action_error", "action": "scale_down_gke", "pool": pool_id, "error": str(exc)}))
            actions.append(pool_id)
    return actions


# ---------------------------------------------------------------------------
# Action: disable billing (nuclear option)
# ---------------------------------------------------------------------------

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
