"""
Account registry for the multi-account cost kill switch.

Tracks which GCP projects are monitored, who owns them, their per-project
budget/quota caps, and whether they're allowlisted (never touched by
kill switch actions).

Backing store is selected by USE_FIRESTORE:
  - "true"  (default in production): Firestore native-mode collection
            "accounts" in the hub project.
  - "false" (default for local dev/tests): a YAML file at ACCOUNTS_FILE
            (default: config/accounts.yaml). See config/accounts.example.yaml
            for the schema.

Environment variables:
  USE_FIRESTORE   "true" | "false" (default: "false")
  ACCOUNTS_FILE   Path to the YAML accounts file when USE_FIRESTORE=false
                  (default: "config/accounts.yaml")
  FIRESTORE_PROJECT  Project hosting the Firestore database (default: PROJECT_ID env var)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, asdict
from typing import Any

from paths import resolve as _resolve_path

log = logging.getLogger("killswitch.registry")

USE_FIRESTORE = os.environ.get("USE_FIRESTORE", "false").lower() == "true"
ACCOUNTS_FILE = _resolve_path(os.environ.get("ACCOUNTS_FILE", "config/accounts.yaml"))
FIRESTORE_PROJECT = os.environ.get("FIRESTORE_PROJECT", os.environ.get("PROJECT_ID", ""))
FIRESTORE_COLLECTION = "accounts"


@dataclass
class Account:
    """A monitored cloud account (GCP project, OpenStack project, etc.).

    The ``cloud`` field selects which provider implementation handles
    kill actions and cost reconciliation for this account.  GCP accounts
    carry ``gcp_project_id``; OpenStack accounts carry ``openstack_*``.
    """
    project_id: str                       # logical key used in intent/actual reports
    billing_account_id: str = ""          # GCP billing account ID (GCP only)
    owner_email: str = ""
    allowlist: bool = False
    budget_amount_usd: float = 5.0
    quota_rpm_cap: int = 0                # 0 = no absolute cap; rely on baseline-ratio detection only

    # --- Multi-cloud fields (Phase 1 extension) ---
    cloud: str = "gcp"                    # "gcp" | "openstack" | "cloudflare" | "generic"
    gcp_project_id: str = ""              # actual GCP project ID (may differ from project_id)
    openstack_project: str = ""           # OpenStack project name/ID
    openstack_regions: list[str] | None = None  # OpenStack regions to monitor
    report_token_secret: str = ""         # Secret Manager ref or env var name for the per-project report token
    jobs: list[dict[str, Any]] | None = None     # per-job kill descriptors (fallback when intent has none)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Account":
        return cls(
            project_id=d["project_id"],
            billing_account_id=d.get("billing_account_id", ""),
            owner_email=d.get("owner_email", ""),
            allowlist=bool(d.get("allowlist", False)),
            budget_amount_usd=float(d.get("budget_amount_usd", 5.0)),
            quota_rpm_cap=int(d.get("quota_rpm_cap", 0)),
            cloud=d.get("cloud", "gcp"),
            gcp_project_id=d.get("gcp_project_id", d.get("project_id", "")),
            openstack_project=d.get("openstack_project", ""),
            openstack_regions=d.get("openstack_regions"),
            report_token_secret=d.get("report_token_secret", ""),
            jobs=d.get("jobs"),
        )


# ---------------------------------------------------------------------------
# YAML backend
# ---------------------------------------------------------------------------

def _yaml_load_all() -> list[Account]:
    import yaml

    if not os.path.exists(ACCOUNTS_FILE):
        return []
    with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return [Account.from_dict(a) for a in data.get("accounts", [])]


def _yaml_save_all(accounts: list[Account]) -> None:
    import yaml

    os.makedirs(os.path.dirname(ACCOUNTS_FILE) or ".", exist_ok=True)
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump({"accounts": [a.to_dict() for a in accounts]}, f, sort_keys=False)


# ---------------------------------------------------------------------------
# Firestore backend
# ---------------------------------------------------------------------------

_fs_client = None


def _get_firestore_client():
    global _fs_client
    if _fs_client is None:
        from google.cloud import firestore

        _fs_client = firestore.Client(project=FIRESTORE_PROJECT or None)
    return _fs_client


def _firestore_load_all() -> list[Account]:
    client = _get_firestore_client()
    docs = client.collection(FIRESTORE_COLLECTION).stream()
    accounts = []
    for doc in docs:
        d = doc.to_dict() or {}
        d.setdefault("project_id", doc.id)
        accounts.append(Account.from_dict(d))
    return accounts


def _firestore_upsert(account: Account) -> None:
    client = _get_firestore_client()
    client.collection(FIRESTORE_COLLECTION).document(account.project_id).set(account.to_dict())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_accounts() -> list[Account]:
    """Return all registered accounts. Errors are logged and yield an empty list."""
    try:
        if USE_FIRESTORE:
            return _firestore_load_all()
        return _yaml_load_all()
    except Exception as exc:
        log.error(json.dumps({"event": "registry_load_error", "error": str(exc)}))
        return []


def get_account(project_id: str) -> Account | None:
    for acct in list_accounts():
        if acct.project_id == project_id:
            return acct
    return None


def is_allowlisted(project_id: str) -> bool:
    acct = get_account(project_id)
    return acct is not None and acct.allowlist


def register_account(account: Account) -> None:
    """Add or update an account in the registry."""
    if USE_FIRESTORE:
        _firestore_upsert(account)
        return
    accounts = _yaml_load_all()
    accounts = [a for a in accounts if a.project_id != account.project_id]
    accounts.append(account)
    _yaml_save_all(accounts)
