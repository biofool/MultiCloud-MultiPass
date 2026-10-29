"""Portable HTTP-callback kill provider.

Works for any project that exposes a ``POST /admin/kill-job`` (or similar)
endpoint.  The kill descriptor in the intent carries the URL, method, and
headers.  This is the most portable kill mechanism — no cloud-specific
credentials needed, just an authenticated HTTP endpoint in the project.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import socket
import urllib.parse
from typing import Any

from providers.base import CostProvider, KillResult

log = logging.getLogger("killswitch.providers.http_callback")

# Response bodies are never echoed back — read only enough to parse the
# {"killed": ...} flag, so a hostile or compromised callback cannot push an
# unbounded body into memory or into kill_events/API responses.
_MAX_BODY_BYTES = 64 * 1024


def _dry_run() -> bool:
    return os.environ.get("DRY_RUN", "true").lower() == "true"


def _host_is_public(url: str) -> tuple[bool, str]:
    """Resolve the URL host and reject non-public targets (SSRF guard).

    The kill descriptor comes from a declared intent, but any
    report-token holder can write intents — without this check a
    descriptor pointing at metadata.google.internal or an RFC-1918
    address turns the hub into an internal request proxy whose
    response body used to be echoed into KillResult.detail.
    """
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return False, "callback url is malformed"
    if not host:
        return False, "callback url has no host"
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False, f"callback host {host} does not resolve"
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False, f"callback host {host} resolved to an unparseable address"
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False, f"callback host {host} resolves to a non-public address ({ip_str})"
    return True, ""


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

        try:
            scheme = urllib.parse.urlsplit(url).scheme
        except ValueError:
            scheme = ""
        if scheme not in ("http", "https"):
            return KillResult(killed=False, job_id=job_id, error="callback url must be http/https")
        if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            return KillResult(killed=False, job_id=job_id, error=f"unsupported callback method {method}")

        if _dry_run():
            log.info(json.dumps({
                "event": "dry_run", "action": "http_callback_kill",
                "url": url, "job_id": job_id, "reason": reason,
            }))
            return KillResult(killed=True, job_id=job_id, action="http_callback", detail="dry_run")

        # SSRF guard — only the live path resolves the host (keeps dry-run
        # offline-safe and free of DNS lookups).
        ok, why = _host_is_public(url)
        if not ok:
            log.warning(json.dumps({
                "event": "ssrf_blocked", "action": "http_callback_kill",
                "job_id": job_id, "reason": why,
            }))
            return KillResult(killed=False, job_id=job_id, action="http_callback", error=why)

        try:
            import requests
            # allow_redirects=False — a redirect to a private/metadata
            # address would bypass _host_is_public. stream=True so the
            # body read can be capped instead of loading it all.
            with requests.request(method, url, json=body, headers=headers,
                                  timeout=30, allow_redirects=False,
                                  stream=True) as resp:
                if 300 <= resp.status_code < 400:
                    return KillResult(killed=False, job_id=job_id,
                                      action="http_callback",
                                      error=f"callback redirect refused (HTTP {resp.status_code})")
                resp.raise_for_status()
                raw = resp.raw.read(_MAX_BODY_BYTES, decode_content=True)
            try:
                data = json.loads(raw) if raw else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                data = {}
            killed = bool(data.get("killed", True)) if isinstance(data, dict) else True
            log.info(json.dumps({
                "event": "action", "action": "http_callback_kill",
                "url": url, "job_id": job_id, "reason": reason, "status": "done",
            }))
            # Detail records only the status line — never the response
            # body, which could carry secrets from an internal endpoint.
            return KillResult(
                killed=killed, job_id=job_id, action="http_callback",
                detail=f"callback {method} {urllib.parse.urlsplit(url).hostname} -> HTTP {resp.status_code}",
            )
        except Exception as exc:
            log.error(json.dumps({
                "event": "action_error", "action": "http_callback_kill",
                "url": url, "job_id": job_id, "error": str(exc)[:200],
            }))
            return KillResult(killed=False, job_id=job_id, action="http_callback", error=str(exc)[:200])
