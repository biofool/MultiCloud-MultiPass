"""Portable HTTP-callback kill provider.

Works for any project that exposes a ``POST /admin/kill-job`` (or similar)
endpoint.  The kill descriptor in the intent carries the URL, method, and
headers.  This is the most portable kill mechanism — no cloud-specific
credentials needed, just an authenticated HTTP endpoint in the project.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from providers.base import CostProvider, KillResult

log = logging.getLogger("killswitch.providers.http_callback")


def _dry_run() -> bool:
    return os.environ.get("DRY_RUN", "true").lower() == "true"


class HttpCallbackProvider(CostProvider):
    """Kill a job by POSTing to a project-defined callback URL."""

    @property
    def cloud(self) -> str:
        return "generic"

    def kill_job(self, kill_descriptor: dict[str, Any], reason: str) -> KillResult:
        url = kill_descriptor.get("url", "")
        method = kill_descriptor.get("method", "POST").upper()
        headers = kill_descriptor.get("headers", {})
        job_id = kill_descriptor.get("job_id", "")
        body = {"reason": reason, "job_id": job_id}

        if not url:
            return KillResult(killed=False, job_id=job_id, error="no url in kill descriptor")

        if _dry_run():
            log.info(json.dumps({
                "event": "dry_run", "action": "http_callback_kill",
                "url": url, "job_id": job_id, "reason": reason,
            }))
            return KillResult(killed=True, job_id=job_id, action="http_callback", detail="dry_run")

        try:
            import requests
            resp = requests.request(method, url, json=body, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json() if resp.content else {}
            killed = data.get("killed", True)
            log.info(json.dumps({
                "event": "action", "action": "http_callback_kill",
                "url": url, "job_id": job_id, "reason": reason, "status": "done",
            }))
            return KillResult(
                killed=killed, job_id=job_id, action="http_callback",
                detail=str(data)[:500],
            )
        except Exception as exc:
            log.error(json.dumps({
                "event": "action_error", "action": "http_callback_kill",
                "url": url, "job_id": job_id, "error": str(exc)[:200],
            }))
            return KillResult(killed=False, job_id=job_id, action="http_callback", error=str(exc)[:200])
