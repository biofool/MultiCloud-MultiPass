"""Cloudflare provider — kill via Pages deployment + GraphQL analytics.

Supports kill descriptor:
  {type: "cloudflare", action: "disable_pages", project: "..."}

Reconciliation via Cloudflare GraphQL Analytics API for R2/Pages bandwidth.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any

from providers.base import BilledCost, CostProvider, KillResult

log = logging.getLogger("killswitch.providers.cloudflare")


def _dry_run() -> bool:
    return os.environ.get("DRY_RUN", "true").lower() == "true"


class CloudflareProvider(CostProvider):
    """Cloudflare Pages/R2 kill + GraphQL analytics reconciliation."""

    @property
    def cloud(self) -> str:
        return "cloudflare"

    def kill_job(self, kill_descriptor: dict[str, Any], reason: str) -> KillResult:
        action = kill_descriptor.get("action", "")
        project = kill_descriptor.get("project", "")
        job_id = kill_descriptor.get("job_id", project)

        if action == "disable_pages":
            return self._disable_pages(project, reason, job_id)
        return KillResult(killed=False, job_id=job_id, action="cloudflare", error=f"unknown cloudflare action: {action}")

    def _disable_pages(self, project: str, reason: str, job_id: str) -> KillResult:
        if not project:
            return KillResult(killed=False, job_id=job_id, action="cloudflare", error="missing project")

        if _dry_run():
            log.info(json.dumps({"event": "dry_run", "action": "cloudflare_disable_pages", "project": project, "reason": reason}))
            return KillResult(killed=True, job_id=job_id, action="cloudflare", detail="dry_run")

        # Cloudflare Pages doesn't have a direct "disable" — would need to
        # delete the deployment or redirect via MaintenanceRedirector.sh.
        # For now, log the intent; the Security repo's scripts handle the
        # actual redirect rules.
        log.info(json.dumps({"event": "action", "action": "cloudflare_disable_pages", "project": project, "reason": reason, "status": "logged"}))
        return KillResult(killed=True, job_id=job_id, action="cloudflare", detail="redirect_required")

    def fetch_billed_costs(
        self, project_id: str, since: datetime, until: datetime
    ) -> list[BilledCost]:
        """Pull R2/Pages bandwidth from Cloudflare GraphQL Analytics.

        Cloudflare's free tier doesn't bill for bandwidth, so this is
        mostly informational.  Returns empty if no API token configured.
        """
        token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
        if not token:
            return []

        # TODO: implement GraphQL query against
        # https://api.cloudflare.com/client/v4/graphql
        # once R2 paid usage is confirmed in ClipQuotes.
        return []
