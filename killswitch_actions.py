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

Maintenance-mode snapshot (upstream convergence): the canonical
implementation of this module is maintained upstream in
``biofool/CloudManagement``. This copy is a frozen snapshot kept so
this repo stays standalone-runnable — it receives security fixes and
upstream backports only. See docs/convergence.md.
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
                    actions.append(svc_name)
                else:
                    try:
                        req = run_v2.UpdateServiceRequest(
                            service=svc,
                            field_mask="template.scaling.min_instance_count,ingress",
                        )
                        client.update_service(request=req)
                        main.log.info(json.dumps({"event": "action", "action": "scale_to_zero", "service": svc_name, "status": "done"}))
                        actions.append(svc_name)
                    except Exception as exc:
                        main.log.error(json.dumps({"event": "action_error", "action": "scale_to_zero", "service": svc_name, "error": str(exc)}))
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
                    actions.append(job_name)
                else:
                    try:
                        paused = scheduler_v1.Job()
                        paused.name = job_name
                        paused.state = scheduler_v1.Job.State.PAUSED
                        client.update_job(request={"job": paused, "update_mask": {"paths": ["state"]}})
                        main.log.info(json.dumps({"event": "action", "action": "pause_scheduler", "job": job_name, "status": "done"}))
                        actions.append(job_name)
                    except Exception as exc:
                        main.log.error(json.dumps({"event": "action_error", "action": "pause_scheduler", "job": job_name, "error": str(exc)}))
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
            actions.append(tid)
        else:
            try:
                patch_url = f"https://cloudbuild.googleapis.com/v2/projects/{project_id}/triggers/{tid}"
                patch_resp = requests.patch(patch_url, headers=headers, json={"disabled": True}, timeout=30)
                patch_resp.raise_for_status()
                main.log.info(json.dumps({"event": "action", "action": "disable_trigger", "trigger": tid, "status": "done"}))
                actions.append(tid)
            except Exception as exc:
                main.log.error(json.dumps({"event": "action_error", "action": "disable_trigger", "trigger": tid, "error": str(exc)}))
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
                    actions.append(f"{zone}/{inst_name}")
                else:
                    try:
                        client.stop(project=project_id, zone=zone, instance=inst_name)
                        main.log.info(json.dumps({"event": "action", "action": "stop_instance", "instance": inst_name, "zone": zone, "status": "done"}))
                        actions.append(f"{zone}/{inst_name}")
                    except Exception as exc:
                        main.log.error(json.dumps({"event": "action_error", "action": "stop_instance", "instance": inst_name, "error": str(exc)}))
    return actions


# ---------------------------------------------------------------------------
# Highest-blast-radius actions — implemented in killswitch_destructive.py
# (split during the ≤250-line refactor) and re-exported here so
# `from killswitch_actions import revoke_api_keys, scale_down_gke_clusters,
# disable_billing` keeps working exactly as before.
# ---------------------------------------------------------------------------

from killswitch_destructive import (  # noqa: E402
    disable_billing,
    revoke_api_keys,
    scale_down_gke_clusters,
)
