"""GCP provider — wraps the existing kill-switch actions from main.py.

Supports GCP-native kill descriptors:
  - {type: "cloud_run", service: "...", region: "..."}
  - {type: "cloud_scheduler", job: "...", location: "..."}
  - {type: "gce", instance: "...", zone: "..."}
  - {type: "gke", cluster: "...", node_pool: "...", location: "..."}

Also implements fetch_billed_costs via BigQuery billing export for the
reconciliation tier.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any

from providers.base import BilledCost, CostProvider, KillResult

log = logging.getLogger("killswitch.providers.gcp")


def _dry_run() -> bool:
    return os.environ.get("DRY_RUN", "true").lower() == "true"


def _bq_dataset() -> str:
    return os.environ.get("BQ_DATASET", "cloud_billing_export")


def _hub_project_id() -> str:
    return os.environ.get("HUB_PROJECT_ID", os.environ.get("PROJECT_ID", ""))


class GcpProvider(CostProvider):
    """GCP-native kill actions + BigQuery billing reconciliation."""

    @property
    def cloud(self) -> str:
        return "gcp"

    def kill_job(self, kill_descriptor: dict[str, Any], reason: str) -> KillResult:
        kill_type = kill_descriptor.get("type", "")
        project_id = kill_descriptor.get("project_id", "")
        job_id = kill_descriptor.get("job_id", "")

        if kill_type == "cloud_run":
            return self._kill_cloud_run(kill_descriptor, reason)
        elif kill_type == "cloud_scheduler":
            return self._kill_cloud_scheduler(kill_descriptor, reason)
        elif kill_type == "gce":
            return self._kill_gce(kill_descriptor, reason)
        elif kill_type == "gke":
            return self._kill_gke(kill_descriptor, reason)
        else:
            return KillResult(
                killed=False, job_id=job_id, action=kill_type,
                error=f"unknown GCP kill type: {kill_type}",
            )

    def _kill_cloud_run(self, desc: dict[str, Any], reason: str) -> KillResult:
        from google.cloud import run_v2

        project = desc.get("project_id", "")
        service = desc.get("service", "")
        region = desc.get("region", "us-central1")
        job_id = desc.get("job_id", service)

        if not project or not service:
            return KillResult(killed=False, job_id=job_id, action="cloud_run", error="missing project_id or service")

        name = f"projects/{project}/locations/{region}/services/{service}"
        if _dry_run():
            log.info(json.dumps({"event": "dry_run", "action": "cloud_run_kill", "service": name, "reason": reason}))
            return KillResult(killed=True, job_id=job_id, action="cloud_run", detail="dry_run")

        try:
            client = run_v2.ServicesClient()
            svc = client.get_service(name=name)
            changed = False
            if svc.template.scaling and svc.template.scaling.min_instance_count and svc.template.scaling.min_instance_count > 0:
                svc.template.scaling.min_instance_count = 0
                changed = True
            if svc.ingress != run_v2.IngressTraffic.INTERNAL_ONLY:
                svc.ingress = run_v2.IngressTraffic.INTERNAL_ONLY
                changed = True
            if changed:
                client.update_service(request=run_v2.UpdateServiceRequest(
                    service=svc,
                    field_mask="template.scaling.min_instance_count,ingress",
                ))
            log.info(json.dumps({"event": "action", "action": "cloud_run_kill", "service": name, "reason": reason, "status": "done"}))
            return KillResult(killed=True, job_id=job_id, action="cloud_run", detail=name)
        except Exception as exc:
            log.error(json.dumps({"event": "action_error", "action": "cloud_run_kill", "service": name, "error": str(exc)[:200]}))
            return KillResult(killed=False, job_id=job_id, action="cloud_run", error=str(exc)[:200])

    def _kill_cloud_scheduler(self, desc: dict[str, Any], reason: str) -> KillResult:
        from google.cloud import scheduler_v1

        project = desc.get("project_id", "")
        job_name = desc.get("job", "")
        location = desc.get("location", "us-central1")
        job_id = desc.get("job_id", job_name)

        if not project or not job_name:
            return KillResult(killed=False, job_id=job_id, action="cloud_scheduler", error="missing project_id or job")

        name = f"projects/{project}/locations/{location}/jobs/{job_name}"
        if _dry_run():
            log.info(json.dumps({"event": "dry_run", "action": "cloud_scheduler_kill", "job": name, "reason": reason}))
            return KillResult(killed=True, job_id=job_id, action="cloud_scheduler", detail="dry_run")

        try:
            client = scheduler_v1.CloudSchedulerClient()
            paused = scheduler_v1.Job()
            paused.name = name
            paused.state = scheduler_v1.Job.State.PAUSED
            client.update_job(request={"job": paused, "update_mask": {"paths": ["state"]}})
            log.info(json.dumps({"event": "action", "action": "cloud_scheduler_kill", "job": name, "reason": reason, "status": "done"}))
            return KillResult(killed=True, job_id=job_id, action="cloud_scheduler", detail=name)
        except Exception as exc:
            log.error(json.dumps({"event": "action_error", "action": "cloud_scheduler_kill", "job": name, "error": str(exc)[:200]}))
            return KillResult(killed=False, job_id=job_id, action="cloud_scheduler", error=str(exc)[:200])

    def _kill_gce(self, desc: dict[str, Any], reason: str) -> KillResult:
        from google.cloud import compute_v1

        project = desc.get("project_id", "")
        instance = desc.get("instance", "")
        zone = desc.get("zone", "us-central1-a")
        job_id = desc.get("job_id", instance)

        if not project or not instance:
            return KillResult(killed=False, job_id=job_id, action="gce", error="missing project_id or instance")

        if _dry_run():
            log.info(json.dumps({"event": "dry_run", "action": "gce_kill", "instance": f"{zone}/{instance}", "reason": reason}))
            return KillResult(killed=True, job_id=job_id, action="gce", detail="dry_run")

        try:
            client = compute_v1.InstancesClient()
            client.stop(project=project, zone=zone, instance=instance)
            log.info(json.dumps({"event": "action", "action": "gce_kill", "instance": f"{zone}/{instance}", "reason": reason, "status": "done"}))
            return KillResult(killed=True, job_id=job_id, action="gce", detail=f"{zone}/{instance}")
        except Exception as exc:
            log.error(json.dumps({"event": "action_error", "action": "gce_kill", "instance": instance, "error": str(exc)[:200]}))
            return KillResult(killed=False, job_id=job_id, action="gce", error=str(exc)[:200])

    def _kill_gke(self, desc: dict[str, Any], reason: str) -> KillResult:
        from google.cloud import container_v1

        project = desc.get("project_id", "")
        cluster = desc.get("cluster", "")
        node_pool = desc.get("node_pool", "")
        location = desc.get("location", "us-central1")
        job_id = desc.get("job_id", f"{cluster}/{node_pool}")

        if not project or not cluster or not node_pool:
            return KillResult(killed=False, job_id=job_id, action="gke", error="missing project_id, cluster, or node_pool")

        pool_name = f"projects/{project}/locations/{location}/clusters/{cluster}/nodePools/{node_pool}"
        if _dry_run():
            log.info(json.dumps({"event": "dry_run", "action": "gke_kill", "pool": pool_name, "reason": reason}))
            return KillResult(killed=True, job_id=job_id, action="gke", detail="dry_run")

        try:
            client = container_v1.ClusterManagerClient()
            client.set_node_pool_size(name=pool_name, node_count=0)
            log.info(json.dumps({"event": "action", "action": "gke_kill", "pool": pool_name, "reason": reason, "status": "done"}))
            return KillResult(killed=True, job_id=job_id, action="gke", detail=pool_name)
        except Exception as exc:
            log.error(json.dumps({"event": "action_error", "action": "gke_kill", "pool": pool_name, "error": str(exc)[:200]}))
            return KillResult(killed=False, job_id=job_id, action="gke", error=str(exc)[:200])

    def fetch_billed_costs(
        self, project_id: str, since: datetime, until: datetime
    ) -> list[BilledCost]:
        """Pull billed costs from BigQuery billing export for reconciliation."""
        from google.cloud import bigquery

        billing_account = _billing_account_for_project(project_id)
        if not billing_account:
            log.warning(json.dumps({"event": "reconcile_skip", "project": project_id, "reason": "no billing account"}))
            return []

        table_suffix = billing_account.replace("-", "_")
        table = f"{_hub_project_id()}.{_bq_dataset()}.gcp_billing_export_resource_v1_{table_suffix}"

        query = f"""
            SELECT
              project.id AS project_id,
              service.description AS service,
              sku.description AS sku,
              SUM(cost) AS cost_usd,
              SUM(IFNULL(usage.amount, 0)) AS usage_units,
              usage.unit AS usage_unit_type,
              DATE(usage_start_time) AS period_date
            FROM `{table}`
            WHERE invoice.month = FORMAT_DATE('%Y%m', '{since:%Y-%m-%d}')
              AND project.id = @project_id
              AND usage_start_time >= @since
              AND usage_start_time < @until
            GROUP BY project_id, service, sku, usage_unit_type, period_date
            ORDER BY cost_usd DESC
        """
        client = bigquery.Client(project=_hub_project_id() or None)
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("project_id", "STRING", project_id),
                bigquery.ScalarQueryParameter("since", "TIMESTAMP", since),
                bigquery.ScalarQueryParameter("until", "TIMESTAMP", until),
            ]
        )
        try:
            results = client.query(query, job_config=job_config).result()
            costs = []
            for row in results:
                costs.append(BilledCost(
                    project_id=row.project_id or project_id,
                    provider=row.service or "unknown",
                    api=row.sku or "",
                    cost_usd=float(row.cost_usd or 0),
                    usage_units=float(row.usage_units or 0),
                    usage_unit_type=row.usage_unit_type or "",
                    period_start=datetime.combine(row.period_date, datetime.min.time()) if row.period_date else None,
                    source="bigquery_export",
                ))
            return costs
        except Exception as exc:
            log.warning(json.dumps({"event": "bq_query_error", "project": project_id, "error": str(exc)[:200]}))
            return []


# ---------------------------------------------------------------------------
# Billing account lookup — maps a GCP project to its billing account ID
# ---------------------------------------------------------------------------

_project_billing_cache: dict[str, str] = {}


def _billing_account_for_project(gcp_project_id: str) -> str:
    """Look up the billing account ID for a GCP project (cached)."""
    if gcp_project_id in _project_billing_cache:
        return _project_billing_cache[gcp_project_id]

    try:
        from google.cloud import billing_v1
        client = billing_v1.CloudBillingClient()
        info = client.get_project_billing_info(name=f"projects/{gcp_project_id}")
        account = info.billing_account_name.replace("billingAccounts/", "") if info.billing_account_name else ""
        _project_billing_cache[gcp_project_id] = account
        return account
    except Exception as exc:
        log.warning(json.dumps({"event": "billing_lookup_error", "project": gcp_project_id, "error": str(exc)[:200]}))
        return ""
