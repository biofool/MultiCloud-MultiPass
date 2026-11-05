"""AWS provider — EC2 instance kill + Cost Explorer reconciliation.

Supports kill descriptor:
  {type: "ec2", instance_id: "...", region: "...", action: "stop"|"terminate"}

``action`` defaults to "stop" (reversible, keeps EBS volumes billing at the
much lower storage rate) rather than "terminate" (destroys the instance).
Use "terminate" explicitly when the instance should not come back.

Credentials come from aws_session.get_client(). With
AWS_CREDENTIAL_SOURCE=secretsmanager, keys are read from AWS Secrets Manager.
If they cannot be read, the kill returns killed=False and never falls back to
another identity. On an auth error, the cached session is dropped so the next
call re-reads the secret (see aws_session.py).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any

from providers.base import BilledCost, CostProvider, KillResult

log = logging.getLogger("killswitch.providers.aws")


def _dry_run() -> bool:
    return os.environ.get("DRY_RUN", "true").lower() == "true"


def _invalidate_on_auth_error(exc: Exception) -> None:
    """Drop the cached AWS session when *exc* shows the credentials failed."""
    try:
        import aws_session
    except ImportError:
        return
    if aws_session.is_auth_error(exc):
        aws_session.invalidate_session()


def _ce_region() -> str:
    # Cost Explorer is a global service reachable only via us-east-1.
    return "us-east-1"


class AwsProvider(CostProvider):
    """EC2 kill actions + Cost Explorer billing reconciliation."""

    @property
    def cloud(self) -> str:
        return "aws"

    def kill_job(self, kill_descriptor: dict[str, Any], reason: str) -> KillResult:
        kill_type = kill_descriptor.get("type", "")
        if kill_type != "ec2":
            return KillResult(
                killed=False, job_id=kill_descriptor.get("job_id", ""),
                action=kill_type, error=f"unknown AWS kill type: {kill_type}",
            )
        return self._kill_ec2(kill_descriptor, reason)

    def _kill_ec2(self, desc: dict[str, Any], reason: str) -> KillResult:
        instance_id = desc.get("instance_id", "")
        region = desc.get("region", "us-east-1")
        action = desc.get("action", "stop")
        job_id = desc.get("job_id", instance_id)

        if not instance_id:
            return KillResult(killed=False, job_id=job_id, action="ec2", error="missing instance_id")
        if action not in ("stop", "terminate"):
            return KillResult(killed=False, job_id=job_id, action="ec2", error=f"invalid action: {action}")

        if _dry_run():
            log.info(json.dumps({
                "event": "dry_run", "action": f"ec2_{action}",
                "instance": instance_id, "region": region, "reason": reason,
            }))
            return KillResult(killed=True, job_id=job_id, action="ec2", detail="dry_run")

        try:
            import aws_session

            client = aws_session.get_client("ec2", region=region)
            if action == "terminate":
                client.terminate_instances(InstanceIds=[instance_id])
            else:
                client.stop_instances(InstanceIds=[instance_id])

            log.info(json.dumps({
                "event": "action", "action": f"ec2_{action}",
                "instance": instance_id, "region": region, "reason": reason, "status": "done",
            }))
            return KillResult(killed=True, job_id=job_id, action="ec2", detail=f"{action}:{instance_id}")
        except Exception as exc:
            log.error(json.dumps({
                "event": "action_error", "action": f"ec2_{action}",
                "instance": instance_id, "error": str(exc)[:200],
            }))
            _invalidate_on_auth_error(exc)
            return KillResult(killed=False, job_id=job_id, action="ec2", error=str(exc)[:200])

    def fetch_billed_costs(
        self, project_id: str, since: datetime, until: datetime
    ) -> list[BilledCost]:
        """Pull billed costs from AWS Cost Explorer for reconciliation.

        ``project_id`` is matched against the registry's cost-allocation
        tag value (``project_id`` tag, same convention as the Terraform
        EC2 module) — Cost Explorer has no native concept of our logical
        project_id, so costs are filtered by that tag.
        """
        try:
            import aws_session

            client = aws_session.get_client("ce", region=_ce_region())
            resp = client.get_cost_and_usage(
                TimePeriod={
                    "Start": since.strftime("%Y-%m-%d"),
                    "End": until.strftime("%Y-%m-%d"),
                },
                Granularity="DAILY",
                Metrics=["UnblendedCost", "UsageQuantity"],
                Filter={
                    "Tags": {"Key": "project_id", "Values": [project_id]},
                },
                GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
            )
        except Exception as exc:
            log.warning(json.dumps({
                "event": "ce_query_error", "project": project_id, "error": str(exc)[:200],
            }))
            _invalidate_on_auth_error(exc)
            return []

        costs: list[BilledCost] = []
        for result in resp.get("ResultsByTime", []):
            period_start = _parse_date(result.get("TimePeriod", {}).get("Start"))
            period_end = _parse_date(result.get("TimePeriod", {}).get("End"))
            for group in result.get("Groups", []):
                service = (group.get("Keys") or ["unknown"])[0]
                metrics = group.get("Metrics", {})
                cost_usd = float(metrics.get("UnblendedCost", {}).get("Amount", 0) or 0)
                usage_units = float(metrics.get("UsageQuantity", {}).get("Amount", 0) or 0)
                costs.append(BilledCost(
                    project_id=project_id,
                    provider=service,
                    cost_usd=cost_usd,
                    usage_units=usage_units,
                    usage_unit_type="usage_quantity",
                    period_start=period_start,
                    period_end=period_end,
                    source="cost_explorer",
                    raw=group,
                ))
        return costs


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
