"""Unified cloud resource inventory.

Reads the account registry (config/accounts.yaml or Firestore) and Terraform
state to produce a single view of every cloud resource and account across
the your-org portfolio. Exposed via GET /api/v1/inventory.

This is the "one place that understands all cloud resources and accounts."
Other repos' PRDs reference this inventory for where to store data and
where to run jobs (see docs/PRD.md, section 6).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, asdict, field
from typing import Any

import registry
from paths import resolve as _resolve_path

log = logging.getLogger("cloudmanagement.inventory")


def _tf_state_file() -> str:
    """Return the terraform state file path, reading TERRAFORM_DIR at call time."""
    tf_dir = os.environ.get("TERRAFORM_DIR", "terraform")
    if os.path.isabs(tf_dir):
        return os.path.join(tf_dir, "terraform.tfstate")
    return _resolve_path(tf_dir, "terraform.tfstate")


@dataclass
class Resource:
    """A single cloud resource (Cloud Run service, bucket, dataset, etc.)."""
    account_id: str = ""
    cloud: str = ""
    type: str = ""          # "cloud_run" | "cloud_function" | "bigquery_dataset" | "storage_bucket" | ...
    name: str = ""
    region: str = ""
    project_id: str = ""    # GCP project ID (if applicable)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _load_terraform_resources() -> list[Resource]:
    """Parse terraform.tfstate (if present) and extract managed resources.

    Returns an empty list if the state file doesn't exist or can't be parsed.
    Errors are logged at WARNING — the inventory degrades gracefully.
    """
    resources: list[Resource] = []
    state_file = _tf_state_file()
    if not os.path.exists(state_file):
        log.debug("inventory: no terraform state file at %s", state_file)
        return resources

    try:
        with open(state_file, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception as exc:
        log.warning(json.dumps({"event": "inventory_tfstate_load_error", "error": str(exc)}))
        return resources

    # Map Terraform resource types to inventory resource types
    type_map = {
        "google_cloud_run_service": "cloud_run",
        "google_cloudfunctions2_function": "cloud_function",
        "google_cloud_scheduler_job": "cloud_scheduler",
        "google_bigquery_dataset": "bigquery_dataset",
        "google_storage_bucket": "storage_bucket",
        "google_firestore_database": "firestore_database",
        "google_pubsub_topic": "pubsub_topic",
        "google_billing_budget": "billing_budget",
        "google_service_account": "service_account",
    }

    for tf_res in state.get("resources", []):
        tf_type = tf_res.get("type", "")
        inv_type = type_map.get(tf_type)
        if inv_type is None:
            continue
        for inst in tf_res.get("instances", []):
            attrs = inst.get("attributes", {})
            resources.append(Resource(
                account_id="",  # filled by build_inventory
                cloud="gcp",
                type=inv_type,
                name=attrs.get("name", tf_res.get("name", "")),
                region=attrs.get("location", attrs.get("region", "")),
                project_id=attrs.get("project", ""),
                metadata={
                    "tf_type": tf_type,
                    "tf_name": tf_res.get("name", ""),
                },
            ))

    return resources


def build_inventory() -> dict[str, Any]:
    """Build the unified inventory from accounts + terraform state.

    Returns a dict with:
      - accounts: list of account dicts (from registry)
      - resources: list of resource dicts (from terraform state)
      - summary: counts by cloud and type
    """
    accounts = registry.list_accounts()
    account_dicts = [a.to_dict() for a in accounts]

    resources = _load_terraform_resources()

    # Associate resources with accounts by matching project_id
    project_to_account = {}
    for acct in accounts:
        gcp_pid = acct.gcp_project_id or acct.project_id
        if gcp_pid:
            project_to_account[gcp_pid] = acct.project_id

    for res in resources:
        if res.project_id and res.project_id in project_to_account:
            res.account_id = project_to_account[res.project_id]
        elif not res.account_id:
            # Default to the hub project (first allowlisted account)
            for acct in accounts:
                if acct.allowlist:
                    res.account_id = acct.project_id
                    break

    resource_dicts = [r.to_dict() for r in resources]

    # Summary
    by_cloud: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for r in resource_dicts:
        by_cloud[r["cloud"]] = by_cloud.get(r["cloud"], 0) + 1
        by_type[r["type"]] = by_type.get(r["type"], 0) + 1

    return {
        "accounts": account_dicts,
        "resources": resource_dicts,
        "summary": {
            "account_count": len(account_dicts),
            "resource_count": len(resource_dicts),
            "by_cloud": by_cloud,
            "by_type": by_type,
        },
    }


# ---------------------------------------------------------------------------
# Flask blueprint
# ---------------------------------------------------------------------------

import flask

bp = flask.Blueprint("inventory", __name__)


@bp.route("/api/v1/inventory", methods=["GET"])
def get_inventory():
    """Return the unified cloud resource inventory."""
    try:
        inv = build_inventory()
        return flask.jsonify(inv)
    except Exception as exc:
        log.error(json.dumps({"event": "inventory_build_error", "error": str(exc)}))
        return flask.jsonify({"error": "inventory build failed", "detail": str(exc)}), 500
