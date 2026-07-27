"""OpenStack provider — kill via Nova compute stop + Ceilometer metering.

Supports kill descriptor:
  {type: "openstack", instance_id: "...", region: "your-region-1"}

Reconciliation via Ceilometer/Gnocchi metering API if the provider
(your-openstack-provider) exposes it; otherwise falls back to self-reports only.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime
from typing import Any

from providers.base import BilledCost, CostProvider, KillResult

log = logging.getLogger("killswitch.providers.openstack")


def _dry_run() -> bool:
    return os.environ.get("DRY_RUN", "true").lower() == "true"


class OpenStackProvider(CostProvider):
    """OpenStack Nova compute kill + Ceilometer metering reconciliation."""

    @property
    def cloud(self) -> str:
        return "openstack"

    def kill_job(self, kill_descriptor: dict[str, Any], reason: str) -> KillResult:
        instance_id = kill_descriptor.get("instance_id", "")
        region = kill_descriptor.get("region", "")
        job_id = kill_descriptor.get("job_id", instance_id)

        if not instance_id:
            return KillResult(killed=False, job_id=job_id, action="openstack", error="missing instance_id")

        if _dry_run():
            log.info(json.dumps({
                "event": "dry_run", "action": "openstack_kill",
                "instance": instance_id, "region": region, "reason": reason,
            }))
            return KillResult(killed=True, job_id=job_id, action="openstack", detail="dry_run")

        # Use openstack CLI — the existing scripts (openstack_shutdown_instances.sh)
        # already use it.  This avoids a heavy SDK dependency.
        cmd = ["openstack", "server", "stop", instance_id]
        if region:
            env = dict(os.environ, OS_REGION_NAME=region)
        else:
            env = dict(os.environ)

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
            if result.returncode != 0:
                log.error(json.dumps({
                    "event": "action_error", "action": "openstack_kill",
                    "instance": instance_id, "error": result.stderr[:200],
                }))
                return KillResult(killed=False, job_id=job_id, action="openstack", error=result.stderr[:200])
            log.info(json.dumps({
                "event": "action", "action": "openstack_kill",
                "instance": instance_id, "region": region, "reason": reason, "status": "done",
            }))
            return KillResult(killed=True, job_id=job_id, action="openstack", detail=instance_id)
        except Exception as exc:
            log.error(json.dumps({
                "event": "action_error", "action": "openstack_kill",
                "instance": instance_id, "error": str(exc)[:200],
            }))
            return KillResult(killed=False, job_id=job_id, action="openstack", error=str(exc)[:200])

    def fetch_billed_costs(
        self, project_id: str, since: datetime, until: datetime
    ) -> list[BilledCost]:
        """Pull metering data from Ceilometer/Gnocchi if available.

        your-openstack-provider may not expose Ceilometer — in that case this
        returns empty and reconciliation falls back to self-reports.
        """
        # TODO: implement Ceilometer/Gnocchi query once we confirm
        # your-openstack-provider exposes the metering API.  For now, return empty
        # so the reconciliation tier degrades gracefully for OpenStack.
        log.info(json.dumps({
            "event": "openstack_reconcile_not_implemented",
            "project": project_id,
            "note": "Ceilometer/Gnocchi access not yet confirmed for your-openstack-provider",
        }))
        return []
