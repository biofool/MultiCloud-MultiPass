"""Billing account lookup for the GCP provider — maps a GCP project to
its billing account ID (cached).

Split out of ``providers/gcp.py`` during the ≤250-line refactor — pure
structural move, no behavior change. ``providers/gcp.py`` re-exports
``_billing_account_for_project`` so existing call sites keep working.
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger("killswitch.providers.gcp")

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
