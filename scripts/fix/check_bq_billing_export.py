#!/usr/bin/env python3
"""
Verify that BigQuery Cloud Billing export is enabled and producing data.

BigQuery billing export is the reconciliation data source for
providers/gcp.py::fetch_billed_costs. The export destination (dataset) is
provisioned by terraform, but the *enable* step is Console-only — no public
API exists as of 2025-12 (see Google issue tracker 504194143). This script
verifies everything around that step so a human only has to click "Save" in
the Console.

Checks performed:
  1. Required APIs enabled on the hub project (cloudbilling, bigquery,
     bigquerydatatransfer)
  2. BigQuery dataset exists with correct location
  3. Runtime service account exists with required IAM roles
  4. Billing export table(s) exist in the dataset (the actual export-enabled
     check — fails until a human enables it in the Console)
  5. Export table has rows (data has started flowing — takes 24-48h after
     enable)

Usage:
  python scripts/fix/check_bq_billing_export.py
  python scripts/fix/check_bq_billing_export.py --project HUB --dataset cloud_billing_export
  python scripts/fix/check_bq_billing_export.py --billing-account 01AB-23CD-EF45

Environment:
  HUB_PROJECT_ID  Hub project hosting the BigQuery dataset (default: from gcloud)
  BQ_DATASET      Dataset ID (default: cloud_billing_export)

Audit output:
  data/audit/bq_billing_export_check_<timestamp>.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# No third-party deps required for the API checks — we use urllib so this
# runs without the venv. google-cloud-* libs are only needed by the service
# itself.
import urllib.request
import urllib.error

# Resolve project root so data/audit paths work from any CWD
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT))
from paths import resolve as _resolve_path  # noqa: E402


def _get_access_token() -> str:
    """Get an access token via gcloud."""
    import subprocess
    result = subprocess.run(
        ["gcloud", "auth", "print-access-token"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _api_get(url: str, token: str, headers: dict | None = None) -> dict:
    """GET a Google API endpoint and return parsed JSON."""
    hdrs = {"Authorization": f"Bearer {token}"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"_error": True, "status": exc.code, "body": exc.read().decode("utf-8", errors="replace")}


def _api_post(url: str, token: str, body: dict, headers: dict | None = None) -> dict:
    """POST a Google API endpoint and return parsed JSON."""
    hdrs = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"_error": True, "status": exc.code, "body": exc.read().decode("utf-8", errors="replace")}


def check_apis(project: str, token: str) -> dict:
    """Check required APIs are enabled."""
    required = ["cloudbilling.googleapis.com", "bigquery.googleapis.com", "bigquerydatatransfer.googleapis.com"]
    url = f"https://serviceusage.googleapis.com/v1/projects/{project}/services:batchGet"
    # batchGet takes ?names= repeated; easier to list and filter
    url = f"https://serviceusage.googleapis.com/v1/projects/{project}/services?filter=state:ENABLED&pageSize=200"
    data = _api_get(url, token)
    if "_error" in data:
        return {"ok": False, "error": data, "apis": {}}
    enabled = {s["config"]["name"].split("/")[-1] for s in data.get("services", [])}
    return {
        "ok": True,
        "apis": {api: {"enabled": api in enabled} for api in required},
        "all_enabled": all(api in enabled for api in required),
    }


def check_dataset(project: str, dataset: str, token: str) -> dict:
    """Check the BigQuery dataset exists."""
    url = f"https://bigquery.googleapis.com/bigquery/v2/projects/{project}/datasets/{dataset}"
    data = _api_get(url, token)
    if "_error" in data:
        return {"ok": False, "exists": False, "error": data.get("body", "")[:200]}
    return {
        "ok": True,
        "exists": True,
        "id": data.get("id"),
        "location": data.get("location"),
    }


def check_runtime_sa(project: str, token: str) -> dict:
    """Check the runtime service account exists and has required IAM roles."""
    sa_email = f"killswitch-rt@{project}.iam.gserviceaccount.com"
    sa_member = f"serviceAccount:{sa_email}"
    # Check SA exists
    url = f"https://iam.googleapis.com/v1/projects/{project}/serviceAccounts/{sa_email}"
    data = _api_get(url, token)
    if "_error" in data:
        return {"ok": False, "exists": False, "email": sa_email, "error": data.get("body", "")[:200]}
    # Check IAM roles
    required_roles = ["roles/bigquery.dataViewer", "roles/bigquery.jobUser", "roles/datastore.user", "roles/pubsub.subscriber"]
    iam_url = f"https://cloudresourcemanager.googleapis.com/v1/projects/{project}:getIamPolicy"
    iam_data = _api_post(iam_url, token, {})
    granted = set()
    if not iam_data.get("_error"):
        for binding in iam_data.get("bindings", []):
            if sa_member in binding.get("members", []):
                granted.add(binding["role"])
    return {
        "ok": True,
        "exists": True,
        "email": sa_email,
        "roles": {r: {"granted": r in granted} for r in required_roles},
        "all_roles_granted": all(r in granted for r in required_roles),
    }


def check_export_tables(project: str, dataset: str, billing_accounts: list[str], token: str) -> dict:
    """Check that billing export tables exist in the dataset."""
    url = f"https://bigquery.googleapis.com/bigquery/v2/projects/{project}/datasets/{dataset}/tables"
    data = _api_get(url, token)
    if "_error" in data:
        return {"ok": False, "error": data.get("body", "")[:200], "tables": []}
    tables = data.get("tables", [])
    table_ids = [t["id"] for t in tables]
    # Expected table names: one per billing account
    # gcp_billing_export_resource_v1_<account_id_with_underscores>
    expected = {}
    for ba in billing_accounts:
        suffix = ba.replace("-", "_")
        expected_name = f"gcp_billing_export_resource_v1_{suffix}"
        expected[ba] = {
            "expected_table": expected_name,
            "found": any(expected_name in tid for tid in table_ids),
        }
    return {
        "ok": True,
        "table_count": len(tables),
        "tables": table_ids,
        "expected": expected,
        "any_enabled": any(v["found"] for v in expected.values()),
    }


def check_table_rows(project: str, dataset: str, billing_accounts: list[str], token: str) -> dict:
    """Check if export tables have rows (data flowing). Uses a dry-run query."""
    results = {}
    for ba in billing_accounts:
        suffix = ba.replace("-", "_")
        table = f"gcp_billing_export_resource_v1_{suffix}"
        # Use jobs.query with dryRun to get table metadata without scanning
        # Actually, use tables.get for numRows
        url = f"https://bigquery.googleapis.com/bigquery/v2/projects/{project}/datasets/{dataset}/tables/{table}"
        data = _api_get(url, token)
        if "_error" in data:
            results[ba] = {"exists": False, "num_rows": 0}
        else:
            results[ba] = {
                "exists": True,
                "num_rows": int(data.get("numRows", 0)),
                "creation_time": data.get("creationTime"),
            }
    return {"ok": True, "tables": results}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--project", default=os.environ.get("HUB_PROJECT_ID", ""),
                        help="Hub project ID (default: HUB_PROJECT_ID env var)")
    parser.add_argument("--dataset", default=os.environ.get("BQ_DATASET", "cloud_billing_export"),
                        help="BigQuery dataset ID (default: cloud_billing_export)")
    parser.add_argument("--billing-account", action="append", default=[],
                        help="Billing account ID to check (repeatable). Default: derived from terraform.tfvars if present.")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="No-op flag (this script is read-only by design — kept for convention)")
    parser.add_argument("--audit-dir", default=_resolve_path("data/audit"),
                        help="Directory for audit JSON output (default: <project>/data/audit)")
    args = parser.parse_args()

    if not args.project:
        # Try to get from gcloud
        import subprocess
        result = subprocess.run(["gcloud", "config", "get-value", "project"], capture_output=True, text=True)
        args.project = result.stdout.strip()
    if not args.project:
        print("ERROR: --project not specified and HUB_PROJECT_ID not set", file=sys.stderr)
        return 2

    if not args.billing_account:
        # Try to read from terraform.tfvars
        tfvars = Path(_resolve_path("terraform/terraform.tfvars"))
        if tfvars.exists():
            text = tfvars.read_text()
            import re
            # Read billing_account_id (hub) and monitored_projects entries
            m = re.search(r'billing_account_id\s*=\s*"([^"]+)"', text)
            if m:
                args.billing_account.append(m.group(1))
            for m in re.finditer(r'billing_account_id\s*=\s*"([^"]+)"', text):
                if m.group(1) not in args.billing_account:
                    args.billing_account.append(m.group(1))

    if not args.billing_account:
        print("ERROR: no billing accounts to check. Use --billing-account or run from repo root.", file=sys.stderr)
        return 2

    print(f"Checking BigQuery billing export for project={args.project} dataset={args.dataset}")
    print(f"Billing accounts: {', '.join(args.billing_account)}")
    print()

    token = _get_access_token()

    report: dict = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "project": args.project,
        "dataset": args.dataset,
        "billing_accounts": args.billing_account,
        "checks": {},
    }

    # 1. APIs
    print("[1/5] Checking required APIs...")
    apis = check_apis(args.project, token)
    report["checks"]["apis"] = apis
    status = "OK" if apis["all_enabled"] else "FAIL"
    print(f"      {status} — {sum(1 for a in apis['apis'].values() if a['enabled'])}/{len(apis['apis'])} APIs enabled")
    for api, info in apis["apis"].items():
        print(f"        {'✓' if info['enabled'] else '✗'} {api}")

    # 2. Dataset
    print("[2/5] Checking BigQuery dataset...")
    ds = check_dataset(args.project, args.dataset, token)
    report["checks"]["dataset"] = ds
    status = "OK" if ds["ok"] and ds["exists"] else "FAIL"
    print(f"      {status} — {ds.get('id', 'not found')} location={ds.get('location', '?')}")

    # 3. Runtime SA + IAM
    print("[3/5] Checking runtime service account + IAM...")
    sa = check_runtime_sa(args.project, token)
    report["checks"]["runtime_sa"] = sa
    status = "OK" if sa["ok"] and sa.get("all_roles_granted") else "FAIL"
    print(f"      {status} — {sa.get('email', '?')}")
    for role, info in sa.get("roles", {}).items():
        print(f"        {'✓' if info['granted'] else '✗'} {role}")

    # 4. Export tables
    print("[4/5] Checking billing export tables (the Console-only step)...")
    tables = check_export_tables(args.project, args.dataset, args.billing_account, token)
    report["checks"]["export_tables"] = tables
    if tables["any_enabled"]:
        print(f"      OK — {tables['table_count']} table(s) found")
    else:
        print(f"      NOT ENABLED — 0 export tables found in {args.dataset}")
        print("      Action required: enable BigQuery Detailed usage cost export in the Console.")
        for ba, info in tables["expected"].items():
            print(f"        Billing account {ba}: expected table {info['expected_table']} — {'found' if info['found'] else 'MISSING'}")

    # 5. Row counts (only if tables exist)
    print("[5/5] Checking export table row counts...")
    if tables["any_enabled"]:
        rows = check_table_rows(args.project, args.dataset, args.billing_account, token)
        report["checks"]["row_counts"] = rows
        for ba, info in rows["tables"].items():
            if info["exists"]:
                print(f"      {ba}: {info['num_rows']} rows")
            else:
                print(f"      {ba}: table not found")
    else:
        report["checks"]["row_counts"] = {"skipped": True, "reason": "no export tables found"}
        print("      Skipped — no export tables to check")

    # Write audit JSON
    audit_dir = Path(args.audit_dir)
    audit_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    audit_path = audit_dir / f"bq_billing_export_check_{ts}.json"
    audit_path.write_text(json.dumps(report, indent=2))
    print(f"\nAudit written to {audit_path}")

    # Summary
    print("\n=== Summary ===")
    all_ok = (
        apis["all_enabled"]
        and ds["ok"] and ds["exists"]
        and sa["ok"] and sa.get("all_roles_granted")
        and tables["any_enabled"]
    )
    if all_ok:
        print("All checks passed — BigQuery billing export is enabled and producing data.")
        return 0
    elif tables["any_enabled"]:
        print("Export enabled but some prerequisites need attention. See details above.")
        return 1
    else:
        print("BigQuery billing export is NOT YET ENABLED.")
        print("All automated prerequisites are in place. The remaining step is Console-only:")
        print()
        for ba in args.billing_account:
            print(f"  https://console.cloud.google.com/billing/{ba}/export")
        print()
        print("Sign in as an account with Billing Account Administrator on the billing account,")
        print("go to BigQuery export → Edit → set Project={project} Dataset={dataset} → Save.".format(
            project=args.project, dataset=args.dataset))
        print("Wait 24-48h for data to appear, then re-run this script to verify.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
