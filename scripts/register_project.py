#!/usr/bin/env python3
"""
Self-service registration for the multi-account cost kill switch.

Run this once per teammate project to add it to the account registry
(YAML file locally, Firestore in production — see registry.py).

Usage:
  python scripts/register_project.py \\
      --project my-sandbox-project \\
      --billing-account 01AB-23CD-EF45 \\
      --owner me@example.com \\
      [--budget 10] [--quota-rpm-cap 6000] [--allowlist]

Environment variables (same as registry.py):
  USE_FIRESTORE   "true" to write to Firestore instead of the local YAML file
  ACCOUNTS_FILE   YAML file path when USE_FIRESTORE=false (default: config/accounts.yaml)
  FIRESTORE_PROJECT  Hub project hosting Firestore, when USE_FIRESTORE=true
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT))

import registry  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", required=True, help="GCP project ID to monitor")
    parser.add_argument("--billing-account", required=True, help="Cloud Billing account ID (e.g. 01AB-23CD-EF45)")
    parser.add_argument("--owner", required=True, help="Owner's email address")
    parser.add_argument("--budget", type=float, default=5.0, help="Monthly budget in USD (default: 5)")
    parser.add_argument("--quota-rpm-cap", type=int, default=0, help="Absolute request/min cap for the poller fallback (default: 0 = disabled)")
    parser.add_argument("--allowlist", action="store_true", help="Mark this project as never touched by kill switch actions")
    args = parser.parse_args()

    account = registry.Account(
        project_id=args.project,
        billing_account_id=args.billing_account,
        owner_email=args.owner,
        allowlist=args.allowlist,
        budget_amount_usd=args.budget,
        quota_rpm_cap=args.quota_rpm_cap,
    )
    registry.register_account(account)

    backend = "Firestore" if registry.USE_FIRESTORE else registry.ACCOUNTS_FILE
    print(f"Registered '{args.project}' in {backend}.")
    print(
        "Reminder: this project must live under the team's shared GCP Organization "
        "or Folder, or the hub service account's IAM grants won't reach it."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
